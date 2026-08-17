"""Coach email digest: poll one mailbox, summarize it, cache the result.

Why the server does this instead of the app: the mailbox password and the model
API key would both be readable by anyone who unzips the APK, and summarizing
once here is one model call for the whole team instead of one per phone.

Summarization runs on OpenRouter's free model tier (see config), so the digest
costs nothing to produce. Free endpoints are rate-limited, which is the other
reason the "did anything change?" check matters: a quiet inbox makes no call.

Shape of the feature (see CLAUDE.md "Coach email digest"):

    coach mail  --forwarded-->  one team mailbox
                                      |  IMAP poll every ~20 min
                                      v
                          coach_messages (deduped by Message-ID)
                                      |  only when the id set changed
                                      v
                          coach_digests  ->  GET /coach-digest

GET only reads those tables. Polling and summarizing happen in the background
job and in POST /coach-digest/refresh — never in GET.
"""
import base64
import email
import email.policy  # imported explicitly: `import email` alone doesn't bind it
import email.utils
import hashlib
import html as html_module
import imaplib
import json
import logging
import re
import threading
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime, timedelta, timezone
from email.message import Message

import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import config
from database import SessionLocal
from models import CoachDigest, CoachMailbox, CoachMessage

log = logging.getLogger("coach_digest")


class MailboxError(Exception):
    """Anything that stopped a refresh: IMAP login, network, model call.

    The message is shown to the athlete, so it must stay useful AND must never
    contain the mailbox password."""


def _utcnow() -> datetime:
    # Naive UTC, matching how every other timestamp in this DB is stored.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_z(value: datetime | None) -> str | None:
    """Serialize a stored (naive UTC) timestamp as ISO-8601 with an explicit Z.

    Without the Z the client parses it as *local* time and "Summarized 3h ago"
    comes out hours wrong."""
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


# --- Password encryption at rest ----------------------------------------------

def _fernet():
    """Build the Fernet cipher used for the stored mailbox password.

    The key is derived from COACH_MAILBOX_KEY, or from JWT_SECRET when that's
    unset. Deriving from JWT_SECRET is fine here: the plaintext password also
    lives in the environment, so a rotated secret only means the stored copy
    can't be decrypted and gets rewritten from env at the next startup."""
    from cryptography.fernet import Fernet  # imported lazily: only this feature needs it

    secret = config.COACH_MAILBOX_KEY or config.JWT_SECRET
    digest = hashlib.sha256(("coach-mailbox-v1:" + secret).encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


# --- IMAP ----------------------------------------------------------------------

# IMAP's SINCE criterion is dd-MMM-yyyy with ENGLISH month abbreviations, always.
# Built by hand on purpose: strftime("%d-%b-%Y") is locale-aware, so on a machine
# with a non-English locale it emits e.g. "05-Aoû-2026" and the search silently
# returns nothing instead of failing.
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def imap_since(day: date) -> str:
    """Format a date for IMAP's SINCE criterion: 05-Aug-2026."""
    return f"{day.day:02d}-{_IMAP_MONTHS[day.month - 1]}-{day.year}"


@dataclass
class CoachEmail:
    """One parsed message, ready to store and to feed the model."""
    message_id: str
    sender: str
    sender_name: str
    subject: str
    sent_at: datetime | None
    body: str
    # Threading headers, used to group a correction with what it corrects.
    # Default so older call sites (and tests) can build one positionally.
    in_reply_to: str | None = None
    references: list[str] = dataclass_field(default_factory=list)


# --- Threading: grouping a correction with the message it corrects ------------
#
# A coach's follow-up ("actually the bus leaves at 7:00") only helps if the model
# can tell it supersedes the original. Two things make that work: grouping the
# messages into threads, and marking the quoted copy of the original that rides
# along inside a reply as historical rather than current.

_REPLY_PREFIX_RE = re.compile(r"^\s*(?:re|fwd?|fw|aw|sv)\s*(?:\[\d+\])?\s*:\s*",
                              re.IGNORECASE)
_MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")


def has_reply_prefix(subject: str) -> bool:
    """Does the subject look like a reply/forward ("Re: ...", "Fwd: ...")?"""
    return bool(_REPLY_PREFIX_RE.match(subject or ""))


def normalize_subject(subject: str) -> str:
    """Strip every leading Re:/Fwd: so a thread's messages compare equal."""
    text = subject or ""
    while True:
        stripped = _REPLY_PREFIX_RE.sub("", text)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.split()).lower()


