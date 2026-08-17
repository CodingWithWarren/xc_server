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

## Coach email digest

Athletes' home screen shows a summary of the coach's email. Coach mail is
forwarded into **one shared team mailbox**; the server polls it, summarizes it
once with Claude, and serves the same digest to every athlete. The app does none
of this: mailbox credentials and the model API key would be readable by anyone
who unzips the APK, and one summarization serves the whole team.

- `coach_digest.py` — IMAP polling, HTML flattening, summarization, caching.
- `models.py` — `CoachMailbox` / `CoachMessage` / `CoachDigest`.
- Config is env-only (`.env.example`); startup mirrors it into the mailbox row
  (`sync_team_mailbox_from_env`) with the password encrypted at rest.

**The response shape is a fixed contract — the mobile client is already shipped
against it.** Both endpoints need `Authorization: Bearer` and are scoped to the
token's athlete. `digest` may be `null` and `messages` `[]` (the legitimate
"nothing yet" state). Timestamps go out ISO-8601 with an explicit **Z** — a
zoneless timestamp makes the app's "Summarized 3h ago" hours wrong — and the
content type carries `charset=utf-8` (summaries contain em-dashes).

| Endpoint | Behavior |
|---|---|
| `GET /coach-digest` | The cached digest. Cheap and side-effect-free — two indexed reads, **never** an IMAP connection or a model call. Called on every app open. |
| `POST /coach-digest/refresh` | Poll now, re-summarize, return it. Backs the "Re-summarize" button, so it bypasses the skip-if-unchanged check. |

**Status codes carry meaning.** `404`/`501` = "this server doesn't do digests":
the app hides the card for the rest of the session. That's what an unconfigured
deployment returns (no `COACH_IMAP_*` → no mailbox row → 501). **Never return
`200` with an empty digest to mean unsupported** — that renders an empty card
instead of no card. `401` sends the athlete back to sign-in; any other 4xx/5xx
shows an error *under* the locally cached digest, so a poll failure is a `502`
with a useful message, not a silent empty 200.

**Only call the model when the mail actually changed.** `coach_digests.source_ids`
holds the sorted Message-IDs behind the summary; if the window still holds
exactly those, skip the model call. Key this on the **`Message-ID` header, never
the IMAP sequence number** — sequence numbers shift as mail arrives, so keying on
them re-summarizes (and re-bills) on every poll.

**IMAP gotchas (learned the hard way):**
- **Fetch with `BODY.PEEK[]`, never `BODY[]`.** `BODY[]` sets `\Seen` and
  silently marks the coach's mail read in a mailbox a human also reads. The
  folder is also opened `readonly=True` as a second guard.
- **`SINCE` is `dd-MMM-yyyy` with English month abbreviations** (`SINCE
  05-Aug-2026`). Built by hand in `imap_since` — `strftime("%d-%b-%Y")` is
  locale-aware and a localized month name makes the search silently return
  nothing.
- **Gmail needs an App Password *and* IMAP enabled** (and 2FA on before app
  passwords exist). `AUTHENTICATIONFAILED` is almost always one of those two.
- **Auto-forwarding preserves the original `From:`; the Forward button does
  not** — a hand-forwarded mail arrives from the athlete with the real sender
  only in the quoted `---------- Forwarded message ----------` block. The sender
  filter matches the header first, then scans the first ~600 body chars.

Budget: 14-day window, 25 messages, 4000 chars per body, poll every ~20 min.

**Summarization goes through OpenRouter** (`_call_model`) — a plain OpenAI-shaped
HTTPS POST via `requests`, no provider SDK. `COACH_DIGEST_MODELS` is a
comma-separated list tried in order, and it deliberately mixes tiers: a cheap
**paid** primary (`deepseek/deepseek-v4-flash-0731`, ~$0.08/M in — pennies a
month at this volume) with a **`:free`** model behind it. The free entry is the
safety net for a zero balance (HTTP 402), a throttled endpoint, or a provider
outage; without it, running out of credit would silently kill the digest instead
of degrading it. Keep a `:free` entry last. The `:free` suffix is what makes a
model cost nothing — dropping it starts billing. Model ids are date-pinned so an
alias can't change behavior under us. Avoid "reasoning" variants; they emit
their thinking alongside the answer and break the strict-JSON reply. Free models
come and go — the current list is at
<https://openrouter.ai/models?max_price=0>.

