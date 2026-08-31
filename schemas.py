from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# --- Incoming payload (mirrors docs/SERVER_SCHEMA.md: "health_sync") ----------

class NumericSample(BaseModel):
    """A point-in-time reading (heart rate, speed, SpO2, ...)."""
    uuid: str | None = None
    time: datetime
    value: float
    unit: str
    source: str | None = None
    recording_method: str | None = None


class IntervalSample(BaseModel):
    """A value measured over a span (steps, distance, calories, sleep stages)."""
    uuid: str | None = None
    start: datetime
    end: datetime
    value: float
    unit: str
    source: str | None = None
    recording_method: str | None = None


class Workout(BaseModel):
    """An explicit ExerciseSessionRecord the recording app wrote (summary only;
    raw samples now arrive as top-level streams, not nested here)."""
    source_uuid: str
    source_app: str
    source_device_id: str | None = None
    activity_type: str
    recording_method: str | None = None
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    # Accept floats on the wire: Health Connect distances/energies are often
    # fractional, and Pydantic would otherwise 422 the WHOLE sync over one
    # fractional value. Stored rounded to int at ingest (see _round_or_none).
    total_distance_meters: float | None = None
    total_energy_kcal: float | None = None
    total_steps: float | None = None


class TrackPoint(BaseModel):
    """One GPS fix from a DIY route recording (see doc "Route tracks")."""
    lat: float
    lng: float
    time: datetime
    accuracy_m: float | None = None
    altitude_m: float | None = None
    speed_mps: float | None = None


class RouteTrack(BaseModel):
    """A full per-point GPS track the app recorded on-device and POSTs to
    /routes — separate from the health sync. Deduped by client_route_id."""
    # Unknown `type` values are rejected with 422 (see doc "Versioning").
    type: Literal["route_track"]
    # The client doesn't send this yet; until it does, the server synthesizes a
    # stable key from (athlete_id, start_time). See doc "Route tracks > Dedup".
    client_route_id: str | None = None
    source: str           # "diy_gps" | "health_connect"
    # health_connect routes: the parent workout's uuid (joins workouts.source_uuid).
    # Often null on early iOS uploads; the client re-sends the track to backfill
    # it, which the upsert in POST /routes absorbs.
    source_workout_uuid: str | None = None
    client_version: str | None = None
    recorded_at: datetime
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    distance_meters: float | None = None
    point_count: int
    points: list[TrackPoint] = []


class HealthSync(BaseModel):
    # Unknown `type` values are rejected with 422 (see doc "Versioning").
    type: Literal["health_sync"]
    # DEPRECATED: ignored. The athlete is derived from the Bearer token so one
    # athlete can never upload as another. Kept so older clients still parse.
    athlete_id: int | None = None
    client_version: str | None = None
    uploaded_at: datetime
    source_platform: str
    window_start: datetime
    window_end: datetime

    workouts: list[Workout] = []

    # Point-in-time streams.
    heart_rate_samples: list[NumericSample] = []
    speed_samples: list[NumericSample] = []
    hrv_rmssd_samples: list[NumericSample] = []
    resting_heart_rate_samples: list[NumericSample] = []
    respiratory_rate_samples: list[NumericSample] = []
    blood_oxygen_samples: list[NumericSample] = []
    skin_temperature_samples: list[NumericSample] = []
    body_temperature_samples: list[NumericSample] = []

    # Interval streams.
    step_samples: list[IntervalSample] = []
    distance_samples: list[IntervalSample] = []
    total_calorie_samples: list[IntervalSample] = []
    active_energy_samples: list[IntervalSample] = []
    basal_energy_samples: list[IntervalSample] = []
    flights_climbed_samples: list[IntervalSample] = []
    activity_intensity_samples: list[IntervalSample] = []

    # Sleep streams.
    sleep_sessions: list[IntervalSample] = []
    sleep_deep_samples: list[IntervalSample] = []
    sleep_rem_samples: list[IntervalSample] = []
    sleep_light_samples: list[IntervalSample] = []
    sleep_awake_samples: list[IntervalSample] = []