# A forwarded block IS the content of a forward, so it must never be treated as
# a stale reply quote — the hand-forward case depends on keeping it.
_FORWARD_MARKER_RE = re.compile(r"^[ \t]*-{2,}[ \t]*Forwarded message[ \t]*-{2,}",
                                re.IGNORECASE | re.MULTILINE)
_REPLY_MARKER_RES = (
    # "On Mon, 10 Aug 2026 at 09:14, Coach Kim <...> wrote:" — DOTALL because
    # Gmail wraps this attribution across two lines, and a single-line pattern
    # then misses it and leaves it sitting in the "new" text.
    re.compile(r"^[ \t]*On\b.{0,300}?\bwrote:[ \t]*$",
               re.IGNORECASE | re.MULTILINE | re.DOTALL),
    re.compile(r"^[ \t]*-{2,}[ \t]*Original Message[ \t]*-{2,}",
               re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*>", re.MULTILINE),
)


def split_reply_quote(body: str) -> tuple[str, str]:
    """Split a reply into (what's new, what it quoted).

    The quoted half is the *previous* state of the world — the very thing a
    correction is overriding — so the prompt labels it instead of letting it sit
    alongside the new text as if both were current. Returns the whole body as
    "new" when there's no reply quote, or when the quote is a forwarded block
    (then it's the content, not history)."""
    if not body:
        return "", ""
    starts = [m.start() for m in
              (rx.search(body) for rx in _REPLY_MARKER_RES) if m]
    if not starts:
        return body, ""
    cut = min(starts)

    forward = _FORWARD_MARKER_RE.search(body)
    if forward and forward.start() <= cut:
        return body, ""          # a forward: the quoted block is the message

    new_text = body[:cut].strip()
    if not new_text:
        return body, ""          # nothing but quote — keep it rather than lose it
    return new_text, body[cut:].strip()


def group_threads(emails: list[CoachEmail]) -> list[list[CoachEmail]]:
    """Group messages into threads, each ordered oldest -> newest.

    Primary signal is the In-Reply-To/References headers. Subject is only a
    fallback, and only when something in the group actually looks like a reply
    — otherwise two unrelated emails that share a subject ("Practice update"
    two weeks apart) would be merged and the older one treated as superseded."""
    parent = {mail.message_id: mail.message_id for mail in emails}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_a] = root_b

    known = set(parent)
    for mail in emails:
        for ref in ([mail.in_reply_to] if mail.in_reply_to else []) + list(mail.references):
            if ref in known:
                union(mail.message_id, ref)

    by_subject: dict[str, list[CoachEmail]] = {}
    for mail in emails:
        by_subject.setdefault(normalize_subject(mail.subject), []).append(mail)
    for subject, group in by_subject.items():
        if subject and len(group) > 1 and any(has_reply_prefix(m.subject) for m in group):
            for mail in group[1:]:
                union(group[0].message_id, mail.message_id)

    threads: dict[str, list[CoachEmail]] = {}
    for mail in emails:
        threads.setdefault(find(mail.message_id), []).append(mail)

    ordered = []
    for messages in threads.values():
        messages.sort(key=lambda m: (m.sent_at is None, m.sent_at or datetime.min))
        ordered.append(messages)
    # Newest thread first, judged by its most recent message.
    ordered.sort(
        key=lambda t: max((m.sent_at for m in t if m.sent_at), default=datetime.min),
        reverse=True)
    return ordered


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"</?(br|p|div|tr|li|h[1-6])\b[^>]*>", re.IGNORECASE)
_BLANK_RE = re.compile(r"\n\s*\n\s*\n+")


def flatten_html(raw: str) -> str:
    """Turn an HTML mail part into plain text.

    The mobile client renders `body` verbatim and does no sanitizing, so tags
    must never reach it. Deliberately simple (no parser dependency): drop
    script/style wholesale, turn block tags into newlines, strip what's left,
    unescape entities."""
    text = _SCRIPT_RE.sub(" ", raw)
    text = _BREAK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html_module.unescape(text)
    # \u00a0 is the non-breaking space &nbsp; unescapes to — mail is full of them.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    # Collapse trailing spaces per line and runs of blank lines.
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:  # a charset Python doesn't know
        return payload.decode("utf-8", errors="replace")


