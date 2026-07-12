from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, Float, ForeignKey, Index, Integer,
                        String, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


def _utcnow() -> datetime:
    # Naive UTC, matching how every other timestamp in this DB is stored.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Athlete(Base):
    """The domain object every data row's athlete_id refers to. Auth lives in
    auth_identities — never store provider ids (google_id etc.) here."""
    __tablename__ = "athletes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    # "athlete" | "coach". Coaches may read any athlete's data. Promotion is a
    # manual DB operation for now (see CLAUDE.md "Auth").
    role: Mapped[str] = mapped_column(String, nullable=False, default="athlete")
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)


class AuthIdentity(Base):
    """One external sign-in linked to an athlete. Provider-agnostic by design:
    Google today, Apple next (required once the iOS app ships with Google
    sign-in). One athlete can hold several identities (Google + Apple)."""
    __tablename__ = "auth_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(ForeignKey("athletes.id"),
                                            nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # "google", "apple"
    provider_user_id: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False,
                                                 default=_utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id",
                         name="uq_identity_provider_user"),
    )


class Sync(Base):
    """One health_sync upload, stored whole so the detection algorithm can be
    re-run against old data later without the client re-uploading."""
    __tablename__ = "syncs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    client_version: Mapped[str | None] = mapped_column(String, nullable=True)
    source_platform: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_syncs_athlete_uploaded", "athlete_id", "uploaded_at"),
    )


class HeartRateSample(Base):
    """One heart-rate reading, deduped by (uuid, time). Health Connect packs
    many readings into a single HeartRateRecord that share one uuid, so uuid
    alone is NOT unique per reading — (uuid, time) is."""
    __tablename__ = "heart_rate_samples"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    time: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bpm: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    recording_method: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (Index("ix_hr_athlete_time", "athlete_id", "time"),)


class IntervalSample(Base):
    """One interval reading (steps, distance, calories, sleep stage, ...).

    Deduped by the composite key (uuid, stream, start_time): a SleepSessionRecord
    decomposes into per-stage rows (DEEP/REM/LIGHT/AWAKE + the session itself)
    that ALL share the parent session's uuid, so uuid alone would collapse a
    whole night into one row. (Non-sleep streams have unique uuids anyway, so the
    extra key columns are harmless there.)"""
    __tablename__ = "interval_samples"

    uuid: Mapped[str] = mapped_column(String, primary_key=True)
    stream: Mapped[str] = mapped_column(String, primary_key=True)  # "step", "sleep_deep", ...
    start_time: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    recording_method: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_interval_athlete_stream_start", "athlete_id", "stream", "start_time"),
    )


class Workout(Base):
    __tablename__ = "workouts"

    # source_uuid is the Health Connect record ID and the dedup key: re-uploads
    # of the same workout carry the same UUID and upsert (overwrite) this row.
    source_uuid: Mapped[str] = mapped_column(String, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    source_app: Mapped[str] = mapped_column(String, nullable=False)
    activity_type: Mapped[str] = mapped_column(String, nullable=False)
    recording_method: Mapped[str | None] = mapped_column(String, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    # Health Connect doesn't always provide these.
    total_distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_energy_kcal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Derived from the sliced heart_rate_samples at ingest and stored as columns
    # so the workouts list doesn't have to load raw_payload just to show them.
    avg_heart_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The global streams (HR, steps, distance, ...) sliced to this workout's
    # time window at ingest, so the dashboard can chart per-workout data. The
    # full untrimmed payload lives on the Sync row for re-processing.
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # From the upload envelope.
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    client_version: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_workouts_athlete_start", "athlete_id", "start_time"),
    )


class RouteTrack(Base):
    """A DIY GPS route the app recorded on-device and uploaded (see doc "Route
    tracks"). Deduped by client_route_id; the points[] live in raw_payload,
    which is enough to draw the polyline."""
    __tablename__ = "route_tracks"

    # Client-generated UUID and dedup key: re-uploads of the same track carry
    # the same id and upsert this row (lets the client retry a flaky upload).
    client_route_id: Mapped[str] = mapped_column(String, primary_key=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String, nullable=False)  # "diy_gps" | "health_connect"
    # The parent workout this route belongs to (workouts.source_uuid), when the
    # client knows it. Preferred over time-overlap when attaching the map.
    source_workout_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    distance_meters: Mapped[float | None] = mapped_column(Float, nullable=True)
    point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # The whole track incl. points[] (lat/lng/time/accuracy/altitude/speed).
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_route_tracks_athlete_start", "athlete_id", "start_time"),
    )


class DetectedSession(Base):
    """A workout session inferred from raw HR + step streams (see detection.py).
    Replaced wholesale per athlete on each (re)detection run."""
    __tablename__ = "detected_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    sync_id: Mapped[int] = mapped_column(Integer, nullable=False)  # source sync
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    peak_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_hr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_steps_per_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inferred_activity: Mapped[str | None] = mapped_column(String, nullable=True)

    # Reconciliation with explicit workouts (plain columns, no FK for simplicity).
    matched_workout_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    matched_activity_type: Mapped[str | None] = mapped_column(String, nullable=True)

    # Data-quality flags.
    hr_coverage_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    hr_source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    detection_version: Mapped[str] = mapped_column(String, nullable=False)

    # HR/step/distance streams sliced to this session, for the detail chart.
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_sessions_athlete_start", "athlete_id", "start_time"),
    )
