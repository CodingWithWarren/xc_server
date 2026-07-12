# CLAUDE.md — Chadwick XC Training App Server

Guidance for working in this repo. See `README.md` for setup/run and
`docs/SERVER_SCHEMA.md` for the authoritative upload wire format.

## What this is

FastAPI + SQLAlchemy + SQLite backend for the Chadwick XC Training mobile app. The
app uploads raw Health Connect data; this server stores it and serves a small
dashboard. Beginner-owned project — favor simple, explained code over cleverness.

## Architecture

- `main.py` — endpoints + `StaticFiles` mount for the dashboard (mounted **last**
  at `/` so API routes take precedence; `GET /` serves `frontend/index.html`).
- `database.py` — engine (`xc_training.db`), `Base`, `get_db` session dependency.
- `config.py` — `.env` loading (JWT secret, DEV_MODE, Google client IDs).
- `auth.py` — JWT issue/verify, `get_current_athlete` dependency, Google ID
  token verification, identity get-or-create.
- `models.py` — `Athlete` + `AuthIdentity` (identity), `Sync` (upload metadata),
  `HeartRateSample` + `IntervalSample` (deduped raw streams), `Workout`
  (explicit sessions), `DetectedSession`.
- `schemas.py` — `HealthSync` (incoming) + auth/token + response models.
- `frontend/` — vanilla HTML/JS/CSS, Chart.js from CDN, **no build tools**.
  `auth.js` is shared by all pages (token in localStorage + Bearer fetches).
- `scripts/link_legacy_data.py` — move pre-auth data onto a real athlete.

## Conventions

- **Validate with Pydantic; mirror `docs/SERVER_SCHEMA.md`.** The incoming
  `type` is `Literal["health_sync"]` — unknown discriminators must 422. If the
  schema changes incompatibly, the doc bumps the discriminator (e.g. `_v2`).
- **The dashboard is an API client.** It must get data only by fetching the JSON
  endpoints — never embed server-side DB access in the frontend.
- **Derived workout HR is computed at ingest and stored as columns.**
  `Workout.avg_heart_rate`/`max_heart_rate` are filled from the sliced
  `heart_rate_samples` when the workout is upserted, so `list_workouts`
  (`load_only` the summary columns) never deserializes `raw_payload`. Keep that
  pattern for new derived values — compute once at write, don't recompute per
  read from the JSON blob.
- **Upserts use the SQLite dialect** `insert(...).on_conflict_do_update(...)`.
  Dedup a batch in Python first (last-wins) — SQLite rejects the same conflict key
  twice in one statement. Dedup keys: workouts `source_uuid`; HR `(uuid, time)`;
  interval samples `(uuid, stream, start_time)` — sleep stages share the
  session's uuid, so uuid alone collapses a night's stages into one row.
- **Ingest model:** `POST /workouts` fans the raw streams into the deduped typed
  tables (`_store_samples`), records a small `syncs` metadata row (bulk streams
  stripped via `_stripped_payload`), upserts each workout with streams **sliced to
  its `[start,end]` window** (`_slice_streams`), then runs detection. The client
  re-uploads the full 30-day window each time, so dedup keeps samples stored once.

## Auth

**Identity model (provider-agnostic by design).** `athletes` is the domain
object; sign-in credentials live in `auth_identities` keyed by
`(provider, provider_user_id)` (unique). Google is the only provider today;
**Sign in with Apple is coming** — Apple requires it once the iOS app ships
with Google sign-in — and the same athlete will link both identities. Never
put a `google_id` (or any provider field) on `athletes`.

**Flow.** Client gets a provider ID token (Google Identity Services), POSTs it
to `/auth/google`; the server verifies it with google-auth (never hand-rolled)
against `GOOGLE_CLIENT_IDS`, finds-or-creates the athlete, and returns **our
own JWT**. Everything after uses `Authorization: Bearer <our-jwt>`; provider
tokens are never passed around again.

**athlete_id comes from the token, never the body.** `POST /workouts` ignores
the payload's deprecated `athlete_id`. Read endpoints take an optional
`?athlete_id=` that athletes may only use for themselves (403 otherwise);
coaches (`athletes.role = "coach"`) may read anyone. Promote a coach manually:
`sqlite3 xc_training.db "UPDATE athletes SET role='coach' WHERE email='...'"`.

**.env contract** (gitignored; `.env.example` is the template): `JWT_SECRET`
(64 hex — regenerating it logs everyone out; it must NOT be generated at
startup or sessions die on restart), `DEV_MODE`, `GOOGLE_CLIENT_IDS`
(comma-separated allow-list of the project's OAuth client IDs — web, Android,
and iOS; each platform's Google SDK stamps a different `aud`, so all must be
accepted. Web stays first: `/auth/config` serves `[0]` to the dashboard).

