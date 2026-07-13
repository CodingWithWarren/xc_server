from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, load_only

import config
from auth import (authorize_athlete_access, create_access_token,
                  get_current_athlete, get_or_create_athlete_for_identity,
                  verify_google_id_token)
from database import Base, engine, get_db
from detection import (DETECTION_VERSION, detect_sessions, parse_utc,
                       session_from_route)
from models import (Athlete, DetectedSession, HeartRateSample, IntervalSample,
                    RouteTrack, Sync, Workout)
import schemas

FRONTEND_DIR = Path(__file__).parent / "frontend"
PACIFIC = ZoneInfo("America/Los_Angeles")  # bucket/display in the athlete's zone

# Bound the list endpoints so they can't return an unbounded history as data
# accumulates over a season (newest-first, so the default covers the dashboard).
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 1000


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIST_LIMIT))


def _round_or_none(value: float | None) -> int | None:
    """Round a wire value to the int the DB column stores (None stays None)."""
    return round(value) if value is not None else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once on startup: create any tables that don't exist yet.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


# --- Auth (public routes) ----------------------------------------------------

@app.get("/auth/config")
def auth_config():
    """Public client bootstrap: which sign-in options exist. The web client ID
    is public by design (it's embedded in every Google sign-in button)."""
    return {
        "google_client_id": config.GOOGLE_CLIENT_IDS[0] if config.GOOGLE_CLIENT_IDS else None,
        "dev_mode": config.DEV_MODE,
    }


@app.post("/auth/google", response_model=schemas.TokenResponse)
def auth_google(body: schemas.GoogleLoginRequest, db: Session = Depends(get_db)):
    """Exchange a Google ID token (verified server-side against Google's keys)
    for our own JWT. First sign-in creates the athlete + identity."""
    claims = verify_google_id_token(body.id_token)
    athlete = get_or_create_athlete_for_identity(
        db, provider="google", provider_user_id=claims["sub"],
        email=claims.get("email"), name=claims.get("name"))
    return schemas.TokenResponse(
        access_token=create_access_token(athlete.id), athlete=athlete)


if config.DEV_MODE:
    # Registered ONLY in DEV_MODE — the route does not exist otherwise.
    @app.post("/auth/dev-login", response_model=schemas.TokenResponse)
    def auth_dev_login(body: schemas.DevLoginRequest,
                       db: Session = Depends(get_db)):
        """Mint a JWT for any athlete without Google. Unknown email creates a
        fresh athlete so tests can fabricate users instantly."""
        athlete = None
        if body.athlete_id is not None:
            athlete = db.get(Athlete, body.athlete_id)
            if athlete is None:
                raise HTTPException(status_code=404, detail="No such athlete")
        elif body.email:
            athlete = db.scalar(select(Athlete).where(Athlete.email == body.email))
            if athlete is None:
                athlete = Athlete(name=body.name or body.email,
                                  email=body.email, role="athlete")
                db.add(athlete)
                db.commit()
                db.refresh(athlete)
        else:
            raise HTTPException(status_code=422,
                                detail="Provide athlete_id or email")
        return schemas.TokenResponse(
            access_token=create_access_token(athlete.id), athlete=athlete)


@app.get("/auth/me", response_model=schemas.AthleteOut)
def auth_me(current: Athlete = Depends(get_current_athlete)):
    """Who am I? Used by the dashboard to route athlete vs coach views."""
    return current


# Columns overwritten when a workout with an existing source_uuid is re-uploaded.
_UPSERT_COLUMNS = [
    "athlete_id", "source_app", "activity_type", "recording_method",
    "start_time", "end_time", "duration_seconds",
    "total_distance_meters", "total_energy_kcal", "total_steps",
    "avg_heart_rate", "max_heart_rate",
    "raw_payload", "uploaded_at", "client_version",
]

# Summary columns for the workouts list — lets the query skip the big raw_payload.
_WORKOUT_SUMMARY_COLUMNS = [
    "source_uuid", "athlete_id", "source_app", "activity_type", "recording_method",
    "start_time", "end_time", "duration_seconds", "total_distance_meters",
    "total_energy_kcal", "total_steps", "avg_heart_rate", "max_heart_rate",
    "uploaded_at", "client_version",
]

# Streams sliced onto each workout for the dashboard. NumericSamples are placed
# by their `time`, IntervalSamples by their `start`. Sleep streams are excluded
# (they don't belong to a workout window).
_NUMERIC_STREAMS = [
    "heart_rate_samples", "speed_samples", "hrv_rmssd_samples",
    "resting_heart_rate_samples", "respiratory_rate_samples",
    "blood_oxygen_samples", "skin_temperature_samples", "body_temperature_samples",
]
_INTERVAL_STREAMS = [
    "step_samples", "distance_samples", "total_calorie_samples",
    "active_energy_samples", "basal_energy_samples",
    "flights_climbed_samples", "activity_intensity_samples",
]

# Interval streams fanned into the interval_samples table: payload field -> label.
_INTERVAL_STREAM_FIELDS = {
    "step_samples": "step",
    "distance_samples": "distance",
    "total_calorie_samples": "total_calorie",
    "active_energy_samples": "active_energy",
    "basal_energy_samples": "basal_energy",
    "flights_climbed_samples": "flights_climbed",
    "activity_intensity_samples": "activity_intensity",
    "sleep_sessions": "sleep_session",
    "sleep_deep_samples": "sleep_deep",
    "sleep_rem_samples": "sleep_rem",
    "sleep_light_samples": "sleep_light",
    "sleep_awake_samples": "sleep_awake",
}


