"""Tests for the coach email digest.

Run with:  venv/bin/python -m pytest tests/ -q
(needs requirements-dev.txt installed)

Nothing here touches the real xc_training.db or the network: every test uses a
temp SQLite file, a fake IMAP server, and a fake model call.
"""
import os

# Must be set before importing config — it raises at import time without a
# JWT_SECRET, and coach_digest_configured() reads these at import too.
os.environ.setdefault("JWT_SECRET", "a" * 64)
os.environ["DEV_MODE"] = "false"
os.environ["COACH_IMAP_HOST"] = "imap.example.com"
os.environ["COACH_IMAP_PORT"] = "993"
os.environ["COACH_IMAP_USERNAME"] = "chadwickxc.mail@example.com"
os.environ["COACH_IMAP_PASSWORD"] = "super-secret-app-password"
os.environ["COACH_SENDERS"] = "coach@school.edu"
os.environ["OPENROUTER_API_KEY"] = "test-key-not-used"

import imaplib  # noqa: E402
import json  # noqa: E402
import locale  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402
from email.message import EmailMessage  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import coach_digest  # noqa: E402
import config  # noqa: E402
import main  # noqa: E402
from auth import create_access_token  # noqa: E402
from database import Base, get_db  # noqa: E402
from models import Athlete, CoachDigest, CoachMailbox, CoachMessage  # noqa: E402