`COACH_DIGEST_DATA_COLLECTION` (default `deny`) restricts routing to providers
that don't retain prompts, because the prompt is students' coach email. Blank it
to route anywhere — more providers, fewer failures, weaker privacy. Verified
working, but note it can make a model **404 outright** ("no endpoints found
matching your data policy") when none of its providers qualify; both NVIDIA free
models did exactly that.

**Thinking is off** (`COACH_DIGEST_REASONING=off` → `reasoning: {"enabled":
false}`). Learned the hard way: DeepSeek v4 Flash is a reasoning model, and
`max_tokens` caps **thinking + answer together** — on a long window the thinking
consumed the whole budget and `content` came back **empty**, which the poller
correctly read as a failed model and fell through to the free fallback every
time. Measured on an 8-email prompt: 3.0s / 123 output tokens with thinking off
versus 9–15s / 850–1070 tokens with it on, for the same JSON. Summarizing email
is extraction, not a puzzle. If a future model genuinely needs thinking, set it
to `on` **and** raise `max_tokens` in `_post_completion` well above the thinking
length, or the empty-content failure comes back.

The summarizer asks for strict JSON and parses defensively (strip a ```json
fence, take the outermost `{...}`); **an unparsable reply keeps the previous
digest** rather than overwriting a good summary with nothing. That matters more
on small free models than it would on a frontier one.

**Replies and corrections (`group_threads` / `split_reply_quote`).** A coach's
follow-up ("Sorry! Media Day is the 25th!") only lands if the model can tell it
supersedes the original, so the prompt groups mail into conversations —
`In-Reply-To`/`References` first, normalized subject as a fallback, and only
when something in the group carries a `Re:`/`Fwd:` prefix so two unrelated
same-subject emails aren't merged. Threads run newest-conversation-first, but
messages **within** a thread run oldest → newest so the last thing read about a
topic is the latest word on it.

The bigger half is `split_reply_quote`: a reply carries a quoted copy of the
message it answers, and that copy is the *stale* version of the very fact being
corrected. Measured on real mail, one reply was **112 chars of correction
followed by a 3,423-char quote** — so the old flat prompt fed the model the
original's content twice and the update was 3% of the text. It duly reported the
superseded date. The quoted tail is now truncated and labelled as history.
Do **not** strip forwarded blocks this way: `---------- Forwarded message ----------`
content IS the message, and the hand-forward sender fallback depends on it.
Gmail wraps the `On ... wrote:` attribution across two lines, so that pattern
needs `DOTALL` — a single-line version silently leaves it in the new text.

The free tier also has a **daily request cap**, which is the other reason the
skip-if-unchanged check earns its keep: a quiet inbox costs zero model calls, so
normal operation is a handful of requests a day, not one per poll.

**The mailbox password must never appear in a response, a log line, or an error
message.** It's stored Fernet-encrypted (key from `COACH_MAILBOX_KEY`, else
derived from `JWT_SECRET`); errors name the host, never the credential.

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

`tests/` holds pytest coverage for the coach email digest (fake IMAP + fake model
— no network, no real DB):

```
pip install -r requirements-dev.txt
venv/bin/python -m pytest tests/ -q
```

The rest of the app has no formal suite yet. Verify changes by running the server on a scratch port
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
- Per-athlete coach mailboxes. `coach_mailboxes.athlete_id` is already nullable
  (NULL = the shared team mailbox) and lookup prefers an athlete's own row, so
  adding them is data, not a migration. Not needed while coach mail is a
  team-wide broadcast.
- Showing the digest on the web dashboard — today it's mobile-only.

## Commits

Branch off `main` before committing unless told otherwise. Keep `venv/`, `*.db`,
and the `docs` symlink out of commits (already in `.gitignore`).