def extract_body(msg: Message) -> str:
    """Best plain-text body for a message: prefer text/plain, else flatten HTML."""
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if part.get_content_type() == "text/plain":
                plain_parts.append(_decode_part(part))
            elif part.get_content_type() == "text/html":
                html_parts.append(_decode_part(part))
    elif msg.get_content_type() == "text/html":
        html_parts.append(_decode_part(msg))
    else:
        plain_parts.append(_decode_part(msg))

    if plain_parts:
        body = "\n\n".join(p for p in plain_parts if p.strip())
        if body.strip():
            return _BLANK_RE.sub("\n\n", body.replace("\r\n", "\n")).strip()
    return flatten_html("\n".join(html_parts))


def _header(msg: Message, name: str) -> str:
    """Read a header as a decoded string (RFC 2047 words already unfolded by
    email.policy.default), never None."""
    value = msg.get(name)
    return str(value).strip() if value else ""


def _synthesized_id(msg: Message, body: str) -> str:
    """A stable stand-in Message-ID for the rare mail that ships without one.

    Hashing the headers + body start keeps the id identical across polls, which
    is the whole point — an unstable id would re-trigger the model every time."""
    seed = "|".join([_header(msg, "From"), _header(msg, "Date"),
                     _header(msg, "Subject"), body[:200]])
    return "<no-id-" + hashlib.sha1(seed.encode("utf-8")).hexdigest() + "@xc-server>"


# The real sender of a hand-forwarded mail only appears in the quoted block:
#     ---------- Forwarded message ----------
#     From: Coach Kim <coach@school.edu>
_FORWARD_SCAN_CHARS = 600
_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def sender_matches(allow_list: list[str], sender: str, body: str) -> bool:
    """Does this message come from an allowed coach address?

    Gmail's *auto-forwarding* preserves the original From:, so the header check
    hits. The "Forward" button does not — the mail arrives from whoever pressed
    it, with the real sender only in the quoted forward block — so fall back to
    scanning the top of the body."""
    if not allow_list:
        return True

    def allowed(address: str) -> bool:
        address = address.lower()
        return any(address == entry or
                   (entry.startswith("@") and address.endswith(entry))
                   for entry in allow_list)

    if sender and allowed(sender):
        return True
    return any(allowed(found)
               for found in _ADDRESS_RE.findall(body[:_FORWARD_SCAN_CHARS]))