# --- Fixtures ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_freshness_window(monkeypatch):
    """poll_mailbox skips a poll that lands within seconds of the previous one
    (so two athletes tapping Re-summarize together don't pay twice). Tests poll
    back-to-back on purpose, so switch that off — except where it's the subject
    of the test."""
    monkeypatch.setattr(coach_digest, "_FRESH_SECONDS", 0)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'test.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db):
    """TestClient wired to the temp DB. Deliberately not used as a context
    manager: that would run the lifespan, which creates the real xc_training.db
    and starts the background poller."""
    main.app.dependency_overrides[get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def make_athlete(db, name="Runner", role="athlete") -> Athlete:
    athlete = Athlete(name=name, email=f"{name.lower()}@example.com", role=role)
    db.add(athlete)
    db.commit()
    db.refresh(athlete)
    return athlete


def auth(athlete: Athlete) -> dict:
    return {"Authorization": f"Bearer {create_access_token(athlete.id)}"}


def make_mailbox(db, athlete_id=None) -> CoachMailbox:
    mailbox = CoachMailbox(
        athlete_id=athlete_id,
        imap_host="imap.example.com", imap_port=993,
        imap_username="team@example.com",
        imap_password_encrypted=coach_digest.encrypt_secret("app-password"),
        sender_filter="coach@school.edu")
    db.add(mailbox)
    db.commit()
    db.refresh(mailbox)
    return mailbox


# --- Fake IMAP + fake model ----------------------------------------------------

def raw_email(message_id, *, sender="Coach Kim <coach@school.edu>",
              subject="Saturday meet — time change", body="Bus leaves at 7:15.",
              when="Sun, 09 Aug 2026 18:00:00 +0000", html=None) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = sender
    msg["To"] = "team@example.com"
    msg["Subject"] = subject
    msg["Date"] = when
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


class FakeIMAP:
    """Records what the poller asked for, so the tests can assert on it."""

    def __init__(self, messages, *, fail_login=False):
        self.messages = messages          # list[bytes], oldest first
        self.fail_login = fail_login
        self.fetch_specs: list[str] = []
        self.search_criteria: list[str] = []
        self.select_calls: list[tuple] = []
        self.logged_out = False

    def __call__(self, host, port):       # used as the imap_factory
        self.host, self.port = host, port
        return self

    def login(self, username, password):
        if self.fail_login:
            raise imaplib.IMAP4.error(
                b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.credentials = (username, password)
        return ("OK", [b"logged in"])

    def select(self, folder, readonly=False):
        self.select_calls.append((folder, readonly))
        return ("OK", [str(len(self.messages)).encode()])

    def uid(self, command, *args):
        if command == "SEARCH":
            self.search_criteria.append(" ".join(a for a in args[1:]))
            uids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
            return ("OK", [uids])
        if command == "FETCH":
            uids, spec = args[0], args[1]
            self.fetch_specs.append(spec)
            # The real client batches the whole window into one FETCH, so uids
            # arrives comma-joined and the reply interleaves one tuple per
            # message with a b")" between them.
            payload = []
            for raw_uid in bytes(uids).split(b","):
                index = int(raw_uid) - 1
                payload.append((b"%d (BODY[] {%d}" % (index + 1,
                                                      len(self.messages[index])),
                                self.messages[index]))
                payload.append(b")")
            return ("OK", payload)
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.logged_out = True


class FakeModel:
    """Stands in for the summarizer call and counts how often it ran."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[str] = []

    def __call__(self, system, prompt):
        self.calls.append(prompt)
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]


GOOD_REPLY = json.dumps({
    "headline": "Saturday's meet moved to 9am — bus leaves school at 7:15",
    "bullets": ["Tuesday: 6x800m at the track, 5pm", "Wednesday: easy 4 miles"],
    "actions": ["Turn in the travel permission form by Thursday"],
})


# --- The four required behaviours ----------------------------------------------

def test_get_never_polls_or_summarizes(db, client, monkeypatch):
    """GET must be cheap and side-effect-free: no IMAP, no model call."""
    mailbox = make_mailbox(db)
    db.add(CoachMessage(mailbox_id=mailbox.id, message_id="<a@mail>",
                        sender="coach@school.edu", sender_name="Coach Kim",
                        subject="Meet", sent_at=datetime(2026, 8, 9, 18, 0),
                        body="Bus at 7:15."))
    db.add(CoachDigest(mailbox_id=mailbox.id, headline="Meet moved to 9am",
                       bullets=["Bus at 7:15"], actions=[],
                       generated_at=datetime(2026, 8, 10, 13, 0),
                       source_ids=["<a@mail>"]))
    db.commit()

    def explode(*a, **k):
        raise AssertionError("GET /coach-digest must not poll or summarize")

    monkeypatch.setattr(coach_digest, "fetch_recent", explode)
    monkeypatch.setattr(coach_digest, "_call_model", explode)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", explode)

    athlete = make_athlete(db)
    response = client.get("/coach-digest", headers=auth(athlete))

    assert response.status_code == 200
    body = response.json()
    assert body["digest"]["headline"] == "Meet moved to 9am"
    assert body["digest"]["source_count"] == 1
    assert body["messages"][0]["id"] == "<a@mail>"
    # last_polled_at untouched — GET really did not poll.
    db.refresh(mailbox)
    assert mailbox.last_polled_at is None


def test_unchanged_inbox_does_not_call_the_model_twice(db):
    """Same set of Message-IDs = same mail, so the model runs once, not twice.
    (Keying this on IMAP sequence numbers would re-bill on every poll, because
    they shift whenever new mail arrives.)"""
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>"), raw_email("<b@mail>")])
    model = FakeModel(GOOD_REPLY)

    first = coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)
    second = coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)

    assert (first, second) == ("summarized", "unchanged")
    assert len(model.calls) == 1
    # It still polled the mailbox both times — only the summarization is skipped.
    assert len(imap.select_calls) == 2

    # A new message changes the id set, so the model runs again.
    imap.messages.append(raw_email("<c@mail>", subject="Practice moved"))
    assert coach_digest.poll_mailbox(db, mailbox, imap_factory=imap,
                                     call_model=model) == "summarized"
    assert len(model.calls) == 2


def test_refresh_bypasses_the_unchanged_check(db):
    """The Re-summarize button must always re-summarize."""
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>")])
    model = FakeModel(GOOD_REPLY)

    coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)
    outcome = coach_digest.poll_mailbox(db, mailbox, force=True,
                                        imap_factory=imap, call_model=model)

    assert outcome == "summarized"
    assert len(model.calls) == 2


def test_malformed_model_reply_keeps_the_previous_digest(db):
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>")])
    model = FakeModel(GOOD_REPLY, "I'm sorry, I can't produce that. <not json>")

    coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)
    before = db.get(CoachDigest, mailbox.id)
    kept = (before.headline, list(before.bullets), before.generated_at,
            list(before.source_ids))

    imap.messages.append(raw_email("<b@mail>", subject="New info"))
    outcome = coach_digest.poll_mailbox(db, mailbox, imap_factory=imap,
                                        call_model=model)

    assert outcome == "unreadable-reply"
    after = db.get(CoachDigest, mailbox.id)
    assert (after.headline, list(after.bullets), after.generated_at,
            list(after.source_ids)) == kept
    # The new message is still stored, so the next successful poll summarizes it.
    assert db.get(CoachMessage, (mailbox.id, "<b@mail>")) is not None


def test_one_athlete_cannot_read_anothers_digest(db, client):
    """Per-athlete mailboxes stay separated, and the token decides — not a
    query parameter."""
    alice, bob = make_athlete(db, "Alice"), make_athlete(db, "Bob")
    for athlete, headline in ((alice, "Alice only"), (bob, "Bob only")):
        mailbox = make_mailbox(db, athlete_id=athlete.id)
        db.add(CoachDigest(mailbox_id=mailbox.id, headline=headline,
                           bullets=[], actions=[],
                           generated_at=datetime(2026, 8, 10, 13, 0),
                           source_ids=[f"<{headline}>"]))
        db.add(CoachMessage(mailbox_id=mailbox.id, message_id=f"<{headline}>",
                            sender="coach@school.edu", sender_name="Coach",
                            subject=headline, sent_at=datetime(2026, 8, 9),
                            body=headline))
    db.commit()

    for athlete, expected in ((alice, "Alice only"), (bob, "Bob only")):
        body = client.get("/coach-digest", headers=auth(athlete)).json()
        assert body["digest"]["headline"] == expected
        assert [m["subject"] for m in body["messages"]] == [expected]

    # There is no athlete_id parameter to abuse: it's ignored, not honoured.
    body = client.get("/coach-digest?athlete_id=%d" % bob.id,
                      headers=auth(alice)).json()
    assert body["digest"]["headline"] == "Alice only"


def test_shared_team_mailbox_serves_every_athlete(db, client):
    """The configured model for this team: one mailbox, one digest, everyone
    reads it."""
    mailbox = make_mailbox(db, athlete_id=None)
    db.add(CoachDigest(mailbox_id=mailbox.id, headline="Team-wide",
                       bullets=[], actions=[],
                       generated_at=datetime(2026, 8, 10, 13, 0),
                       source_ids=["<x@mail>"]))
    db.commit()
    alice, bob = make_athlete(db, "Alice"), make_athlete(db, "Bob")

    for athlete in (alice, bob):
        body = client.get("/coach-digest", headers=auth(athlete)).json()
        assert body["digest"]["headline"] == "Team-wide"


# --- IMAP behaviour ------------------------------------------------------------

def test_fetch_uses_body_peek_and_opens_the_folder_read_only(db):
    """BODY[] would set \\Seen and silently mark the coach's mail as read."""
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>")])

    coach_digest.fetch_recent(mailbox, imap_factory=imap)

    assert imap.fetch_specs == ["(BODY.PEEK[])"]
    assert all("BODY[" not in spec.replace("BODY.PEEK[", "")
               for spec in imap.fetch_specs)
    assert imap.select_calls == [("INBOX", True)]
    assert imap.logged_out


def test_imap_since_uses_english_months_regardless_of_locale():
    """A locale-aware formatter emits localized month names and the search
    silently returns nothing."""
    assert coach_digest.imap_since(date(2026, 8, 5)) == "05-Aug-2026"
    assert coach_digest.imap_since(date(2026, 12, 31)) == "31-Dec-2026"

    previous = locale.setlocale(locale.LC_TIME)
    try:
        try:
            locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
        except locale.Error:
            pytest.skip("no German locale installed to test against")
        assert coach_digest.imap_since(date(2026, 3, 1)) == "01-Mar-2026"
    finally:
        locale.setlocale(locale.LC_TIME, previous)


def test_search_window_is_a_since_criterion(db):
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>")])

    coach_digest.fetch_recent(mailbox, imap_factory=imap, window_days=14)

    criteria = imap.search_criteria[0]
    assert criteria.startswith("SINCE ")
    expected = coach_digest.imap_since(
        (datetime.now(timezone.utc) - timedelta(days=14)).date())
    assert criteria == f"SINCE {expected}"


def test_the_whole_window_is_fetched_in_one_command(db):
    """Gmail charges ~3s of latency per FETCH command almost regardless of
    message size, so per-message fetching made the poll scale with mail volume
    and would blow the client's 90s refresh timeout on a full window."""
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email(f"<{i}@mail>") for i in range(12)])

    emails = coach_digest.fetch_recent(mailbox, imap_factory=imap)

    assert len(emails) == 12
    assert len(imap.fetch_specs) == 1        # one command, not twelve
    assert imap.fetch_specs == ["(BODY.PEEK[])"]