# --- Auth ----------------------------------------------------------------------

class GoogleLoginRequest(BaseModel):
    id_token: str


class DevLoginRequest(BaseModel):
    """DEV_MODE only: identify an athlete by id or email (creates one for an
    unknown email so tests can mint fake athletes instantly)."""
    athlete_id: int | None = None
    email: str | None = None
    name: str | None = None


class AthleteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str | None
    role: str
    grade: int | None
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    athlete: AthleteOut


# --- Outgoing responses --------------------------------------------------------

class WorkoutSummary(BaseModel):
    """A stored workout without the (potentially huge) raw sample arrays."""
    model_config = ConfigDict(from_attributes=True)

    source_uuid: str
    athlete_id: int
    source_app: str
    activity_type: str
    recording_method: str | None
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    total_distance_meters: int | None
    total_energy_kcal: int | None
    total_steps: int | None
    avg_heart_rate: int | None  # derived from sliced heart_rate_samples
    max_heart_rate: int | None  # derived from sliced heart_rate_samples
    uploaded_at: datetime
    client_version: str | None


class WorkoutDetail(WorkoutSummary):
    """A single workout including the streams sliced to its time window."""
    raw_payload: dict[str, Any]


class WeeklyDistance(BaseModel):
    """Total distance summed per ISO week (Monday), for the dashboard chart."""
    week_start: date  # the Monday of that week
    total_distance_meters: float


class WeekStats(BaseModel):
    """One week's training totals for an athlete (Pacific week, Monday start)."""
    week_start: date
    total_distance_meters: float
    total_duration_seconds: int
    run_count: int
    session_count: int


class WeeklySummary(BaseModel):
    """This week vs the athlete's own previous week, for the dashboard hero."""
    this_week: WeekStats
    last_week: WeekStats


class SessionSummary(BaseModel):
    """A detected exercise session (output of the detection algorithm)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    athlete_id: int
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    peak_hr: int | None
    avg_hr: int | None
    total_steps: int | None
    avg_steps_per_min: float | None
    total_distance_meters: int | None
    inferred_activity: str | None
    matched_workout_uuid: str | None
    matched_activity_type: str | None
    hr_coverage_pct: float | None
    hr_source_count: int | None
    detection_version: str


class SessionDetail(SessionSummary):
    """A detected session including its sliced streams (for the detail chart)."""
    raw_payload: dict[str, Any]


# --- Coach email digest --------------------------------------------------------
# The mobile client is already shipped against this shape — field names and
# nullability are a fixed contract (see CLAUDE.md "Coach email digest").
# Timestamps are `str`, not `datetime`, on purpose: coach_digest._iso_z has
# already rendered them as ISO-8601 with an explicit Z, and re-serializing a
# datetime here would drop the Z and make the client read it as local time.

class CoachDigestOut(BaseModel):
    headline: str
    bullets: list[str] = []
    actions: list[str] = []
    generated_at: str  # when the SERVER produced the summary, ISO-8601 UTC
    source_count: int


class CoachMessageOut(BaseModel):
    id: str            # RFC Message-ID
    from_: str | None = Field(default=None, alias="from")
    from_name: str | None = None
    subject: str | None = None
    date: str | None = None
    body: str          # plain text — the client renders it verbatim

    model_config = ConfigDict(populate_by_name=True)


class CoachDigestResponse(BaseModel):
    """Both /coach-digest endpoints return this. `digest` is null and `messages`
    empty in the legitimate "nothing yet" state; the app renders "No coach email
    yet". Absence of the feature is a 501, never an empty 200."""
    digest: CoachDigestOut | None = None
    messages: list[CoachMessageOut] = []


# --- Route tracks --------------------------------------------------------------

class RouteSummary(BaseModel):
    """A stored route without the (potentially large) points array."""
    model_config = ConfigDict(from_attributes=True)

    client_route_id: str
    athlete_id: int
    source: str
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    distance_meters: float | None
    point_count: int
    uploaded_at: datetime


class RouteDetail(RouteSummary):
    """A single route including its GPS points (the polyline to draw)."""
    points: list[TrackPoint]