def _scope_athlete(current: Athlete, requested: int | None) -> int | None:
    """Resolve which athlete a read endpoint may serve. Athletes are pinned to
    themselves (403 if they ask for someone else); coaches may request any
    athlete, or None for all."""
    if current.role == "coach":
        return requested
    if requested is not None and requested != current.id:
        raise HTTPException(status_code=403,
                            detail="You may only access your own data")
    return current.id


def _store_samples(db: Session, payload: schemas.HealthSync, aid: int) -> None:
    """Fan the raw streams into the typed tables, deduped by their natural key
    (HR by (uuid, time); intervals by uuid). Re-uploads upsert in place, so the
    same sample is stored once no matter how many times it's sent."""

    # Heart rate -> heart_rate_samples (dedup within batch by (uuid, time)).
    hr_rows: dict[tuple, dict] = {}
    for s in payload.heart_rate_samples:
        if s.uuid is None:
            continue  # no key to dedup on (none in real data)
        t = parse_utc(s.time)
        hr_rows[(s.uuid, t)] = {
            "uuid": s.uuid, "time": t, "athlete_id": aid, "bpm": round(s.value),
            "source": s.source, "recording_method": s.recording_method,
        }
    if hr_rows:
        stmt = sqlite_insert(HeartRateSample)
        stmt = stmt.on_conflict_do_update(
            index_elements=["uuid", "time"],
            set_={c: getattr(stmt.excluded, c)
                  for c in ("athlete_id", "bpm", "source", "recording_method")},
            # The key (uuid, time) is global, not athlete-scoped, so without this
            # guard one athlete could overwrite (and re-own) another's sample by
            # reusing its key. Only update a row that already belongs to us.
            where=HeartRateSample.athlete_id == aid,
        )
        db.execute(stmt, list(hr_rows.values()))

    # Interval streams -> interval_samples. Dedup within the batch by
    # (uuid, stream, start_time): sleep stages share the session's uuid, so uuid
    # alone would collapse a night's stages into one row.
    iv_rows: dict[tuple, dict] = {}
    for field, label in _INTERVAL_STREAM_FIELDS.items():
        for s in getattr(payload, field):
            if s.uuid is None:
                continue
            start = parse_utc(s.start)
            iv_rows[(s.uuid, label, start)] = {
                "uuid": s.uuid, "stream": label, "start_time": start,
                "athlete_id": aid, "end_time": parse_utc(s.end),
                "value": s.value, "unit": s.unit, "source": s.source,
                "recording_method": s.recording_method,
            }
    if iv_rows:
        stmt = sqlite_insert(IntervalSample)
        stmt = stmt.on_conflict_do_update(
            index_elements=["uuid", "stream", "start_time"],
            set_={c: getattr(stmt.excluded, c)
                  for c in ("athlete_id", "end_time", "value", "unit",
                            "source", "recording_method")},
            # Global key — guard against cross-athlete overwrite (see HR above).
            where=IntervalSample.athlete_id == aid,
        )
        db.execute(stmt, list(iv_rows.values()))


def _stripped_payload(payload: schemas.HealthSync) -> dict:
    """The sync payload minus the bulk sample streams — keeps the syncs row small
    instead of re-storing them on every overlapping upload. Strips ALL numeric
    streams (HR + speed/HRV/SpO2/etc.) and every interval stream; the ones we
    query live in the typed tables, and per-workout copies are sliced onto each
    Workout.raw_payload at ingest (not read back from here)."""
    data = payload.model_dump(mode="json")
    for key in (*_NUMERIC_STREAMS, *_INTERVAL_STREAM_FIELDS.keys()):
        data.pop(key, None)
    return data


def _slice_streams(payload: schemas.HealthSync, start, end) -> dict:
    """Return the global streams trimmed to [start, end] for one workout."""
    sliced: dict[str, list] = {}
    for name in _NUMERIC_STREAMS:
        sel = [s.model_dump(mode="json") for s in getattr(payload, name)
               if start <= s.time <= end]
        if sel:
            sliced[name] = sel
    for name in _INTERVAL_STREAMS:
        sel = [s.model_dump(mode="json") for s in getattr(payload, name)
               if start <= s.start <= end]
        if sel:
            sliced[name] = sel
    return sliced


def _slice_session_from_tables(db: Session, athlete_id: int, start, end) -> dict:
    """Build a detected session's HR/step/distance slice by querying the typed
    tables for [start, end] — for the session detail chart."""
    sliced: dict[str, list] = {}
    hr = db.execute(
        select(HeartRateSample.time, HeartRateSample.bpm, HeartRateSample.source)
        .where(HeartRateSample.athlete_id == athlete_id,
               HeartRateSample.time >= start, HeartRateSample.time < end)
        .order_by(HeartRateSample.time)).all()
    if hr:
        sliced["heart_rate_samples"] = [
            {"time": t.isoformat(), "value": bpm, "source": src} for t, bpm, src in hr]
    for label in ("step", "distance"):
        rows = db.execute(
            select(IntervalSample.start_time, IntervalSample.end_time,
                   IntervalSample.value, IntervalSample.source)
            .where(IntervalSample.athlete_id == athlete_id,
                   IntervalSample.stream == label,
                   IntervalSample.start_time >= start, IntervalSample.start_time < end)
            .order_by(IntervalSample.start_time)).all()
        if rows:
            sliced[f"{label}_samples"] = [
                {"start": st.isoformat(), "end": et.isoformat(), "value": v, "source": src}
                for st, et, v, src in rows]
    return sliced


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _slice_from_memory(hr_samples, step_samples, distance_samples, start, end) -> dict:
    """Build a detected session's HR/step/distance slice from already-loaded
    in-memory sample lists, instead of re-querying per session. Same shape as
    _slice_session_from_tables; half-open [start, end) window."""
    sliced: dict[str, list] = {}
    hr = sorted((s for s in hr_samples if start <= s["time"] < end),
                key=lambda s: s["time"])
    if hr:
        sliced["heart_rate_samples"] = [
            {"time": s["time"].isoformat(), "value": s["value"], "source": s["source"]}
            for s in hr]
    for label, lst in (("step", step_samples), ("distance", distance_samples)):
        rows = sorted((s for s in lst if start <= s["start"] < end),
                      key=lambda s: s["start"])
        if rows:
            sliced[f"{label}_samples"] = [
                {"start": s["start"].isoformat(), "end": s["end"].isoformat(),
                 "value": s["value"], "source": s["source"]} for s in rows]
    return sliced