def test_messages_come_back_newest_first_whatever_the_server_order(db):
    """A batched FETCH makes no ordering promise, and the model is told to
    prefer the newest email when two disagree."""
    mailbox = make_mailbox(db)
    imap = FakeIMAP([
        raw_email("<mid@mail>", when="Sun, 09 Aug 2026 12:00:00 +0000"),
        raw_email("<newest@mail>", when="Mon, 10 Aug 2026 12:00:00 +0000"),
        raw_email("<oldest@mail>", when="Sat, 08 Aug 2026 12:00:00 +0000"),
    ])

    ids = [e.message_id for e in coach_digest.fetch_recent(mailbox, imap_factory=imap)]

    assert ids == ["<newest@mail>", "<mid@mail>", "<oldest@mail>"]


def test_message_cap_keeps_the_newest(db):
    mailbox = make_mailbox(db)
    # Distinct, increasing dates — otherwise "newest" is ambiguous and the
    # assertion would only be checking the fetch order.
    imap = FakeIMAP([raw_email(f"<{i}@mail>",
                               when=f"Sun, {i + 1:02d} Aug 2026 12:00:00 +0000")
                     for i in range(10)])

    emails = coach_digest.fetch_recent(mailbox, imap_factory=imap, max_messages=3)

    # SEARCH returns oldest-first, so the cap keeps the last three uids, and
    # they come back newest-first.
    assert [e.message_id for e in emails] == ["<9@mail>", "<8@mail>", "<7@mail>"]


def test_authentication_failure_is_explained_without_leaking_the_password(db):
    mailbox = make_mailbox(db)
    imap = FakeIMAP([], fail_login=True)

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest.fetch_recent(mailbox, imap_factory=imap)

    message = str(excinfo.value)
    assert "IMAP" in message and "App Password" in message
    assert "app-password" not in message


# --- Parsing -------------------------------------------------------------------

def test_html_mail_is_flattened_to_plain_text(db):
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email(
        "<a@mail>", body="", html="<html><head><style>p{color:red}</style></head>"
        "<body><p>Meet is at <b>9am</b>.</p><p>Bring&nbsp;spikes &amp; trainers.</p>"
        "</body></html>")])

    body = coach_digest.fetch_recent(mailbox, imap_factory=imap)[0].body

    assert "<" not in body and ">" not in body
    assert "color:red" not in body
    assert "Meet is at 9am." in body
    assert "Bring spikes & trainers." in body