def parse_message(raw: bytes) -> CoachEmail:
    """Parse one fetched message into the fields the digest needs."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = extract_body(msg)
    sender_name, sender = email.utils.parseaddr(_header(msg, "From"))

    sent_at = None
    date_header = _header(msg, "Date")
    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(date_header)
            if parsed is not None:
                sent_at = (parsed.astimezone(timezone.utc).replace(tzinfo=None)
                           if parsed.tzinfo else parsed)
        except (TypeError, ValueError):
            sent_at = None

    return CoachEmail(
        message_id=_header(msg, "Message-ID") or _synthesized_id(msg, body),
        sender=sender.lower(),
        sender_name=sender_name or sender,
        subject=_header(msg, "Subject"),
        sent_at=sent_at,
        body=body,
        # References is a space-separated chain of the whole thread; In-Reply-To
        # is just the immediate parent. Both are advisory — plenty of clients
        # omit them, which is why subject is kept as a fallback.
        in_reply_to=next(iter(_MESSAGE_ID_RE.findall(_header(msg, "In-Reply-To"))),
                         None),
        references=_MESSAGE_ID_RE.findall(_header(msg, "References")),
    )


def _open_mailbox(mailbox: CoachMailbox, imap_factory):
    """Connect + log in. Turns imaplib's errors into a MailboxError whose text is
    safe to show an athlete (and never contains the password)."""
    try:
        connection = imap_factory(mailbox.imap_host, mailbox.imap_port)
    except Exception as e:
        raise MailboxError(
            f"Could not reach the mail server {mailbox.imap_host}:{mailbox.imap_port} "
            f"({type(e).__name__}) — check COACH_IMAP_HOST/COACH_IMAP_PORT and "
            "this server's network access.")
    try:
        connection.login(mailbox.imap_username, decrypt_secret(mailbox.imap_password_encrypted))
    except imaplib.IMAP4.error as e:
        detail = str(e)
        if "AUTHENTICATIONFAILED" in detail.upper():
            # Almost always one of these two on Gmail.
            raise MailboxError(
                "The mailbox rejected the login. On Gmail this means IMAP is off "
                "or the password isn't an App Password (2FA must be on first).")
        raise MailboxError("The mailbox rejected the login.")
    except Exception as e:
        raise MailboxError(f"Mailbox login failed: {type(e).__name__}")
    return connection


def fetch_recent(mailbox: CoachMailbox, *, imap_factory=None,
                 window_days: int | None = None,
                 max_messages: int | None = None) -> list[CoachEmail]:
    """Fetch the coach mail in the current window, newest first.

    Uses BODY.PEEK[] (never BODY[]) and opens the folder read-only: BODY[] sets
    the \\Seen flag, which would silently mark the coach's mail as read in a
    mailbox a human may also be reading by hand."""
    imap_factory = imap_factory or imaplib.IMAP4_SSL
    window_days = window_days if window_days is not None else config.COACH_WINDOW_DAYS
    max_messages = max_messages if max_messages is not None else config.COACH_MAX_MESSAGES
    allow_list = [s.strip().lower()
                  for s in (mailbox.sender_filter or "").split(",") if s.strip()]

    connection = _open_mailbox(mailbox, imap_factory)
    try:
        connection.select("INBOX", readonly=True)
        since = imap_since((_utcnow() - timedelta(days=window_days)).date())
        status, data = connection.uid("SEARCH", None, "SINCE", since)
        if status != "OK":
            raise MailboxError("The mailbox refused the search for recent mail.")

        uids = (data[0] or b"").split()
        if not uids:
            return []
        # SEARCH returns oldest-first; keep the newest slice.
        uids = uids[-max_messages:]

        # ONE fetch for the whole window, not one per message. Gmail charges
        # ~3s of latency per FETCH command almost regardless of message size
        # (measured: 31KB took 19s on its own), so per-message fetching made the
        # poll scale with the number of emails — 25 messages would have blown
        # past the client's 90s refresh timeout. Batched, it's a single round
        # trip whatever the count.
        status, payload = connection.uid("FETCH", b",".join(uids), "(BODY.PEEK[])")
        if status != "OK" or not payload:
            return []

        emails: list[CoachEmail] = []
        for part in payload:
            if not (isinstance(part, tuple) and len(part) > 1 and part[1]):
                continue
            parsed = parse_message(part[1])
            if sender_matches(allow_list, parsed.sender, parsed.body):
                emails.append(parsed)

        # A batched FETCH doesn't promise ordering, so sort here rather than
        # relying on it: newest first, undated mail last. reverse=True puts
        # "has a date" (True) ahead of undated, and orders dates descending.
        emails.sort(key=lambda e: (e.sent_at is not None, e.sent_at or datetime.min),
                    reverse=True)
        return emails
    except MailboxError:
        raise
    except Exception as e:
        raise MailboxError(f"Reading the mailbox failed: {type(e).__name__}")
    finally:
        try:
            connection.logout()
        except Exception:
            pass


# --- Summarization -------------------------------------------------------------

SUMMARY_PROMPT = """\
You summarize emails a high school cross country coach sent to a runner, for a
card on the home screen of the runner's training app.

Reply with ONLY a JSON object, no markdown fence and no commentary:
{"headline": string, "bullets": [string], "actions": [string]}

- "headline": one sentence, at most 100 characters, naming the single most
  important or most time-sensitive thing across all the emails.
- "bullets": 2-5 short factual points — practice times and locations, meet
  details, workout assignments, schedule changes. Keep each under 120
  characters. Put concrete dates and times in, and prefer the newest email
  when two emails disagree.

The emails are grouped into conversations. A later reply in a conversation
UPDATES or CORRECTS the messages above it: when they conflict, report only the
corrected version and leave the superseded detail out entirely. A reply that
adds something is additional, not a replacement. Text marked as quoted from an
earlier message is history — never treat it as new or current.
- "actions": anything the runner personally has to do (forms, gear, replies,
  arrival times), each with its deadline if one was given. Empty list if the
  emails ask nothing of the runner.

