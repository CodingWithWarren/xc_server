# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

Backend for the XC Training Flutter app. Ingests health samples (heart rate,
steps, workouts) and serves them back + a web dashboard. The data contract is
defined in [`docs/SERVER_SCHEMA.md`](docs/SERVER_SCHEMA.md) — treat it as the
source of truth for sample shapes, dedup, and validation rules. (`docs/` is a
symlink to the Flutter app's docs folder, which owns that contract.)

## Critical constraint: zero external dependencies

This project deliberately uses **only Node built-in modules** — `node:sqlite`,
`node:http`, `node:crypto`. There is **no `node_modules`** and `npm install` is
not expected to be run.

- Requires **Node ≥ 22.5** (for the stable built-in `node:sqlite`).
- Do **not** add npm packages (no express, better-sqlite3, jsonwebtoken,
  bcrypt, etc.). The JWT, password hashing, and SQLite layers are all
  hand-rolled on built-ins under `src/utils/` and `src/db.js`. If you're tempted
  to reach for a library, extend the built-in helpers instead.
- The dashboard (`public/index.html`) may use CDN-loaded browser libs
  (Chart.js) — that's the browser's dependency, not the server's.

## Environment specifics (important)

- System `node`/`npm` are **not installed**. Use the Node binary bundled with
  the VS Code server. `./run.sh` auto-discovers it (its
  `~/.vscode-server/bin/<hash>/node` path changes on VS Code updates, so never
  hardcode the hash).
- This runs under **WSL2 in NAT mode**. For a phone/emulator to reach the
  server, Windows needs a portproxy + firewall rule forwarding `:3000` to the
  WSL IP (see git history / prior setup). The phone connects to the **Windows
  LAN IP**, not the WSL IP.

## Running

```bash
./run.sh                 # preferred; auto-finds Node, loads .env
# or: node --env-file-if-exists=.env src/index.js
```

Listens on `0.0.0.0:3000`. Health check: `GET /health`. Dashboard: `GET /`.

When launching in an agent session, run it as a background task — it's a
long-lived server.

## Architecture

- `src/index.js` — plain `http.createServer` with a small exact-match route
  table. Each handler gets `{ req, res, body, query, user, send }`. `send(status,
  obj)` writes JSON. Auth-required routes are marked `auth: true`.
- `src/db.js` — opens the SQLite DB, sets WAL + foreign keys, creates the
  `users` and `health_samples` tables. The `value` column is JSON text; numeric
  values are promoted into `numeric_value` for cheap aggregation.
- `src/routes/healthSamples.js` — batch ingest wraps inserts in one transaction
  and uses `INSERT OR IGNORE` against the unique constraint
  `(user_id, source_uuid, type, date_from)` for idempotent dedup.
- `src/utils/dedup.js` — when `uuid` is empty, synthesizes a deterministic
  `surrogate:<sha256>` key (keep hash inputs in sync with the client if it ever
  pre-hashes).

## Conventions

- Match the existing style: CommonJS `require`, no build step, comments that
  explain *why* (especially where they cite a `docs/SERVER_SCHEMA.md` section).
- Unknown enum values (`type`, `unit`, `workoutActivityType`) are stored as
  free-text strings — never add CHECK constraints that would reject new sample
  types from a client upgrade.
- Per-sample validation rejects only the offending sample; a batch is never
  failed wholesale.

## Testing changes

There is no test suite. Verify by running the server and exercising endpoints
with `curl` (register → login → batch ingest → read back / summary / series),
or by loading the dashboard at `/`.