def _write_detected_session(db: Session, athlete_id: int, s, workouts, sliced,
                            consumed: set | None = None) -> None:
    """Insert one DetectedSession, matched to the first overlapping workout.

    With `consumed`, matching is one-to-one: a workout already claimed by an
    earlier session isn't matched again, so two detected sessions that overlap
    the same recorded workout don't both suppress-and-recount it."""
    match = next((w for w in workouts
                  if (consumed is None or w.source_uuid not in consumed)
                  and _overlaps(s.start, s.end, w.start_time, w.end_time)), None)
    if match is not None and consumed is not None:
        consumed.add(match.source_uuid)
    db.add(DetectedSession(
        athlete_id=athlete_id,
        sync_id=0,  # detection spans all syncs, not tied to one
        start_time=s.start,
        end_time=s.end,
        duration_seconds=s.duration_seconds,
        peak_hr=s.peak_hr,
        avg_hr=s.avg_hr,
        total_steps=s.total_steps,
        avg_steps_per_min=s.avg_steps_per_min,
        total_distance_meters=s.total_distance_meters,
        inferred_activity=s.inferred_activity,
        matched_workout_uuid=match.source_uuid if match else None,
        matched_activity_type=match.activity_type if match else None,
        hr_coverage_pct=s.hr_coverage_pct,
        hr_source_count=s.hr_source_count,
        detection_version=DETECTION_VERSION,
        raw_payload=sliced,
    ))


def run_detection_for_athlete(db: Session, athlete_id: int) -> int:
    """Detect sessions from the athlete's deduped samples (across all syncs) and
    replace their detected_sessions. Caller commits."""
    hr_samples = [
        {"time": t, "value": bpm, "source": src}
        for t, bpm, src in db.execute(
            select(HeartRateSample.time, HeartRateSample.bpm, HeartRateSample.source)
            .where(HeartRateSample.athlete_id == athlete_id))]
    def interval_stream(stream: str):
        return [
            {"start": st, "end": et, "value": v, "source": src}
            for st, et, v, src in db.execute(
                select(IntervalSample.start_time, IntervalSample.end_time,
                       IntervalSample.value, IntervalSample.source)
                .where(IntervalSample.athlete_id == athlete_id,
                       IntervalSample.stream == stream))]

    step_samples = interval_stream("step")
    distance_samples = interval_stream("distance")
    sessions = detect_sessions(hr_samples, step_samples, distance_samples)

    workouts = db.scalars(
        select(Workout).where(Workout.athlete_id == athlete_id)).all()

    db.execute(delete(DetectedSession)
               .where(DetectedSession.athlete_id == athlete_id))

    consumed: set = set()  # workouts already claimed, so matching is one-to-one

    def write(s):
        # Slice from the already-loaded streams, not a per-session re-query.
        sliced = _slice_from_memory(hr_samples, step_samples, distance_samples,
                                    s.start, s.end)
        _write_detected_session(db, athlete_id, s, workouts, sliced, consumed)

    for s in sessions:
        write(s)

    # DIY GPS routes are explicit, user-started workouts, so each one ALWAYS
    # becomes a session (the HR heuristic skips easy efforts). Skip a route that
    # already overlaps an HR-detected session — that session covers it and the
    # route attaches to it via /sessions/{id}/route.
    routes = db.scalars(
        select(RouteTrack).where(RouteTrack.athlete_id == athlete_id)
        .order_by(RouteTrack.start_time)).all()
    route_windows: list[tuple] = []
    extra = 0
    for r in routes:
        if any(_overlaps(r.start_time, r.end_time, s.start, s.end) for s in sessions):
            continue  # an HR-detected session already covers this route
        if any(_overlaps(r.start_time, r.end_time, a, b) for a, b in route_windows):
            continue  # an earlier route already made a session for this window
        rs = session_from_route(r.start_time, r.end_time, hr_samples,
                                step_samples, distance_meters=r.distance_meters)
        write(rs)
        route_windows.append((r.start_time, r.end_time))
        extra += 1

    return len(sessions) + extra


