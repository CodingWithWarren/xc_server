# Privacy — XC Training Data Server

This server stores athletes' health data (heart rate, steps, distance, sleep,
workouts). That data is sensitive; this document records who can see what.

## Authenticated access model

- Every data endpoint requires a signed-in user (`Authorization: Bearer` with a
  server-issued JWT). There is no anonymous read or write access.
- Sign-in is via an identity provider (Google today; Sign in with Apple
  planned). The server verifies provider tokens server-side and never stores
  provider passwords. Provider credentials live in `auth_identities`; the
  athlete's profile (name, email, role, grade) lives in `athletes`.
- Uploads are attributed to the athlete in the token — a client-supplied
  athlete id is ignored, so one athlete cannot write data as another.

## Role rules

| Role | Can read | Can write |
|---|---|---|
| `athlete` (default) | Only their own data (403 for anyone else's) | Uploads under their own identity |
| `coach` | Any athlete's data, and the athlete roster | Uploads under their own identity |

- New sign-ups default to `athlete`. Coach promotion is a deliberate manual
  database operation, not self-service.
- Comparisons shown on the dashboard ("vs last week") are always within one
  athlete's own history — athletes are never ranked against each other.

## Development mode

`DEV_MODE=true` enables a development-only sign-in endpoint that bypasses the
identity provider. It must be disabled (`DEV_MODE=false`) on any deployment
holding real team data beyond the developer's own.

## Coach email digest

The server polls **one shared team mailbox** that coach email is forwarded into,
and stores the last 14 days of matching messages (sender, subject, date, plain
text body) plus a Claude-generated summary.

- **Every signed-in athlete reads the same team digest**, because the mailbox is
  shared. Treat anything in that mailbox as visible to the whole team — the
  `COACH_SENDERS` allow-list exists to keep it to coach mail, and personal email
  must not be forwarded there.
- Message bodies are sent to **OpenRouter** to be summarized, which forwards them
  to whichever model provider serves the configured model. Nothing else in this
  database (health data, names, workouts) is sent with them.
- Coach email can name students and give practice times and locations, so
  `COACH_DIGEST_DATA_COLLECTION=deny` (the default) restricts routing to
  providers that don't retain prompts. Blanking it trades that for reliability.
- **The fallback model is a free endpoint, which carries weaker guarantees.**
  Free tiers generally allow more logging than paid ones, and OpenRouter gates
  some free models behind an account setting that permits training on prompts —
  so a request that falls back can be handled under a looser policy than the
  primary. Review the toggles at <https://openrouter.ai/settings/privacy>. If
  that's not acceptable for team mail, make every entry in
  `COACH_DIGEST_MODELS` a paid model and keep credit on the account.
- The mailbox password is stored encrypted at rest and never appears in a
  response, a log line, or an error message. It, and the model API key, live in
  `.env` — never in the mobile app, which only ever sees the finished digest.
- Messages that fall out of the 14-day window are deleted on the next poll.

## Data retention

No automatic deletion yet. Removing an athlete's data is currently a manual
database operation; build a proper delete flow before team rollout.
