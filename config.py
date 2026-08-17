"""App configuration loaded from .env (see .env.example).

The JWT secret must come from the environment — never generated at startup —
so issued tokens survive server restarts (regenerating it logs everyone out).
"""
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env in the project root; real env vars take precedence

JWT_SECRET = os.getenv("JWT_SECRET", "")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is not set. Copy .env.example to .env and generate one "
        'with: python -c "import secrets; print(secrets.token_hex(32))"'
    )

DEV_MODE = os.getenv("DEV_MODE", "false").strip().lower() in ("1", "true", "yes")

# Google OAuth client IDs accepted as the ID token audience (web + Android).
GOOGLE_CLIENT_IDS = [
    c.strip() for c in os.getenv("GOOGLE_CLIENT_IDS", "").split(",") if c.strip()
]

# Long-lived for dev convenience. Before real team rollout: shorten to ~1h and
# add refresh tokens so a leaked access token has a small blast radius.
ACCESS_TOKEN_TTL_DAYS = 30


# --- Coach email digest (see CLAUDE.md "Coach email digest") -------------------
# One shared team mailbox: coach mail is forwarded into it, the server polls it
# and summarizes once, and every athlete reads the same digest. Credentials live
# here (env) and NEVER in the app binary — anyone can unzip an APK.

COACH_IMAP_HOST = os.getenv("COACH_IMAP_HOST", "").strip()
COACH_IMAP_PORT = int(os.getenv("COACH_IMAP_PORT", "993").strip() or 993)
COACH_IMAP_USERNAME = os.getenv("COACH_IMAP_USERNAME", "").strip()
# Gmail: this must be a 16-character App Password (with 2FA on and IMAP enabled
# in Gmail settings). The account password is rejected over IMAP.
COACH_IMAP_PASSWORD = os.getenv("COACH_IMAP_PASSWORD", "")

# Comma-separated allow-list of coach addresses ("coach@school.edu") or bare
# domains ("@school.edu"). Empty means "summarize everything in the mailbox".
COACH_SENDERS = [s.strip().lower()
                 for s in os.getenv("COACH_SENDERS", "").split(",") if s.strip()]

# Summarization goes through OpenRouter (one OpenAI-compatible endpoint in front
# of many providers) so this can run on the free model tier — a school project
# shouldn't need a billed API account.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip().rstrip("/")

# Tried in order, first success wins.
#
# Primary is a PAID model (~$0.08/M in, $0.18/M out) — pennies a month at this
# volume, but it needs credit on the OpenRouter account. The ":free" entry behind
# it is the safety net: if credits run out, a provider errors, or the endpoint is
# busy, the digest is still produced instead of disappearing. Free endpoints are
# also rate-limited, which is the other reason to keep more than one entry.
#
# The model id is date-pinned on purpose — an unpinned alias can change model
# behavior under you without a code change. Avoid "reasoning" variants: they emit
# their thinking alongside the answer, which breaks the strict-JSON reply.
# Current free list: https://openrouter.ai/models?max_price=0
DEFAULT_COACH_DIGEST_MODELS = (
    "deepseek/deepseek-v4-flash-0731,"
    "google/gemma-4-26b-a4b-it:free"
)
COACH_DIGEST_MODELS = [
    m.strip() for m in
    os.getenv("COACH_DIGEST_MODELS", DEFAULT_COACH_DIGEST_MODELS).split(",")
    if m.strip()
]

# Per-model HTTP timeout. Two models at 40s stays inside the mobile client's 90s
# timeout on POST /coach-digest/refresh. Measured: the paid primary answers in a
# few seconds, but a busy free endpoint took ~24s on a 3-email prompt — 25s was
# cutting it too fine, and some free models are far slower still.
COACH_DIGEST_TIMEOUT_SECONDS = int(
    os.getenv("COACH_DIGEST_TIMEOUT_SECONDS", "40").strip() or 40)

# Some models (the DeepSeek v4 family included) "think" before answering, and
# max_tokens caps thinking + answer TOGETHER — so on a long prompt the thinking
# eats the whole budget and the answer comes back EMPTY. Summarizing email is
# extraction, not a puzzle, so thinking is off by default. Measured on an
# 8-email prompt: 3.0s / 123 output tokens with it off, vs 9-15s / 850-1070
# tokens with it on, for the same JSON. Set to "on" if a future model needs it.
COACH_DIGEST_REASONING = os.getenv("COACH_DIGEST_REASONING", "off").strip().lower()

# ...but the flag above is only a REQUEST, and not every OpenRouter provider
# honors it: AtlasCloud served deepseek-v4-flash with ~3,900 chars of reasoning
# despite reasoning.enabled=false, which burned all 900 tokens and returned
# empty content on 2 of 3 tries. So the ceiling has to leave room for thinking
# we asked not to happen. ~3k covers the observed reasoning plus the answer;
# the extra output tokens cost fractions of a cent.
COACH_DIGEST_MAX_TOKENS = int(
    os.getenv("COACH_DIGEST_MAX_TOKENS", "3000").strip() or 3000)

# OpenRouter routes each request to one of many providers, whose data-retention
# policies differ. "deny" restricts routing to providers that don't collect
# prompts — the safer default when the prompt is students' coach email. Set it
# blank to let OpenRouter route anywhere (more providers, so fewer failures).
COACH_DIGEST_DATA_COLLECTION = os.getenv(
    "COACH_DIGEST_DATA_COLLECTION", "deny").strip().lower()

# Providers to route around. AtlasCloud ignores reasoning.enabled=false on
# deepseek-v4-flash and thinks anyway — up to 12,000 characters of it, which no
# max_tokens ceiling can absorb, leaving `content` empty. Measured on the real
# digest prompt: 2/4 replies usable with it in the pool, 4/5 with it excluded
# (and reasoning tokens dropped to zero). `require_parameters` does NOT help —
# it advertises support for the flag and then ignores it.
COACH_DIGEST_IGNORE_PROVIDERS = [
    p.strip() for p in
    os.getenv("COACH_DIGEST_IGNORE_PROVIDERS", "AtlasCloud").split(",") if p.strip()
]

# Background poll cadence. Every poll is an IMAP fetch; the model is only called
# when the set of Message-IDs in the window actually changed.
COACH_POLL_INTERVAL_MINUTES = int(
    os.getenv("COACH_POLL_INTERVAL_MINUTES", "20").strip() or 20)

# Budget knobs. 14 days / 25 messages / 4000 chars per body is plenty for a
# season's worth of coach mail and keeps one summarization cheap.
COACH_WINDOW_DAYS = int(os.getenv("COACH_WINDOW_DAYS", "14").strip() or 14)
COACH_MAX_MESSAGES = int(os.getenv("COACH_MAX_MESSAGES", "25").strip() or 25)
COACH_MAX_BODY_CHARS = int(os.getenv("COACH_MAX_BODY_CHARS", "4000").strip() or 4000)

# Optional dedicated key for encrypting the stored mailbox password. When unset
# the key is derived from JWT_SECRET — safe here because the plaintext password
# always exists in the environment anyway, so a rotated secret just means the
# stored copy is re-encrypted at the next startup.
COACH_MAILBOX_KEY = os.getenv("COACH_MAILBOX_KEY", "").strip()


def coach_digest_configured() -> bool:
    """True when this deployment has a coach mailbox set up. When False the two
    /coach-digest endpoints answer 501 — the mobile app reads that as "this
    server doesn't do digests" and hides the card entirely (a 200 with an empty
    digest would render an empty card instead)."""
    return bool(COACH_IMAP_HOST and COACH_IMAP_USERNAME and COACH_IMAP_PASSWORD)