def _ensure_route_session(db: Session, athlete_id: int, route: RouteTrack) -> None:
    """Ensure a single DIY route has a detected session WITHOUT re-running full
    detection (which would reload the athlete's entire HR history). Idempotent for
    the same route window. Full route-vs-HR reconciliation across all data still
    happens on the next /workouts ingest or POST /detect."""
    start, end = route.start_time, route.end_time
    existing = db.scalars(select(DetectedSession)
                          .where(DetectedSession.athlete_id == athlete_id)).all()
    # Drop this route's own prior session (a re-upload of the same window) so it
    # isn't duplicated; keep the rest to test for coverage below.
    others = []
    for s in existing:
        if s.start_time == start and s.end_time == end:
            db.delete(s)
        else:
            others.append(s)
    # If another session already covers this window (e.g. an HR-detected one), it
    # represents the run; the route attaches to it via /sessions/{id}/route.
    if any(_overlaps(start, end, s.start_time, s.end_time) for s in others):
        return
    # Build the session from just this window — bounded queries, not full history.
    hr_win = [{"time": t, "value": b, "source": src} for t, b, src in db.execute(
        select(HeartRateSample.time, HeartRateSample.bpm, HeartRateSample.source)
        .where(HeartRateSample.athlete_id == athlete_id,
               HeartRateSample.time >= start, HeartRateSample.time < end))]
    step_win = [{"start": st, "value": v, "source": src} for st, v, src in db.execute(
        select(IntervalSample.start_time, IntervalSample.value, IntervalSample.source)
        .where(IntervalSample.athlete_id == athlete_id, IntervalSample.stream == "step",
               IntervalSample.start_time >= start, IntervalSample.start_time < end))]
    rs = session_from_route(start, end, hr_win, step_win,
                            distance_meters=route.distance_meters)
    workouts = db.scalars(select(Workout).where(
        Workout.athlete_id == athlete_id,
        Workout.start_time < end, Workout.end_time > start)).all()
    sliced = _slice_session_from_tables(db, athlete_id, start, end)
    _write_detected_session(db, athlete_id, rs, workouts, sliced)


# Same threshold as the app's display rescue: an untyped (OTHER) workout with a
# GPS route averaging over 3.5 mph shows as RUNNING. Distinct from detection.py's
# RUN_SPEED_MPS (run-vs-walk split for route sessions); this one only rescues
# workouts Fitbit exported without a type.
UNTYPED_RUN_MIN_MPH = 3.5
_MPS_PER_MPH = 0.44704


def _is_untyped_run(distance_meters, duration_seconds) -> bool:
    """Average-speed half of the untyped-run rescue (route presence is checked
    separately). False when distance or duration is missing, like the app."""
    if not distance_meters or not duration_seconds:
        return False
    return distance_meters / duration_seconds / _MPS_PER_MPH > UNTYPED_RUN_MIN_MPH


def _workout_has_route(db: Session, aid: int, source_uuid: str,
                       start, end) -> bool:
    """Does any of the athlete's routes belong to this workout — linked by
    source_workout_uuid, or overlapping its time window (pre-backfill uploads)?"""
    return db.scalar(
        select(RouteTrack.client_route_id)
        .where(RouteTrack.athlete_id == aid,
               or_(RouteTrack.source_workout_uuid == source_uuid,
                   and_(RouteTrack.start_time < end,
                        RouteTrack.end_time > start)))
        .limit(1)) is not None


@app.post("/workouts", status_code=201)
def ingest_sync(payload: schemas.HealthSync,
                current: Athlete = Depends(get_current_athlete),
                db: Session = Depends(get_db)):
    # The athlete comes from the Bearer token — NEVER from the request body —
    # so one athlete cannot upload as another. payload.athlete_id is ignored.
    aid = current.id

    # 1. Record the sync (metadata + workouts only; the bulk streams live,
    #    deduped, in the typed tables instead of being copied here every upload).
    sync = Sync(
        athlete_id=aid,
        # Normalize to naive UTC like the sample tables, so all stored times are
        # comparable regardless of the client's offset (the wire format is UTC).
        uploaded_at=parse_utc(payload.uploaded_at),
        window_start=parse_utc(payload.window_start),
        window_end=parse_utc(payload.window_end),
        client_version=payload.client_version,
        source_platform=payload.source_platform,
        raw_payload=_stripped_payload(payload),
    )
    db.add(sync)

    # 2. Fan the raw streams into the typed tables, deduped by their keys.
    _store_samples(db, payload, aid)

    # 3. Upsert workout summaries, slicing the global streams to each window so
    #    the dashboard/detail view keep working. Dedup within the batch (last
    #    wins) so a single INSERT can't hit the same source_uuid twice.
    rows_by_uuid: dict[str, dict] = {}
    for w in payload.workouts:
        sliced = _slice_streams(payload, w.start_time, w.end_time)
        hr_vals = [s["value"] for s in sliced.get("heart_rate_samples", [])
                   if s.get("value") is not None]
        rows_by_uuid[w.source_uuid] = {
            "source_uuid": w.source_uuid,
            "athlete_id": aid,
            "source_app": w.source_app,
            "activity_type": w.activity_type,
            "recording_method": w.recording_method,
            "start_time": parse_utc(w.start_time),
            "end_time": parse_utc(w.end_time),
            "duration_seconds": w.duration_seconds,
            "total_distance_meters": _round_or_none(w.total_distance_meters),
            "total_energy_kcal": _round_or_none(w.total_energy_kcal),
            "total_steps": _round_or_none(w.total_steps),
            "avg_heart_rate": round(sum(hr_vals) / len(hr_vals)) if hr_vals else None,
            "max_heart_rate": round(max(hr_vals)) if hr_vals else None,
            "raw_payload": sliced,
            "uploaded_at": parse_utc(payload.uploaded_at),
            "client_version": payload.client_version,
        }

    # Fitbit exports auto-detected runs as OTHER. Mirror the app's display rule
    # server-side (xc_training_app lib/main.dart, _HcRun.activityType): an
    # untyped workout with a GPS route averaging over 3.5 mph is a run. The
    # original type stays in the sync row's raw_payload. Detection below reads
    # the upserted rows, so matched sessions inherit the rescued type.
    for row in rows_by_uuid.values():
        if (row["activity_type"] == "OTHER"
                and _is_untyped_run(row["total_distance_meters"],
                                    row["duration_seconds"])
                and _workout_has_route(db, aid, row["source_uuid"],
                                       row["start_time"], row["end_time"])):
            row["activity_type"] = "RUNNING"

    rows = list(rows_by_uuid.values())
    if rows:
        stmt = sqlite_insert(Workout).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Workout.source_uuid],
            set_={col: getattr(stmt.excluded, col) for col in _UPSERT_COLUMNS},
            # source_uuid is a global key; without this an athlete could overwrite
            # and re-own another athlete's workout by reusing its uuid (which is
            # visible to coaches via GET /workouts). Only update our own rows.
            where=Workout.athlete_id == aid,
        )
        db.execute(stmt)

    db.commit()  # sync, samples, and workouts persisted

    # 4. Detect exercise sessions from the deduped streams (incl. untagged ones).
    detected = run_detection_for_athlete(db, aid)
    db.commit()

    return {
        "athlete_id": aid,
        "received_workouts": len(rows),
        "received_hr_samples": len(payload.heart_rate_samples),
        "received_step_samples": len(payload.step_samples),
        "detected_sessions": detected,
    }