**DEV_MODE.** When true, `POST /auth/dev-login` exists (`{athlete_id}` or
`{email}` — unknown email creates a fresh athlete) and the dashboard shows a
dev-login control. When false the route is **not registered at all**. Tokens
are 30-day for dev convenience; shorten + add refresh tokens before team
rollout. Public routes: `/`, static files, `/health`, `/auth/*`, `/docs`.

**Legacy data:** pre-auth uploads sat under `athlete_id=1`; re-attach with
`venv/bin/python scripts/link_legacy_data.py --from-id 1 --to-email you@...`.

## Gotchas (learned the hard way)

- **Schema changes need a DB reset.** `Base.metadata.create_all` only creates
  missing tables; it never alters existing ones. After changing a model, delete
  `xc_training.db` and restart (no migrations yet; re-sync from the phone). Call
  this out to the user before doing it — they may have real data.
- **Never `pkill -f uvicorn` from a tool call.** The pattern matches the calling
  shell's own command line and kills it mid-run. Stop the server by PID instead:
  `kill $(ss -ltnp | grep ':8000' | grep -oP 'pid=\K[0-9]+')`.
- **Phone → server over WSL2 needs a port-proxy.** WSL2 is a NAT'd VM; binding
  uvicorn to `0.0.0.0` is necessary but not sufficient. Windows only forwards
  *localhost* into WSL, not the LAN IP. A physical phone must hit the PC's Wi-Fi
  IP, which requires a Windows `netsh interface portproxy` (LAN IP:8000 → WSL
  IP:8000) + firewall rule. The WSL IP changes on reboot and breaks it. `10.0.2.2`
  is emulator-only. (See the `phone-to-wsl-networking` memory.)
- **Uploads are large** (~15 MB, ~160k HR samples per 30-day sync) but fan out to
  the typed tables deduped, so re-uploads don't grow storage. The 164k upsert
  takes ~2s. Most samples fall outside any explicit workout — session detection
  surfaces them.

## Testing

No formal test suite yet. Verify changes by running the server on a scratch port
against a built JSON payload (see how it's done in conversation history): POST a
sample, then GET the endpoints and assert counts/values. A headless screenshot of
the dashboard via Windows Chrome confirms the frontend renders.

## Session detection

`detection.py` implements the algorithm as pure functions: minute-grid → active
minutes by **elevated HR** (HR drives continuity; per-minute steps are too noisy
to gate on) → gap-merge → ≥5-min filter → validate session **average cadence**
(rejects stress/heat HR spikes) → run/walk by cadence.
`run_detection_for_athlete` in `main.py` reads the athlete's deduped HR + step
rows from the typed tables (across all syncs), matches each session to overlapping
explicit workouts, and writes `detected_sessions` (replacing the athlete's rows).
It runs automatically at ingest and via `POST /detect` (reprocess from stored
samples — no re-upload). When building the step list for detection, **include
`source`** or the primary-source dedup silently breaks and cadence inflates.
`GET /sessions` + `/sessions/{id}` serve them; the dashboard shows them with
recorded/detected badges and a per-session HR chart (`session.html`).

Bump `DETECTION_VERSION` and re-run `/detect` when the algorithm changes.

**Step/distance source dedup:** steps *and* distance arrive from multiple sources
(Fitbit + Android + Health Connect) that redundantly count the same activity with
overlapping records — summing inflates totals (cadence ~2x; distance likewise).
`_minute_max_from_primary_source` (detection.py) picks one primary source (the one
with the MOST records — the continuous wrist tracker) and takes the largest record
per minute. Picking by total is wrong: a coarse all-day phone counter can have the
highest total but not cover workouts. Detected-session distance uses this too —
don't sum interval rows across sources.

**Known tuning gaps (v1):** HR threshold is a fixed default (no per-athlete
resting/max yet). 5-sample HR smoothing from the doc isn't applied (minute-median
already smooths). Detected windows can extend past the real workout (gap-merge
bridges adjacent walking), which dilutes a run's average cadence.

## Not yet built (deferred, described in the schema doc)

- Per-athlete resting/max HR profiles to tune the detection threshold.
- Pruning old `syncs` rows (metadata only now, so low priority).
- Splitting sleep stages out of `interval_samples` into their own table — only if
  sleep analysis becomes a heavy query path. Fine in the shared table for now
  (sleep volume is tiny vs HR); the `stream` column makes it a clean migration.
- Sign in with Apple (second `auth_identities` provider), refresh tokens +
  shorter access tokens, GPS via Strava OAuth.

## Commits

Branch off `main` before committing unless told otherwise. Keep `venv/`, `*.db`,
and the `docs` symlink out of commits (already in `.gitignore`).