def test_sender_filter_matches_header_then_falls_back_to_the_forward_block(db):
    """Gmail auto-forwarding keeps the original From:; the Forward button does
    not — the real sender is only in the quoted block."""
    mailbox = make_mailbox(db)
    hand_forwarded = (
        "---------- Forwarded message ----------\n"
        "From: Coach Kim <coach@school.edu>\n"
        "Date: Sun, 9 Aug 2026\n"
        "Subject: Meet time\n\nBus leaves at 7:15.")
    imap = FakeIMAP([
        raw_email("<direct@mail>", sender="Coach Kim <coach@school.edu>"),
        raw_email("<forwarded@mail>", sender="Runner <runner@gmail.com>",
                  body=hand_forwarded),
        raw_email("<spam@mail>", sender="Deals <deals@shoes.example>",
                  body="50% off racing flats"),
    ])

    ids = [e.message_id for e in coach_digest.fetch_recent(mailbox, imap_factory=imap)]

    assert "<direct@mail>" in ids
    assert "<forwarded@mail>" in ids
    assert "<spam@mail>" not in ids


def test_domain_filter_and_empty_filter():
    assert coach_digest.sender_matches(["@school.edu"], "anyone@school.edu", "")
    assert not coach_digest.sender_matches(["@school.edu"], "x@other.com", "")
    # No filter configured = summarize everything in the mailbox.
    assert coach_digest.sender_matches([], "anyone@anywhere.com", "")


def test_message_without_a_message_id_gets_a_stable_synthetic_one():
    msg = EmailMessage()
    msg["From"] = "Coach Kim <coach@school.edu>"
    msg["Subject"] = "No id"
    msg["Date"] = "Sun, 09 Aug 2026 18:00:00 +0000"
    msg.set_content("Practice at 5.")
    raw = msg.as_bytes()

    first, second = coach_digest.parse_message(raw), coach_digest.parse_message(raw)

    assert first.message_id == second.message_id  # stable across polls
    assert first.message_id.endswith("@xc-server>")


def test_parse_model_reply_handles_fences_and_junk():
    fenced = "```json\n{\"headline\": \"Hi\", \"bullets\": [\"a\"]}\n```"
    assert coach_digest.parse_model_reply(fenced) == {
        "headline": "Hi", "bullets": ["a"], "actions": []}

    chatty = 'Sure! {"headline": "Hi", "bullets": [], "actions": []} Hope that helps.'
    assert coach_digest.parse_model_reply(chatty)["headline"] == "Hi"

    assert coach_digest.parse_model_reply("no json here") is None
    assert coach_digest.parse_model_reply("") is None
    assert coach_digest.parse_model_reply("{broken") is None
    # Wrong types don't crash — they degrade to empty lists.
    assert coach_digest.parse_model_reply(
        '{"headline": 5, "bullets": "nope"}') == {
            "headline": "", "bullets": [], "actions": []}


# --- Replies, corrections, and threading ---------------------------------------

def mail(mid, subject, body, day, *, in_reply_to=None, references=()):
    return coach_digest.CoachEmail(
        mid, "coach@school.edu", "Coach Kim", subject,
        datetime(2026, 8, day, 12, 0), body,
        in_reply_to=in_reply_to, references=list(references))


def test_reply_is_threaded_with_the_message_it_answers():
    original = mail("<a@mail>", "Saturday meet", "Bus leaves at 7:15.", 9)
    reply = mail("<b@mail>", "Re: Saturday meet", "Correction: bus at 7:00.", 10,
                 in_reply_to="<a@mail>")

    threads = coach_digest.group_threads([reply, original])

    assert len(threads) == 1
    # Oldest first, so the correction is the last thing read.
    assert [m.message_id for m in threads[0]] == ["<a@mail>", "<b@mail>"]


def test_threading_also_works_off_the_references_chain():
    a = mail("<a@mail>", "Meet", "original", 9)
    b = mail("<b@mail>", "Re: Meet", "reply", 10, references=["<a@mail>"])
    c = mail("<c@mail>", "Re: Meet", "reply 2", 11,
             references=["<a@mail>", "<b@mail>"])

    threads = coach_digest.group_threads([c, a, b])

    assert len(threads) == 1
    assert [m.message_id for m in threads[0]] == ["<a@mail>", "<b@mail>", "<c@mail>"]


def test_subject_groups_a_reply_that_lost_its_headers():
    """Forwarding strips In-Reply-To often enough that subject has to work."""
    original = mail("<a@mail>", "Saturday meet", "Bus at 7:15.", 9)
    reply = mail("<b@mail>", "Re: Saturday meet", "Bus at 7:00 actually.", 10)

    threads = coach_digest.group_threads([reply, original])

    assert len(threads) == 1
    assert [m.message_id for m in threads[0]] == ["<a@mail>", "<b@mail>"]


def test_same_subject_without_a_reply_marker_is_not_merged():
    """Two unrelated "Practice update" emails must not be threaded, or the older
    one gets treated as superseded and its content silently dropped."""
    first = mail("<a@mail>", "Practice update", "Monday: 5 miles.", 3)
    second = mail("<b@mail>", "Practice update", "Tuesday: track.", 10)

    threads = coach_digest.group_threads([second, first])

    assert len(threads) == 2


def test_threads_are_ordered_newest_conversation_first():
    old = mail("<old@mail>", "Uniforms", "pickup Friday", 2)
    new = mail("<new@mail>", "Meet", "9am start", 12)

    threads = coach_digest.group_threads([old, new])

    assert [t[0].message_id for t in threads] == ["<new@mail>", "<old@mail>"]