@app.get("/workouts", response_model=list[schemas.WorkoutSummary])
def list_workouts(athlete_id: int | None = None, limit: int = DEFAULT_LIST_LIMIT,
                  current: Athlete = Depends(get_current_athlete),
                  db: Session = Depends(get_db)):
    athlete_id = _scope_athlete(current, athlete_id)
    # load_only the summary columns so the list doesn't deserialize each
    # workout's raw_payload (avg/max HR are columns now, not derived from it).
    query = (
        select(Workout)
        .options(load_only(*(getattr(Workout, c) for c in _WORKOUT_SUMMARY_COLUMNS)))
        .order_by(Workout.start_time.desc())
    )
    if athlete_id is not None:
        query = query.where(Workout.athlete_id == athlete_id)
    return db.scalars(query.limit(_clamp_limit(limit))).all()


def _pacific_date(dt) -> date:
    """Stored times are naive UTC; bucket by the Pacific date so weeks line up
    with the times shown on the dashboard."""
    return dt.replace(tzinfo=timezone.utc).astimezone(PACIFIC).date()


def _activity_entries(db: Session, athlete_id: int | None):
    """The athlete's activity: detected sessions plus recorded workouts that
    have no matching session (e.g. manual entries with no samples). Returns
    (start_time, duration_seconds, distance_meters, activity) tuples."""
    sq = select(DetectedSession.start_time, DetectedSession.duration_seconds,
                DetectedSession.total_distance_meters,
                DetectedSession.matched_activity_type,
                DetectedSession.inferred_activity,
                DetectedSession.matched_workout_uuid)
    wq = select(Workout.source_uuid, Workout.start_time, Workout.duration_seconds,
                Workout.total_distance_meters, Workout.activity_type)
    if athlete_id is not None:
        sq = sq.where(DetectedSession.athlete_id == athlete_id)
        wq = wq.where(Workout.athlete_id == athlete_id)

    workouts = {uuid: (start, dur, dist, act)
                for uuid, start, dur, dist, act in db.execute(wq)}

    entries, matched = [], set()
    for start, dur, dist, m_act, i_act, m_uuid in db.execute(sq):
        if m_uuid in workouts:
            _, w_dur, w_dist, _ = workouts[m_uuid]
            # The recorded workout carries the real distance/duration; fall back
            # to it when the detected session lacks one (e.g. its distance source
            # was dropped by dedup) so a recorded run isn't counted as 0 km.
            dist = dist or w_dist
            dur = dur or w_dur
            matched.add(m_uuid)
        entries.append((start, dur, dist, m_act or i_act))
    for uuid, (start, dur, dist, act) in workouts.items():
        if uuid not in matched:
            entries.append((start, dur, dist, act))
    return entries


@app.get("/stats/weekly", response_model=list[schemas.WeeklyDistance])
def weekly_distance(athlete_id: int | None = None,
                    current: Athlete = Depends(get_current_athlete),
                    db: Session = Depends(get_db)):
    athlete_id = _scope_athlete(current, athlete_id)
    totals: dict[date, float] = defaultdict(float)
    for start_time, _dur, distance, _act in _activity_entries(db, athlete_id):
        d = _pacific_date(start_time)
        monday = d - timedelta(days=d.weekday())  # ISO week start (Monday)
        totals[monday] += distance or 0

    return [
        schemas.WeeklyDistance(week_start=wk, total_distance_meters=totals[wk])
        for wk in sorted(totals)
    ]


@app.get("/stats/summary", response_model=schemas.WeeklySummary)
def weekly_summary(athlete_id: int | None = None,
                   current: Athlete = Depends(get_current_athlete),
                   db: Session = Depends(get_db)):
    """This week vs the athlete's own previous week (Pacific weeks, Monday
    start). Comparisons are always within one athlete's history."""
    athlete_id = _scope_athlete(current, athlete_id)
    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)

    buckets = {
        this_monday: {"distance": 0.0, "duration": 0, "runs": 0, "sessions": 0},
        last_monday: {"distance": 0.0, "duration": 0, "runs": 0, "sessions": 0},
    }
    for start_time, dur, dist, act in _activity_entries(db, athlete_id):
        d = _pacific_date(start_time)
        monday = d - timedelta(days=d.weekday())
        b = buckets.get(monday)
        if b is None:
            continue
        b["distance"] += dist or 0
        b["duration"] += dur or 0
        b["sessions"] += 1
        if (act or "").startswith("RUNNING"):
            b["runs"] += 1

    def week_stats(monday: date) -> schemas.WeekStats:
        b = buckets[monday]
        return schemas.WeekStats(
            week_start=monday,
            total_distance_meters=b["distance"],
            total_duration_seconds=b["duration"],
            run_count=b["runs"],
            session_count=b["sessions"],
        )

    return schemas.WeeklySummary(
        this_week=week_stats(this_monday), last_week=week_stats(last_monday))


