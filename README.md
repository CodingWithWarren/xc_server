# XC Training — Backend Server

Backend for the **XC Training** Flutter health-tracking app. It ingests health
samples (heart rate, steps, workouts, etc.) read from Apple HealthKit / Android
Health Connect, stores them in SQLite, and serves them back for display and a
built-in web dashboard.

Implements the upload contract in [`docs/SERVER_SCHEMA.md`](docs/SERVER_SCHEMA.md).

## Highlights

- **Zero external dependencies.** Runs entirely on Node's built-in modules:
  `node:sqlite`, `node:http`, and `node:crypto`. No `npm install` required.
- **JWT auth** (register / login) with scrypt password hashing.
- **Idempotent batch ingest** with per-sample validation, partial success, and
  dedup — including synthesized surrogate keys for samples with empty UUIDs.
- **Cursor-paginated read-back** plus aggregation endpoints.
- **Web dashboard** served at `/` for browsing the data (summary cards, charts,
  recent-samples table).

## Requirements

- **Node.js ≥ 22.5** (for the built-in `node:sqlite` module). No other tooling.

## Running

```bash
npm start              # if npm is available
# or directly:
node --env-file-if-exists=.env src/index.js
# or via the launch script (auto-discovers a Node binary):
./run.sh
```

The server listens on `0.0.0.0:3000` (override with `PORT`). Open the dashboard
at <http://localhost:3000>.

### Configuration (`.env`)

Copy `.env.example` to `.env` and adjust:

| Var | Default | Purpose |
|---|---|---|
| `PORT` | `3000` | HTTP port |
| `JWT_SECRET` | _(change me)_ | HMAC secret for signing tokens |
| `JWT_EXPIRES_IN` | `7d` | Token lifetime (`7d`, `12h`, `30m`, `60s`) |
| `DB_PATH` | `./xc_training.db` | SQLite database file |

## API

All `/v1/*` data endpoints require `Authorization: Bearer <token>`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/auth/register` | Create a user, returns `{ userId, email, token }` |
| `POST` | `/v1/auth/login` | Log in, returns `{ userId, email, token }` |
| `POST` | `/v1/health-samples:batch` | Batch ingest (≤ 1000 samples) |
| `GET` | `/v1/health-samples` | Read back: `?type=&from=&to=&limit=&cursor=` |
| `GET` | `/v1/summary` | Per-type counts, ranges, and aggregates |
| `GET` | `/v1/series` | Downsampled time series: `?type=&buckets=&from=&to=` |
| `GET` | `/health` | Liveness probe |
| `GET` | `/` | Web dashboard |

### Batch ingest

```http
POST /v1/health-samples:batch
Authorization: Bearer <token>
Content-Type: application/json

{ "schemaVersion": 1, "uploadedAt": "...", "device": {...}, "samples": [ ... ] }
```

Response is partially successful by design — valid samples commit even if some
are rejected:

```json
{ "received": 500, "inserted": 480, "duplicates": 20, "rejected": [ { "index": 312, "uuid": "...", "reason": "dateTo before dateFrom" } ] }
```

See [`docs/SERVER_SCHEMA.md`](docs/SERVER_SCHEMA.md) for the full sample shape,
dedup rules, and validation guards.

## Project layout

```
src/
├── index.js                 HTTP server, router, static file serving
├── db.js                    SQLite init + schema (node:sqlite)
├── middleware/auth.js       Bearer JWT verification
├── routes/
│   ├── auth.js              register / login
│   ├── healthSamples.js     batch ingest + cursor-paginated read-back
│   └── stats.js             summary + series aggregation
└── utils/
    ├── jwt.js               HS256 sign/verify (no external lib)
    ├── password.js          scrypt hash/verify
    ├── dedup.js             empty-UUID surrogate key synthesis
    └── validation.js        per-sample validation rules
public/index.html            web dashboard (vanilla JS + Chart.js via CDN)
```

## Notes

- The dashboard loads Chart.js from a CDN, so the **browser** viewing it needs
  internet; the server itself has no network dependencies.
- This is a development/testing setup. For production, set a strong `JWT_SECRET`,
  serve over HTTPS, and review the open questions in `docs/SERVER_SCHEMA.md` §8
  (retention, PII/compliance, rate limiting).