def test_reply_quote_is_split_off_from_the_new_text():
    body = ("Correction: the bus leaves at 7:00, not 7:15.\n\n"
            "On Mon, 10 Aug 2026 at 09:14, Coach Kim <coach@school.edu> wrote:\n"
            "> Bus leaves at 7:15 sharp.\n> Bring both pairs of shoes.\n")

    new_text, quoted = coach_digest.split_reply_quote(body)

    assert new_text == "Correction: the bus leaves at 7:00, not 7:15."
    # The quoted original is kept, but out of the new text: nothing from the
    # quote leaks in, and the "On ... wrote:" attribution line goes with it.
    assert "Bring both pairs of shoes." in quoted
    assert "Bring both pairs of shoes." not in new_text
    assert "wrote:" not in new_text


def test_a_wrapped_attribution_line_is_still_recognised():
    """Gmail wraps "On <date>, <name> <addr>\\nwrote:" across two lines; a
    single-line pattern misses it and leaves it in the new text."""
    body = ("Sorry! Media Day is the 25th!\n\n"
            "On Sun, Aug 16, 2026 at 10:47 AM George Ramos <gramos@school.edu>\n"
            "wrote:\n\n> Media Day is Tuesday the 18th.\n")

    new_text, quoted = coach_digest.split_reply_quote(body)

    assert new_text == "Sorry! Media Day is the 25th!"
    assert "wrote:" not in new_text
    assert "18th" in quoted


def test_a_forwarded_block_is_content_not_a_stale_quote():
    """The hand-forward case: the quoted block IS the coach's message."""
    body = ("FYI team\n\n"
            "---------- Forwarded message ---------\n"
            "From: Coach Kim <coach@school.edu>\n\n"
            "> Practice moved to 4pm.\n")

    new_text, quoted = coach_digest.split_reply_quote(body)

    assert quoted == ""                       # nothing discarded as history
    assert "Practice moved to 4pm." in new_text


def test_a_body_that_is_only_a_quote_is_kept_whole():
    body = "> Bus at 7:15.\n> Bring spikes.\n"
    new_text, quoted = coach_digest.split_reply_quote(body)
    assert quoted == "" and "7:15" in new_text


def test_prompt_puts_the_correction_last_and_drops_the_redundant_quote():
    """When the original is shown above in the same thread, the reply's quoted
    copy of it is dropped: keeping it hands the model a second, STALE statement
    of the fact being corrected, and it sometimes reports that one instead."""
    original = mail("<a@mail>", "Saturday meet", "Bus leaves at 7:15 sharp.", 9)
    reply = mail("<b@mail>", "Re: Saturday meet",
                 "Correction: bus leaves at 7:00.\n\n"
                 "On Sun, 9 Aug 2026, Coach Kim wrote:\n> Bus leaves at 7:15 sharp.\n",
                 10, in_reply_to="<a@mail>")

    prompt = coach_digest.build_prompt([reply, original], date(2026, 8, 11))

    assert "Conversation 1: Saturday meet (2 messages)" in prompt
    assert "LATER REPLY" in prompt
    # The correction must be read after the original.
    assert prompt.index("Bus leaves at 7:15 sharp.") < prompt.index("bus leaves at 7:00")
    # The stale time appears once (the original), not twice.
    assert prompt.count("7:15") == 1
    assert "omitted" in prompt


def test_a_quote_is_kept_when_the_original_is_not_in_the_window():
    """A lone reply whose parent aged out still needs its quoted context — but
    labelled as history, not as a current statement."""
    orphan = mail("<b@mail>", "Re: Saturday meet",
                  "Correction: bus leaves at 7:00.\n\n"
                  "On Sun, 9 Aug 2026, Coach Kim wrote:\n> Bus leaves at 7:15 sharp.\n",
                  10)

    prompt = coach_digest.build_prompt([orphan], date(2026, 8, 11))

    assert "historical context" in prompt
    assert "7:15" in prompt          # context preserved rather than lost
    assert prompt.index("bus leaves at 7:00") < prompt.index("historical context")


def test_prompt_carries_todays_date_and_newest_email_first(db):
    emails = [
        coach_digest.CoachEmail("<new@mail>", "coach@school.edu", "Coach Kim",
                                "Newest", datetime(2026, 8, 9, 18, 0), "new body"),
        coach_digest.CoachEmail("<old@mail>", "coach@school.edu", "Coach Kim",
                                "Older", datetime(2026, 8, 1, 18, 0), "old body"),
    ]
    prompt = coach_digest.build_prompt(emails, date(2026, 8, 10))

    assert "Today's date is 2026-08-10." in prompt
    assert prompt.index("Newest") < prompt.index("Older")
    assert "2026-08-09T18:00:00Z" in prompt


def test_bodies_are_truncated_for_the_model():
    email_obj = coach_digest.CoachEmail("<a@mail>", "coach@school.edu", "Coach",
                                        "Long", datetime(2026, 8, 9), "x" * 9000)
    prompt = coach_digest.build_prompt([email_obj], date(2026, 8, 10),
                                       max_body_chars=4000)
    assert "x" * 4000 in prompt
    assert "x" * 4001 not in prompt


# --- The summarizer (OpenRouter free tier) -------------------------------------

class FakeHTTPResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def completion(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakePost:
    """Records each request and replays queued responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "body": json,
                              "timeout": timeout})
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def test_openrouter_request_shape(monkeypatch):
    post = FakePost(FakeHTTPResponse(payload=completion(GOOD_REPLY)))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/model-a:free"])

    assert coach_digest._call_model("SYSTEM", "PROMPT") == GOOD_REPLY

    sent = post.requests[0]
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer test-key-not-used"
    assert sent["body"]["model"] == "vendor/model-a:free"
    assert [m["role"] for m in sent["body"]["messages"]] == ["system", "user"]
    assert sent["body"]["messages"][0]["content"] == "SYSTEM"
    assert sent["body"]["messages"][1]["content"] == "PROMPT"
    assert sent["timeout"] == config.COACH_DIGEST_TIMEOUT_SECONDS


def test_falls_back_to_the_next_free_model_when_one_is_rate_limited(monkeypatch):
    """Free endpoints are throttled and go busy — one being unavailable must not
    cost the team its digest."""
    post = FakePost(
        FakeHTTPResponse(status_code=429, text="rate limit exceeded"),
        FakeHTTPResponse(payload=completion(GOOD_REPLY)),
    )
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS",
                        ["vendor/busy:free", "vendor/spare:free"])

    assert coach_digest._call_model("s", "p") == GOOD_REPLY
    assert [r["body"]["model"] for r in post.requests] == ["vendor/busy:free",
                                                           "vendor/spare:free"]


def test_empty_or_malformed_completions_fall_through_to_the_next_model(monkeypatch):
    """A reasoning-style model can return an empty `content`; treat that as a
    failure rather than feeding "" to the parser."""
    post = FakePost(
        FakeHTTPResponse(payload=completion("   ")),          # empty content
        FakeHTTPResponse(payload={"error": {"message": "model offline"}}),
        FakeHTTPResponse(payload=completion(GOOD_REPLY)),
    )
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS",
                        ["vendor/a:free", "vendor/b:free", "vendor/c:free"])

    assert coach_digest._call_model("s", "p") == GOOD_REPLY
    assert len(post.requests) == 3


def test_every_model_failing_reports_each_one_without_leaking_the_key(monkeypatch):
    post = FakePost(
        FakeHTTPResponse(status_code=429, text="rate limit exceeded"),
        FakeHTTPResponse(status_code=502, text="upstream is down"),
    )
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS",
                        ["vendor/a:free", "vendor/b:free"])

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest._call_model("s", "p")

    message = str(excinfo.value)
    assert "vendor/a:free" in message and "vendor/b:free" in message
    assert "rate-limited" in message and "502" in message
    assert "test-key-not-used" not in message


def test_provider_errors_are_scrubbed_before_reaching_an_athlete(monkeypatch):
    """OpenRouter embeds a key-management URL (with a key identifier) in some
    errors. That belongs in the log, not on a runner's phone."""
    post = FakePost(FakeHTTPResponse(
        status_code=400,
        text='{"error":{"message":"Bad thing. Manage it using '
             'https://openrouter.ai/workspaces/default/keys/1ba024ad7aadde20"}}'))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/a:free"])

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest._call_model("s", "p")

    message = str(excinfo.value)
    assert "Bad thing" in message          # the useful part survives
    assert "https://" not in message       # the URL does not
    assert "1ba024ad" not in message


def test_a_key_spend_cap_is_named_as_such(monkeypatch):
    """A key with limit=$0 returns 403, which is otherwise easy to misread as a
    bad key."""
    post = FakePost(FakeHTTPResponse(
        status_code=403, text='{"error":{"message":"Key limit exceeded (total limit)."}}'))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/paid"])

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest._call_model("s", "p")
    assert "spend limit" in str(excinfo.value)


def test_data_policy_404_names_the_setting_that_caused_it(monkeypatch):
    """data_collection=deny can leave a model with no eligible provider."""
    post = FakePost(FakeHTTPResponse(
        status_code=404,
        text='{"error":{"message":"No endpoints found matching your data policy"}}'))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/a:free"])
    monkeypatch.setattr(config, "COACH_DIGEST_DATA_COLLECTION", "deny")

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest._call_model("s", "p")
    assert "COACH_DIGEST_DATA_COLLECTION" in str(excinfo.value)


def test_a_network_error_is_reported_not_raised_raw(monkeypatch):
    import requests as requests_module
    post = FakePost(requests_module.ConnectionError("boom"))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/a:free"])

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest._call_model("s", "p")
    assert "OpenRouter" in str(excinfo.value)


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")

    with pytest.raises(coach_digest.MailboxError) as excinfo:
        coach_digest._call_model("s", "p")
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_default_models_keep_a_free_fallback_behind_the_paid_primary():
    """The paid primary stops working at a zero balance, so the list must end in
    a ":free" entry — otherwise running out of credit silently kills the digest
    instead of degrading it."""
    defaults = [m.strip() for m in config.DEFAULT_COACH_DIGEST_MODELS.split(",")]
    assert len(defaults) >= 2  # a single model has nothing to fall back to
    assert defaults[-1].endswith(":free")


def test_running_out_of_credit_falls_back_to_the_free_model(monkeypatch):
    post = FakePost(
        FakeHTTPResponse(status_code=402, text="insufficient credits"),
        FakeHTTPResponse(payload=completion(GOOD_REPLY)),
    )
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS",
                        ["vendor/paid-model", "vendor/spare:free"])

    assert coach_digest._call_model("s", "p") == GOOD_REPLY
    assert [r["body"]["model"] for r in post.requests] == ["vendor/paid-model",
                                                           "vendor/spare:free"]