@app.get("/workouts/{source_uuid}", response_model=schemas.WorkoutDetail)
def get_workout(source_uuid: str,
                current: Athlete = Depends(get_current_athlete),
                db: Session = Depends(get_db)):
    workout = db.get(Workout, source_uuid)
    if workout is None:
        raise HTTPException(status_code=404, detail="Workout not found")
    authorize_athlete_access(current, workout.athlete_id)
    return workout


@app.post("/detect")
def redetect(athlete_id: int | None = None,
             current: Athlete = Depends(get_current_athlete),
             db: Session = Depends(get_db)):
    """Re-run detection against already-stored samples (no client re-upload).
    Athletes reprocess themselves; coaches may pass any athlete_id (or none
    for everyone)."""
    athlete_id = _scope_athlete(current, athlete_id)
    if athlete_id is not None:
        ids = [athlete_id]  # run unconditionally — a route-only athlete has no HR rows
    else:
        # Sweep everyone with data: union HR + route athletes (a route-only
        # athlete has detected sessions but no heart_rate_samples).
        ids = sorted(
            {a for (a,) in db.execute(select(HeartRateSample.athlete_id).distinct())}
            | {a for (a,) in db.execute(select(RouteTrack.athlete_id).distinct())})

    results: dict[int, int] = {}
    for aid in ids:
        results[aid] = run_detection_for_athlete(db, aid)
    db.commit()
    return {"detection_version": DETECTION_VERSION, "detected_per_athlete": results}


@app.get("/sessions", response_model=list[schemas.SessionSummary])
def list_sessions(athlete_id: int | None = None, limit: int = DEFAULT_LIST_LIMIT,
                  current: Athlete = Depends(get_current_athlete),
                  db: Session = Depends(get_db)):
    athlete_id = _scope_athlete(current, athlete_id)
    query = select(DetectedSession).order_by(DetectedSession.start_time.desc())
    if athlete_id is not None:
        query = query.where(DetectedSession.athlete_id == athlete_id)
    return db.scalars(query.limit(_clamp_limit(limit))).all()


@app.get("/sessions/{session_id}", response_model=schemas.SessionDetail)
def get_session(session_id: int,
                current: Athlete = Depends(get_current_athlete),
                db: Session = Depends(get_db)):
    session = db.get(DetectedSession, session_id)
    # 404 (not 403) for a foreign session too: sessions use a sequential integer
    # id, so distinct 403/404 would let a non-coach enumerate others' sessions.
    if session is None or (current.role != "coach" and session.athlete_id != current.id):
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/team/week")
def team_week(start: str | None = None,
              current: Athlete = Depends(get_current_athlete),
              db: Session = Depends(get_db)):
    """Coach board: per athlete, which days of one Pacific week they trained,
    with week totals and sync recency. Day states distinguish "no activity"
    (the phone has reported since) from "nosync" (it hasn't — unknown, not
    rested). `start` is any date in the wanted week; defaults to this week."""
    if current.role != "coach":
        raise HTTPException(status_code=403, detail="Coaches only")

    today = datetime.now(timezone.utc).astimezone(PACIFIC).date()
    anchor = date.fromisoformat(start) if start else today
    monday = anchor - timedelta(days=anchor.weekday())
    # Nothing exists before the season; clamp navigation to its first week.
    season = SEASON_START.date()
    season_monday = season - timedelta(days=season.weekday())
    monday = max(monday, season_monday)
    days = [monday + timedelta(days=i) for i in range(7)]
    prev_monday = monday - timedelta(days=7)

    def day_start_utc(d: date) -> datetime:
        return (datetime(d.year, d.month, d.day, tzinfo=PACIFIC)
                .astimezone(timezone.utc).replace(tzinfo=None))

    last_syncs = dict(db.execute(
        select(Sync.athlete_id, func.max(Sync.uploaded_at))
        .group_by(Sync.athlete_id)).all())
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    athletes_out, trained_today, week_miles, prev_week_miles, stale = [], 0, 0.0, 0.0, []
    for a in db.scalars(select(Athlete).order_by(Athlete.name)).all():
        per_day = {d: {"runs": 0, "miles": 0.0, "minutes": 0} for d in days}
        for start_time, dur, dist, _act in _activity_entries(db, a.id):
            d = _pacific_date(start_time)
            if d in per_day:
                per_day[d]["runs"] += 1
                per_day[d]["miles"] += (dist or 0) / 1609.344
                per_day[d]["minutes"] += round((dur or 0) / 60)
            elif prev_monday <= d < monday:
                prev_week_miles += (dist or 0) / 1609.344

        last_sync = last_syncs.get(a.id)
        day_states = []
        for d in days:
            cell = per_day[d]
            if cell["runs"]:
                day_states.append({"state": "trained",
                                   "runs": cell["runs"],
                                   "miles": round(cell["miles"], 1),
                                   "minutes": cell["minutes"]})
            elif d > today:
                day_states.append({"state": "future"})
            else:
                # A past day is only a true rest day if the phone has reported
                # since that day ended (today: since it began).
                covered_from = day_start_utc(d if d == today else d + timedelta(days=1))
                known = last_sync is not None and last_sync >= covered_from
                day_states.append({"state": "none" if known else "nosync"})

        runs = sum(c["runs"] for c in per_day.values())
        miles = sum(c["miles"] for c in per_day.values())
        week_miles += miles
        if today in per_day and per_day[today]["runs"]:
            trained_today += 1
        is_stale = last_sync is None or (now_utc - last_sync) > timedelta(hours=48)
        if is_stale:
            stale.append(a.name)
        athletes_out.append({
            "id": a.id, "name": a.name, "role": a.role,
            "days": day_states,
            "week_runs": runs, "week_miles": round(miles, 1),
            "last_sync": last_sync.isoformat() + "Z" if last_sync else None,
            "stale": is_stale,
        })

    return {
        "week_start": monday.isoformat(),
        "season_week_start": season_monday.isoformat(),
        "days": [d.isoformat() for d in days],
        "today": today.isoformat(),
        "athletes": athletes_out,
        "team": {
            "total": len(athletes_out),
            "trained_today": trained_today,
            "week_miles": round(week_miles, 1),
            "prev_week_miles": round(prev_week_miles, 1),
            "stale": stale,
        },
    }