Be concrete and never invent a detail that is not in the emails. If the emails
carry no useful information, return an empty headline and empty lists."""


# How much of a reply's quoted tail to show. Enough for the model to see what's
# being corrected, not so much that stale detail crowds out the correction.
_QUOTED_CHARS = 600


def build_prompt(emails: list[CoachEmail], today: date,
                 max_body_chars: int | None = None) -> str:
    """The user turn: today's date, then the mail grouped into threads.

    Threads are newest-first, but messages WITHIN a thread run oldest -> newest
    so the last thing the model reads about a topic is the latest word on it.
    That ordering is what makes a correction land."""
    max_body_chars = (max_body_chars if max_body_chars is not None
                      else config.COACH_MAX_BODY_CHARS)
    threads = group_threads(emails)

    parts = [
        f"Today's date is {today.isoformat()}.",
        "",
        f"Here are {len(emails)} email(s), grouped into {len(threads)} "
        "conversation(s), newest conversation first. Within a conversation the "
        "messages run oldest to newest, so the LAST message is the most recent "
        "word on that topic.",
    ]
    for index, thread in enumerate(threads, 1):
        # Title the thread by its earliest subject, minus the Re:/Fwd: noise.
        title = thread[0].subject or "(no subject)"
        parts += ["", f"=== Conversation {index}: {title} "
                      f"({len(thread)} message{'s' if len(thread) != 1 else ''}) ==="]
        for position, mail in enumerate(thread, 1):
            new_text, quoted = split_reply_quote(mail.body)
            header = f"message {position} of {len(thread)}"
            if position > 1:
                header += " — LATER REPLY: updates or corrects the message(s) above"
            parts += [
                "",
                f"--- {header} ---",
                f"From: {mail.sender_name} <{mail.sender}>",
                f"Date: {_iso_z(mail.sent_at) or 'unknown'}",
                f"Subject: {mail.subject}",
                "",
                new_text[:max_body_chars],
            ]
            if quoted and position == 1:
                # Only worth showing when the quoted material isn't otherwise in
                # the prompt. If earlier messages of this thread are printed
                # above (position > 1), the quote is a verbatim copy of them —
                # keeping it just gives the model a second, STALE statement of
                # the fact the reply is correcting, and it sometimes picks that
                # one. Measured on real mail: dropping it is what makes a
                # terse correction ("Media Day is the 25th!") win reliably.
                parts += [
                    "",
                    "[text quoted from an earlier message — historical context "
                    "only; do not treat it as new or current information]",
                    quoted[:_QUOTED_CHARS],
                ]
            elif quoted:
                parts += ["", "[the quoted copy of the earlier message(s) is "
                              "omitted — they appear in full above]"]
    return "\n".join(parts)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_model_reply(text: str) -> dict | None:
    """Pull the digest out of the model's reply, defensively.

    Strips a ```json fence, then takes the outermost {...} before decoding.
    Returns None if it can't be read as the expected shape — the caller then
    keeps the previous digest rather than overwriting it with nothing."""
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    def string_list(value) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    headline = data.get("headline")
    return {
        "headline": headline.strip() if isinstance(headline, str) else "",
        "bullets": string_list(data.get("bullets")),
        "actions": string_list(data.get("actions")),
    }


_URL_RE = re.compile(r"https?://\S+")


def _scrub(text: str, limit: int = 160) -> str:
    """Make a provider error safe to show an athlete.

    Errors from OpenRouter can embed a key-management URL containing a key
    identifier; that belongs in the server log, not on a runner's phone."""
    return _URL_RE.sub("[url]", text or "").strip()[:limit]


def _post_completion(model: str, system: str, prompt: str) -> str:
    """One OpenRouter chat completion. Raises MailboxError on any failure.

    OpenRouter speaks the OpenAI chat-completions shape, so this is a plain HTTP
    POST — no provider SDK needed."""
    body = {
        "model": model,
        # Low temperature: this is extraction, not creative writing.
        "temperature": 0.2,
        # Must leave room for reasoning tokens even though we asked for none —
        # some providers ignore reasoning.enabled=false (see config).
        "max_tokens": config.COACH_DIGEST_MAX_TOKENS,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    if config.COACH_DIGEST_REASONING != "on":
        # Off by default: on a thinking model, max_tokens covers thinking AND
        # the answer, so a long window can spend the whole budget thinking and
        # return empty content (which we'd then treat as a failed model).
        body["reasoning"] = {"enabled": False}
    provider: dict = {}
    if config.COACH_DIGEST_DATA_COLLECTION:
        # Route only to providers matching this data-retention policy. The
        # prompt is students' coach email, so "deny" (providers that don't
        # collect prompts) is the default.
        provider["data_collection"] = config.COACH_DIGEST_DATA_COLLECTION
    if config.COACH_DIGEST_IGNORE_PROVIDERS:
        # Some providers ignore reasoning.enabled=false and think anyway, which
        # returns empty content (see config).
        provider["ignore"] = config.COACH_DIGEST_IGNORE_PROVIDERS
    if provider:
        body["provider"] = provider

    try:
        response = requests.post(
            f"{config.OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                # Optional attribution headers OpenRouter shows in its rankings.
                "HTTP-Referer": "https://github.com/chadwick-xc/xc_server",
                "X-Title": "Chadwick XC Training",
            },
            json=body,
            timeout=config.COACH_DIGEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        raise MailboxError(f"could not reach OpenRouter ({type(e).__name__})")

    if response.status_code == 401:
        raise MailboxError("OpenRouter rejected the API key (check OPENROUTER_API_KEY)")
    if response.status_code == 402:
        # Paid models stop here when the account balance hits zero; the free
        # entry later in COACH_DIGEST_MODELS is what keeps the digest alive.
        raise MailboxError("out of OpenRouter credit (paid model)")
    if response.status_code == 403 and "limit" in response.text.lower():
        # The key itself carries a spend cap, separate from the account balance.
        raise MailboxError("this API key's spend limit is used up or set to $0")
    if response.status_code == 429:
        raise MailboxError("rate-limited")
    if response.status_code == 404 and "data policy" in response.text.lower():
        # COACH_DIGEST_DATA_COLLECTION=deny can leave a model with no eligible
        # provider at all. Confusing as a bare 404, so name the cause.
        raise MailboxError(
            "no provider for this model meets COACH_DIGEST_DATA_COLLECTION="
            f"{config.COACH_DIGEST_DATA_COLLECTION}")
    if response.status_code >= 400:
        # The body explains what's wrong (bad model id, model offline). Scrubbed
        # and truncated: this text reaches the athletes' phones, and provider
        # errors like to embed dashboard URLs containing key identifiers.
        raise MailboxError(f"HTTP {response.status_code}: {_scrub(response.text)}")

    try:
        body = response.json()
    except ValueError:
        raise MailboxError("OpenRouter returned a non-JSON body")
    if "choices" not in body:
        # OpenRouter also reports some failures as a 200 with an error object.
        detail = str(body.get("error", body))[:200]
        raise MailboxError(f"no completion returned: {detail}")
    try:
        content = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise MailboxError("unexpected response shape from OpenRouter")
    if not content.strip():
        # Reasoning-style models sometimes put everything in a separate field and
        # leave content empty. Treat it as a failure so the next model is tried.
        raise MailboxError("returned an empty message")
    return content


def _call_model(system: str, prompt: str) -> str:
    """Summarize via OpenRouter, trying each configured model in turn.

    Free endpoints are rate-limited and periodically busy, so one model being
    unavailable must not cost the team its digest. Isolated behind this function
    so tests can inject a fake in its place."""
    if not config.OPENROUTER_API_KEY:
        raise MailboxError(
            "The summarizer is not configured (OPENROUTER_API_KEY is unset). "
            "Get a free key at https://openrouter.ai/keys.")
    if not config.COACH_DIGEST_MODELS:
        raise MailboxError("No summarizer model configured (COACH_DIGEST_MODELS is empty).")

    failures = []
    for model in config.COACH_DIGEST_MODELS:
        try:
            return _post_completion(model, system, prompt)
        except MailboxError as e:
            failures.append(f"{model} — {e}")
            log.warning("coach digest: %s failed (%s); trying the next model", model, e)
    raise MailboxError("Every summarizer model failed. " + "; ".join(failures))


def summarize(emails: list[CoachEmail], *, call_model=None,
              today: date | None = None) -> dict | None:
    """Summarize the window. None means "unreadable reply — keep what we have"."""
    call_model = call_model or _call_model
    prompt = build_prompt(emails, today or _utcnow().date())
    return parse_model_reply(call_model(SUMMARY_PROMPT, prompt))


# --- Mailbox lookup / provisioning ---------------------------------------------

def team_mailbox(db: Session) -> CoachMailbox | None:
    """The shared mailbox every athlete's digest comes from (athlete_id NULL)."""
    return db.scalars(
        select(CoachMailbox).where(CoachMailbox.athlete_id.is_(None))
        .order_by(CoachMailbox.id).limit(1)).first()


def mailbox_for_athlete(db: Session, athlete_id: int) -> CoachMailbox | None:
    """Which mailbox this athlete reads: their own if one exists, else the team
    mailbox. Returns None when this deployment has no mailbox at all — the
    endpoints turn that into the 501 "not implemented" signal."""
    own = db.scalars(select(CoachMailbox)
                     .where(CoachMailbox.athlete_id == athlete_id)).first()
    return own or team_mailbox(db)


def sync_team_mailbox_from_env(db: Session) -> CoachMailbox | None:
    """Make the DB row match the .env config at startup.

    The environment is the source of truth; the row exists so the poller and a
    future per-athlete mailbox read the same way, and so the password is stored
    encrypted rather than in a second plaintext place."""
    if not config.coach_digest_configured():
        return None
    mailbox = team_mailbox(db)
    if mailbox is None:
        mailbox = CoachMailbox(athlete_id=None, imap_host="", imap_port=993,
                               imap_username="", imap_password_encrypted="")
        db.add(mailbox)
    mailbox.imap_host = config.COACH_IMAP_HOST
    mailbox.imap_port = config.COACH_IMAP_PORT
    mailbox.imap_username = config.COACH_IMAP_USERNAME
    mailbox.imap_password_encrypted = encrypt_secret(config.COACH_IMAP_PASSWORD)
    mailbox.sender_filter = ",".join(config.COACH_SENDERS) or None
    db.commit()
    db.refresh(mailbox)
    return mailbox


# --- Poll + store ---------------------------------------------------------------

# One poll per mailbox at a time. Without this, two athletes tapping
# "Re-summarize" together would open two IMAP sessions and pay for two
# identical model calls.
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()

# If a poll just finished, a refresh that was queued behind it returns that
# result instead of immediately polling again.
_FRESH_SECONDS = 10


def _lock_for(mailbox_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(mailbox_id, threading.Lock())


def _store_messages(db: Session, mailbox: CoachMailbox,
                    emails: list[CoachEmail]) -> None:
    """Replace the stored window with what the mailbox just returned.

    Deleting what's no longer in the window keeps `messages` in the response
    equal to what the digest was built from, and stops the table growing for
    ever."""
    keep = {mail.message_id for mail in emails}
    existing = {row.message_id: row for row in db.scalars(
        select(CoachMessage).where(CoachMessage.mailbox_id == mailbox.id))}

    for mail in emails:
        row = existing.get(mail.message_id)
        if row is None:
            row = CoachMessage(mailbox_id=mailbox.id, message_id=mail.message_id)
            db.add(row)
        row.sender = mail.sender
        row.sender_name = mail.sender_name
        row.subject = mail.subject
        row.sent_at = mail.sent_at
        row.body = mail.body

    stale = [mid for mid in existing if mid not in keep]
    if stale:
        db.execute(delete(CoachMessage).where(
            CoachMessage.mailbox_id == mailbox.id,
            CoachMessage.message_id.in_(stale)))


def poll_mailbox(db: Session, mailbox: CoachMailbox, *, force: bool = False,
                 imap_factory=None, call_model=None) -> str:
    """Poll, store, and re-summarize if the window changed.

    Returns what happened, for logs/tests: "summarized", "unchanged",
    "unreadable-reply", "empty", or "already-fresh".

    `force=True` (POST /coach-digest/refresh) skips the did-anything-change
    check and always re-summarizes. The background poller leaves it False, so a
    quiet inbox costs an IMAP fetch and no model call."""
    lock = _lock_for(mailbox.id)
    with lock:
        db.refresh(mailbox)
        if (mailbox.last_polled_at is not None
                and (_utcnow() - mailbox.last_polled_at).total_seconds() < _FRESH_SECONDS):
            # Another request polled while we waited on the lock.
            return "already-fresh"

        emails = fetch_recent(mailbox, imap_factory=imap_factory)
        _store_messages(db, mailbox, emails)
        mailbox.last_polled_at = _utcnow()

        digest = db.get(CoachDigest, mailbox.id)
        window_ids = sorted(mail.message_id for mail in emails)

        if not emails:
            # Nothing in the window any more (the season ended, or the mail aged
            # out). Drop the digest too: _store_messages just deleted the mail it
            # described, and a digest with `source_count: 3` alongside an empty
            # `messages` list is an inconsistent payload for a client we can't
            # change. The app falls back to "No coach email yet".
            if digest is not None:
                db.delete(digest)
            db.commit()
            return "empty"

        # The cheap "did anything change?" check: same set of Message-IDs means
        # the same mail, so don't call (and pay for) the model again.
        if not force and digest is not None and list(digest.source_ids or []) == window_ids:
            db.commit()
            return "unchanged"

        summary = summarize(emails, call_model=call_model)
        if summary is None:
            # Unreadable model reply: keep the previous digest rather than
            # overwriting a good summary with nothing.
            db.commit()
            log.warning("coach digest: model reply could not be parsed; keeping "
                        "the previous digest for mailbox %s", mailbox.id)
            return "unreadable-reply"

        if digest is None:
            digest = CoachDigest(mailbox_id=mailbox.id)
            db.add(digest)
        digest.headline = summary["headline"]
        digest.bullets = summary["bullets"]
        digest.actions = summary["actions"]
        digest.generated_at = _utcnow()
        digest.source_ids = window_ids
        db.commit()
        return "summarized"


# --- Response building ----------------------------------------------------------

def digest_response(db: Session, mailbox: CoachMailbox) -> dict:
    """Build the response both endpoints return. Pure read — no poll, no model.

    Timestamps are already-formatted ISO-8601 Z strings (see _iso_z)."""
    digest = db.get(CoachDigest, mailbox.id)
    messages = db.scalars(
        select(CoachMessage)
        .where(CoachMessage.mailbox_id == mailbox.id)
        # Newest first; NULL sent_at (undated mail) sorts last.
        .order_by(CoachMessage.sent_at.is_(None), CoachMessage.sent_at.desc())
        .limit(config.COACH_MAX_MESSAGES)).all()

    payload: dict = {"digest": None, "messages": [
        {
            "id": row.message_id,
            "from": row.sender,
            "from_name": row.sender_name,
            "subject": row.subject,
            "date": _iso_z(row.sent_at),
            "body": row.body or "",
        }
        for row in messages
    ]}

    if digest is not None:
        headline = digest.headline or ""
        bullets = list(digest.bullets or [])
        actions = list(digest.actions or [])
        # A summary with nothing in it means the model found nothing useful.
        # Serve it as "no digest yet" so the app shows its empty state instead
        # of a blank card — the row still records source_ids, so we don't pay
        # for the same summarization again.
        if headline or bullets or actions:
            payload["digest"] = {
                "headline": headline,
                "bullets": bullets,
                "actions": actions,
                "generated_at": _iso_z(digest.generated_at),
                "source_count": len(digest.source_ids or []),
            }
    return payload


# --- Background poller ----------------------------------------------------------

def poll_once() -> str | None:
    """One background poll, on its own DB session. Never raises."""
    db = SessionLocal()
    try:
        mailbox = team_mailbox(db)
        if mailbox is None:
            return None
        outcome = poll_mailbox(db, mailbox)
        log.info("coach digest poll: %s", outcome)
        return outcome
    except MailboxError as e:
        log.warning("coach digest poll failed: %s", e)
        return None
    except Exception:
        log.exception("coach digest poll crashed")
        return None
    finally:
        db.close()


async def run_poller(interval_minutes: int | None = None,
                     initial_delay_seconds: float = 5.0) -> None:
    """Background job: poll the mailbox every COACH_POLL_INTERVAL_MINUTES.

    Started from main.lifespan. Each poll runs in a worker thread because
    imaplib is blocking and would otherwise stall the event loop. Assumes a
    single server process — with several uvicorn workers each would poll (still
    correct, just redundant IMAP fetches)."""
    import asyncio

    interval = (interval_minutes if interval_minutes is not None
                else config.COACH_POLL_INTERVAL_MINUTES)
    await asyncio.sleep(initial_delay_seconds)  # let startup finish first
    while True:
        await asyncio.to_thread(poll_once)
        await asyncio.sleep(max(60, interval * 60))