def test_reasoning_is_disabled_by_default(monkeypatch):
    """max_tokens caps thinking AND answer together on a reasoning model, so
    leaving thinking on lets a long window spend the whole budget thinking and
    return empty content. Measured 3x faster and ~7x cheaper with it off."""
    post = FakePost(FakeHTTPResponse(payload=completion(GOOD_REPLY)),
                    FakeHTTPResponse(payload=completion(GOOD_REPLY)))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/thinker"])

    monkeypatch.setattr(config, "COACH_DIGEST_REASONING", "off")
    coach_digest._call_model("s", "p")
    assert post.requests[0]["body"]["reasoning"] == {"enabled": False}

    # Opt back in for a model that needs it.
    monkeypatch.setattr(config, "COACH_DIGEST_REASONING", "on")
    coach_digest._call_model("s", "p")
    assert "reasoning" not in post.requests[1]["body"]


def test_max_tokens_leaves_room_for_reasoning_we_asked_not_to_happen(monkeypatch):
    """Some providers ignore reasoning.enabled=false. Measured: ~3,900 chars of
    reasoning burned a 900-token ceiling and returned empty content 2 of 3
    tries, which the poller reads as a dead model."""
    post = FakePost(FakeHTTPResponse(payload=completion(GOOD_REPLY)))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/thinker"])

    coach_digest._call_model("s", "p")

    assert post.requests[0]["body"]["max_tokens"] == config.COACH_DIGEST_MAX_TOKENS
    assert config.COACH_DIGEST_MAX_TOKENS >= 2000


def test_provider_routing_restricts_data_collection(monkeypatch):
    """Coach email names students, so by default only providers that don't
    collect prompts may serve the request."""
    post = FakePost(FakeHTTPResponse(payload=completion(GOOD_REPLY)))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/a:free"])
    monkeypatch.setattr(config, "COACH_DIGEST_DATA_COLLECTION", "deny")

    monkeypatch.setattr(config, "COACH_DIGEST_IGNORE_PROVIDERS", [])
    coach_digest._call_model("s", "p")
    assert post.requests[0]["body"]["provider"] == {"data_collection": "deny"}

    # Blank = let OpenRouter route anywhere; the key must then be absent, not
    # sent as an empty value.
    monkeypatch.setattr(config, "COACH_DIGEST_DATA_COLLECTION", "")
    coach_digest._call_model("s", "p")
    assert "provider" not in post.requests[1]["body"]


def test_misbehaving_providers_are_routed_around(monkeypatch):
    """AtlasCloud ignores reasoning.enabled=false and thinks anyway, returning
    empty content. It advertises support for the flag, so require_parameters
    doesn't filter it out — it has to be named."""
    post = FakePost(FakeHTTPResponse(payload=completion(GOOD_REPLY)))
    monkeypatch.setattr(coach_digest.requests, "post", post)
    monkeypatch.setattr(config, "COACH_DIGEST_MODELS", ["vendor/a"])
    monkeypatch.setattr(config, "COACH_DIGEST_IGNORE_PROVIDERS", ["AtlasCloud"])

    coach_digest._call_model("s", "p")

    assert post.requests[0]["body"]["provider"]["ignore"] == ["AtlasCloud"]


# --- Wire contract -------------------------------------------------------------

def test_missing_mailbox_is_501_so_the_app_hides_the_card(db, client):
    """501 (or 404) is the feature flag. A 200 with an empty digest would render
    an empty card instead of no card."""
    athlete = make_athlete(db)

    for response in (client.get("/coach-digest", headers=auth(athlete)),
                     client.post("/coach-digest/refresh", headers=auth(athlete))):
        assert response.status_code == 501