@app.get("/athletes", response_model=list[schemas.AthleteOut])
def list_athletes(current: Athlete = Depends(get_current_athlete),
                  db: Session = Depends(get_db)):
    """Coach-only roster, used by the dashboard's coach view."""
    if current.role != "coach":
        raise HTTPException(status_code=403, detail="Coaches only")
    return db.scalars(select(Athlete).order_by(Athlete.name)).all()


# --- Route tracks (DIY GPS recording — see docs/SERVER_SCHEMA.md) -------------

@app.post("/routes", status_code=201)
def ingest_route(payload: schemas.RouteTrack,
                 current: Athlete = Depends(get_current_athlete),
                 db: Session = Depends(get_db)):
    """Store one DIY GPS track. The athlete comes from the Bearer token — never
    the body. Upsert by client_route_id so a retried upload doesn't duplicate."""
    aid = current.id
    # The client doesn't always send client_route_id yet; fall back to a stable
    # key derived from (athlete_id, start_time) so re-uploads still dedup.
    crid = payload.client_route_id or f"diy:{aid}:{payload.start_time.isoformat()}"
    row = {
        "client_route_id": crid,
        "athlete_id": aid,
        "source": payload.source,
        "source_workout_uuid": payload.source_workout_uuid,
        "start_time": parse_utc(payload.start_time),
        "end_time": parse_utc(payload.end_time),
        "duration_seconds": payload.duration_seconds,
        "distance_meters": payload.distance_meters,
        "point_count": payload.point_count,
        "uploaded_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "raw_payload": payload.model_dump(mode="json"),
    }
    stmt = sqlite_insert(RouteTrack).values(row)
    stmt = stmt.on_conflict_do_update(
        index_elements=[RouteTrack.client_route_id],
        set_={c: row[c] for c in (
            "athlete_id", "source", "source_workout_uuid", "start_time",
            "end_time", "duration_seconds", "distance_meters", "point_count",
            "uploaded_at", "raw_payload")},
        # client_route_id is client-supplied and a global key, so without this an
        # athlete could overwrite another's route by reusing its id. Only update
        # a row we already own; a conflicting foreign row is left untouched.
        where=RouteTrack.athlete_id == aid,
    )
    db.execute(stmt)
    db.commit()

    # A DIY route always becomes a session so it's visible with its map. Do it
    # incrementally for just this route's window — re-running full detection here
    # would reload the athlete's entire HR history on every upload. (db.get may
    # return a row owned by someone else if a foreign-key collision was blocked
    # by the upsert guard above; only build a session for our own row.)
    route = db.get(RouteTrack, crid)
    if route is not None and route.athlete_id == aid:
        # The route often lands after its workout: re-run the untyped-run rescue
        # for OTHER workouts this route belongs to, before the session is built,
        # so the new session inherits the rescued type.
        for w in db.scalars(select(Workout).where(
                Workout.athlete_id == aid, Workout.activity_type == "OTHER",
                or_(Workout.source_uuid == route.source_workout_uuid,
                    and_(Workout.start_time < route.end_time,
                         Workout.end_time > route.start_time)))):
            if _is_untyped_run(w.total_distance_meters, w.duration_seconds):
                w.activity_type = "RUNNING"
                # Sessions already matched to this workout carry a stale type.
                db.execute(update(DetectedSession)
                           .where(DetectedSession.athlete_id == aid,
                                  DetectedSession.matched_workout_uuid == w.source_uuid)
                           .values(matched_activity_type="RUNNING"))
        _ensure_route_session(db, aid, route)
        db.commit()
    return {"client_route_id": crid, "received_points": len(payload.points)}


@app.get("/routes", response_model=list[schemas.RouteSummary])
def list_routes(athlete_id: int | None = None, limit: int = DEFAULT_LIST_LIMIT,
                current: Athlete = Depends(get_current_athlete),
                db: Session = Depends(get_db)):
    athlete_id = _scope_athlete(current, athlete_id)
    # load_only the summary columns so the list skips the big points payload.
    cols = ("client_route_id", "athlete_id", "source", "start_time", "end_time",
            "duration_seconds", "distance_meters", "point_count", "uploaded_at")
    query = (select(RouteTrack)
             .options(load_only(*(getattr(RouteTrack, c) for c in cols)))
             .order_by(RouteTrack.start_time.desc()))
    if athlete_id is not None:
        query = query.where(RouteTrack.athlete_id == athlete_id)
    return db.scalars(query.limit(_clamp_limit(limit))).all()