def test_endpoints_require_a_token(db, client):
    assert client.get("/coach-digest").status_code == 401
    assert client.post("/coach-digest/refresh").status_code == 401
    assert client.get("/coach-digest",
                      headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_nothing_yet_is_a_200_with_nulls(db, client):
    make_mailbox(db)
    athlete = make_athlete(db)

    response = client.get("/coach-digest", headers=auth(athlete))

    assert response.status_code == 200
    assert response.json() == {"digest": None, "messages": []}


def test_empty_summary_serves_a_null_digest(db, client):
    """The model returning "nothing useful here" must not render a blank card."""
    mailbox = make_mailbox(db)
    db.add(CoachDigest(mailbox_id=mailbox.id, headline="", bullets=[], actions=[],
                       generated_at=datetime(2026, 8, 10, 13, 0),
                       source_ids=["<a@mail>"]))
    db.commit()
    athlete = make_athlete(db)

    assert client.get("/coach-digest", headers=auth(athlete)).json()["digest"] is None


def test_response_shape_content_type_and_utc_timestamps(db, client):
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>", subject="Meet — 9am start")])
    coach_digest.poll_mailbox(db, mailbox, imap_factory=imap,
                              call_model=FakeModel(GOOD_REPLY))
    athlete = make_athlete(db)

    response = client.get("/coach-digest", headers=auth(athlete))
    body = response.json()

    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert set(body) == {"digest", "messages"}
    assert set(body["digest"]) == {"headline", "bullets", "actions",
                                   "generated_at", "source_count"}
    assert set(body["messages"][0]) == {"id", "from", "from_name", "subject",
                                        "date", "body"}
    # Explicit Z on both timestamps: without it the app reads them as local time
    # and "Summarized 3h ago" comes out wrong.
    assert body["digest"]["generated_at"].endswith("Z")
    assert body["messages"][0]["date"] == "2026-08-09T18:00:00Z"
    assert body["digest"]["source_count"] == 1
    assert body["messages"][0]["from"] == "coach@school.edu"
    assert body["messages"][0]["from_name"] == "Coach Kim"
    # Em-dash survives as real UTF-8.
    assert "—" in body["messages"][0]["subject"]


def test_refresh_returns_502_with_a_useful_message_and_no_password(db, client):
    make_mailbox(db)
    athlete = make_athlete(db)
    imap = FakeIMAP([], fail_login=True)

    def factory(host, port):
        return imap(host, port)

    import imaplib as imaplib_module
    original = imaplib_module.IMAP4_SSL
    imaplib_module.IMAP4_SSL = factory
    try:
        response = client.post("/coach-digest/refresh", headers=auth(athlete))
    finally:
        imaplib_module.IMAP4_SSL = original

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "App Password" in detail
    assert "app-password" not in detail
    assert "super-secret" not in response.text


def test_refresh_end_to_end_returns_the_fresh_digest(db, client, monkeypatch):
    make_mailbox(db)
    athlete = make_athlete(db)
    imap = FakeIMAP([raw_email("<a@mail>")])
    monkeypatch.setattr(imaplib, "IMAP4_SSL", imap)
    monkeypatch.setattr(coach_digest, "_call_model", FakeModel(GOOD_REPLY))

    response = client.post("/coach-digest/refresh", headers=auth(athlete))

    assert response.status_code == 200
    body = response.json()
    assert body["digest"]["headline"].startswith("Saturday's meet moved to 9am")
    assert body["digest"]["bullets"] == ["Tuesday: 6x800m at the track, 5pm",
                                         "Wednesday: easy 4 miles"]
    assert body["digest"]["actions"] == [
        "Turn in the travel permission form by Thursday"]
    assert [m["id"] for m in body["messages"]] == ["<a@mail>"]


# --- Storage -------------------------------------------------------------------

def test_stored_password_is_encrypted_and_round_trips(db):
    mailbox = make_mailbox(db)

    assert mailbox.imap_password_encrypted != "app-password"
    assert "app-password" not in mailbox.imap_password_encrypted
    assert coach_digest.decrypt_secret(mailbox.imap_password_encrypted) == "app-password"


def test_messages_that_left_the_window_are_dropped(db):
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<old@mail>"), raw_email("<new@mail>")])
    model = FakeModel(GOOD_REPLY)
    coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)

    imap.messages = [raw_email("<new@mail>"), raw_email("<newer@mail>")]
    coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)

    stored = {row.message_id for row in db.scalars(select(CoachMessage)).all()}
    assert stored == {"<new@mail>", "<newer@mail>"}


def test_an_empty_window_clears_the_digest_too(db):
    """Once the mail it summarized has aged out, the digest describes messages
    the app can no longer show — serve the "nothing yet" state instead."""
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>")])
    model = FakeModel(GOOD_REPLY)
    coach_digest.poll_mailbox(db, mailbox, imap_factory=imap, call_model=model)
    assert db.get(CoachDigest, mailbox.id) is not None

    imap.messages = []
    outcome = coach_digest.poll_mailbox(db, mailbox, imap_factory=imap,
                                        call_model=model)

    assert outcome == "empty"
    assert db.get(CoachDigest, mailbox.id) is None
    assert db.scalars(select(CoachMessage)).all() == []
    assert len(model.calls) == 1  # an empty window costs no model call


def test_env_config_syncs_into_the_mailbox_row(db):
    mailbox = coach_digest.sync_team_mailbox_from_env(db)

    assert mailbox is not None and mailbox.athlete_id is None
    assert mailbox.imap_host == "imap.example.com"
    assert mailbox.imap_username == "chadwickxc.mail@example.com"
    assert mailbox.sender_filter == "coach@school.edu"
    assert coach_digest.decrypt_secret(
        mailbox.imap_password_encrypted) == "super-secret-app-password"

    # Running again updates the same row instead of adding a second mailbox.
    coach_digest.sync_team_mailbox_from_env(db)
    assert db.query(CoachMailbox).count() == 1


def test_a_second_refresh_right_behind_the_first_reuses_the_result(db, monkeypatch):
    """Guards against two athletes tapping Re-summarize together paying twice."""
    monkeypatch.setattr(coach_digest, "_FRESH_SECONDS", 10)
    mailbox = make_mailbox(db)
    imap = FakeIMAP([raw_email("<a@mail>")])
    model = FakeModel(GOOD_REPLY)

    coach_digest.poll_mailbox(db, mailbox, force=True, imap_factory=imap,
                             call_model=model)
    outcome = coach_digest.poll_mailbox(db, mailbox, force=True,
                                        imap_factory=imap, call_model=model)

    assert outcome == "already-fresh"
    assert len(model.calls) == 1


def test_poll_interval_of_zero_disables_the_background_poller():
    """A staging deployment shares production's mailbox, so it must not poll:
    two pollers would each fetch every cycle and each pay to summarize. The
    endpoints stay up, so refresh still works by hand."""
    import inspect
    import main
    source = inspect.getsource(main.lifespan)
    assert "COACH_POLL_INTERVAL_MINUTES > 0" in source
    assert "create_task(coach_digest.run_poller())" in source