def _route_to_detail(route: RouteTrack) -> schemas.RouteDetail:
    """A stored route + its points (pulled from raw_payload) for the map."""
    return schemas.RouteDetail(
        client_route_id=route.client_route_id, athlete_id=route.athlete_id,
        source=route.source, start_time=route.start_time, end_time=route.end_time,
        duration_seconds=route.duration_seconds,
        distance_meters=route.distance_meters, point_count=route.point_count,
        uploaded_at=route.uploaded_at,
        points=route.raw_payload.get("points", []))


@app.get("/routes/{client_route_id}", response_model=schemas.RouteDetail)
def get_route(client_route_id: str,
              current: Athlete = Depends(get_current_athlete),
              db: Session = Depends(get_db)):
    route = db.get(RouteTrack, client_route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="Route not found")
    authorize_athlete_access(current, route.athlete_id)
    return _route_to_detail(route)


@app.get("/sessions/{session_id}/route", response_model=schemas.RouteDetail | None)
def get_session_route(session_id: int,
                      current: Athlete = Depends(get_current_athlete),
                      db: Session = Depends(get_db)):
    """The DIY route whose time window overlaps this detected session (the GPS
    path for the session detail map), or null if none. Reconciled at read time."""
    session = db.get(DetectedSession, session_id)
    if session is None or (current.role != "coach" and session.athlete_id != current.id):
        raise HTTPException(status_code=404, detail="Session not found")
    # Prefer the explicit link: a route that names this session's workout as its
    # parent (source_workout_uuid). Time overlap below is only the fallback for
    # routes uploaded without it (e.g. early iOS uploads, DIY recordings).
    if session.matched_workout_uuid:
        linked = db.scalar(
            select(RouteTrack)
            .where(RouteTrack.athlete_id == session.athlete_id,
                   RouteTrack.source_workout_uuid == session.matched_workout_uuid))
        if linked is not None:
            return _route_to_detail(linked)
    # Find overlapping routes by window only (no raw_payload), pick the one with
    # the most temporal overlap, then load just that one row's GPS points — so we
    # neither scan every route's payload nor return an arbitrary overlap.
    candidates = db.execute(
        select(RouteTrack.client_route_id, RouteTrack.start_time, RouteTrack.end_time)
        .where(RouteTrack.athlete_id == session.athlete_id,
               RouteTrack.start_time < session.end_time,
               RouteTrack.end_time > session.start_time)).all()
    if not candidates:
        return None
    best = max(candidates, key=lambda c:
               min(session.end_time, c.end_time) - max(session.start_time, c.start_time))
    return _route_to_detail(db.get(RouteTrack, best.client_route_id))


# --- Sync watermark (see docs/SERVER_SCHEMA.md "Sync watermark") --------------

# Where a first sync (or a post-DELETE /me/data re-sync) starts uploading from:
# the beginning of summer training. Served as the watermark when an athlete has
# no data, so the client backfills the whole season instead of just 24 hours.
SEASON_START = datetime(2026, 7, 6)


@app.get("/me/last-sample-time")
def last_sample_time(current: Athlete = Depends(get_current_athlete),
                     db: Session = Depends(get_db)):
    """Newest sample timestamp stored for the authenticated athlete, across every
    ingested table (workouts, heart rate, interval samples). The client fetches
    this before a sync and re-queries from it, so a reinstall or second device
    resumes where the athlete's data actually ends. When we hold no data (first
    sync, or right after DELETE /me/data) it's SEASON_START, so the client
    uploads the whole season. Otherwise it's the max of *end* timestamps
    (workout/interval end_time, HR sample time) — the same quantity the client
    used to track locally."""
    newest = max(
        (t for t in (
            db.scalar(select(func.max(Workout.end_time))
                      .where(Workout.athlete_id == current.id)),
            db.scalar(select(func.max(HeartRateSample.time))
                      .where(HeartRateSample.athlete_id == current.id)),
            db.scalar(select(func.max(IntervalSample.end_time))
                      .where(IntervalSample.athlete_id == current.id)),
        ) if t is not None),
        default=SEASON_START)
    # Stored times are naive UTC; stamp Z so the mobile client doesn't misread
    # a zoneless timestamp as local time.
    return {"last_sample_time": newest.isoformat() + "Z"}


# --- Data reset (dev convenience — see docs/SERVER_SCHEMA.md "Data reset") ----

# Every athlete_id-keyed table, wiped wholesale by DELETE /me/data. No
# cross-table FKs, so order is cosmetic (derived rows first for readability).
_ATHLETE_DATA_MODELS = [
    DetectedSession, Workout, RouteTrack,
    HeartRateSample, IntervalSample, Sync,
]


@app.delete("/me/data")
def delete_my_data(current: Athlete = Depends(get_current_athlete),
                   db: Session = Depends(get_db)):
    """Delete everything the authenticated athlete has uploaded: raw sample
    streams, workouts, detected sessions, route tracks, and sync rows. Scoped
    to the token's athlete only — it can never touch another athlete's rows.
    Backs the app's debug "Reset (wipe server + start over)" button. It's a dev
    convenience; consider gating on DEV_MODE before any real deployment."""
    deleted: dict[str, int] = {}
    for model in _ATHLETE_DATA_MODELS:
        result = db.execute(delete(model).where(model.athlete_id == current.id))
        deleted[model.__tablename__] = result.rowcount
    db.commit()
    return {"deleted": deleted}


class NoCacheStaticFiles(StaticFiles):
    """Serve static files with Cache-Control: no-cache so browsers always
    revalidate (via ETag) instead of silently serving a stale cached copy —
    important while the dashboard JS/CSS is changing."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Serve the dashboard. Mounted LAST so all API routes above take precedence;
# "/" returns frontend/index.html (html=True).
app.mount("/", NoCacheStaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
