"""Offline checks for /paystatus and the pending-payment reminders.

No Discord connection or token is needed: the player status command is driven with
fake interactions and the reminder sweep is driven with fake users/channels plus an
injected "now", so the 30-minute cadence is tested without waiting.

Run with either:
    python tests/test_payment_status_and_reminders.py
    python -m pytest tests -q
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# Configure an isolated database *before* importing the bot module.
_TMP = tempfile.mkdtemp(prefix="ffpay-status-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "payments.sqlite3")
os.environ["GUILD_ID"] = "0"
os.environ.pop("NOTIFICATION_USER_ID", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402  (import after env setup)

PLAYER_ID = 7101
OTHER_PLAYER_ID = 7202
REVIEWER_ID = 7303
REVIEW_CHANNEL_ID = 7404


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        raise AssertionError("this command must never open a modal")

    def is_done(self):
        return bool(self.messages)


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, embed=None, view=None, **kwargs):
        message = SimpleNamespace(id=900000 + len(self.sent), content=content, embed=embed, view=view)
        self.sent.append(message)
        return message

    def __str__(self):
        return f"channel-{self.id}"


class FakeUser:
    def __init__(self, user_id, name, dms_blocked=False):
        self.id = user_id
        self.name = name
        self.mention = f"<@{user_id}>"
        self.dm_channel = FakeChannel(user_id)
        self.dms = []
        self.dms_blocked = dms_blocked

    async def send(self, content=None, embed=None, view=None, **kwargs):
        if self.dms_blocked:
            raise _http_error(403)
        self.dms.append(SimpleNamespace(content=content, embed=embed, view=view))
        return SimpleNamespace(id=800000 + len(self.dms))

    async def create_dm(self):
        return self.dm_channel

    def __str__(self):
        return self.name


class FakeClient:
    def __init__(self, channels, users):
        self._channels = channels
        self._users = users

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_user(self, user_id):
        return self._users.get(user_id)

    async def fetch_user(self, user_id):
        return self._users.get(user_id)

    def get_guild(self, guild_id):
        return None


def _http_error(status=403):
    return bot.discord.HTTPException(SimpleNamespace(status=status, reason="Forbidden"), "simulated failure")


def _interaction(user, response=None):
    return SimpleNamespace(user=user, response=response or FakeResponse(), data={}, message=None, guild=None)


def _now():
    return datetime.now(timezone.utc)


def _quarantine_pending(now=None, keep=None):
    """Stop PENDING rows left behind by earlier checks from joining the next sweep.

    The reminder gates are shared per payment, so stamping last_reminded_at makes
    every pre-existing row "already reminded" and each test only ever sees the
    payments it created itself. `keep` protects the row under test.
    """
    stamp = (now or _now()).isoformat()
    sql = "UPDATE payments SET last_reminded_at=? WHERE status='PENDING'"
    params = [stamp]
    if keep is not None:
        sql += " AND id<>?"
        params.append(keep)
    with bot.db() as conn:
        conn.execute(sql, params)


def _insert_payment(user_id, transaction_id, *, status="PENDING", minutes_ago=0,
                    last_reminded_minutes_ago=None, reminder_count=0, reviewed_by=None,
                    review_reason="", registration_ref="FF-2026-0042", tournament="University FF Cup",
                    amount=150, method="bKash", sender_number="01712345678"):
    submitted = (_now() - timedelta(minutes=minutes_ago)).isoformat()
    last = None
    if last_reminded_minutes_ago is not None:
        last = (_now() - timedelta(minutes=last_reminded_minutes_ago)).isoformat()
    with bot.db() as conn:
        cur = conn.execute(
            """INSERT INTO payments
               (user_id, username, registration_ref, tournament, amount, method, sender_number,
                transaction_id, status, submitted_at, reviewed_by, reviewed_at, review_reason,
                reminder_count, last_reminded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, f"player#{user_id}", registration_ref, tournament, amount, method, sender_number,
             transaction_id, status, submitted, reviewed_by,
             (_now() - timedelta(minutes=1)).isoformat() if reviewed_by else None,
             review_reason, reminder_count, last),
        )
        return cur.lastrowid


async def _run_paystatus(user, record=None):
    response = FakeResponse()
    await bot.paystatus.callback(_interaction(user, response), record=record)
    return response


def _reply_text(response):
    assert response.messages, "the command must always answer with something"
    content, kwargs = response.messages[-1]
    embed = kwargs.get("embed")
    return f"{content or ''} {embed.to_dict() if embed else ''}", kwargs


def test_paystatus_shows_only_the_callers_latest_payment():
    bot.init_db()
    bot.set_setting("fee", 150)
    older = _insert_payment(PLAYER_ID, "STATUS-LATEST-OLD", minutes_ago=90)
    latest = _insert_payment(PLAYER_ID, "STATUS-LATEST-NEW", minutes_ago=5)

    response = asyncio.run(_run_paystatus(SimpleNamespace(id=PLAYER_ID)))
    text, kwargs = _reply_text(response)

    assert kwargs.get("ephemeral") is True, "status replies must stay private"
    assert bot.payment_label(latest) in text, "no argument must return the newest submission"
    assert bot.payment_label(older) not in text
    assert "STATUS-LATEST-NEW" in text
    assert "PENDING" in text
    assert "cannot verify" in text, "the player must be told verification is manual"


def test_paystatus_accepts_record_label_transaction_id_and_bare_number():
    bot.init_db()
    payment_id = _insert_payment(PLAYER_ID, "STATUS-TRX-123", minutes_ago=10)
    label = bot.payment_label(payment_id)

    for query in (label, label.lower(), label.replace("PAY-", "PAY-0"), str(payment_id), "status-trx-123"):
        response = asyncio.run(_run_paystatus(SimpleNamespace(id=PLAYER_ID), record=query))
        text, kwargs = _reply_text(response)
        assert label in text, f"query {query!r} must find the player's own record"
        assert kwargs.get("ephemeral") is True
        assert "STATUS-TRX-123" in text


def test_paystatus_reports_reviewed_records_with_their_outcome():
    bot.init_db()
    approved = _insert_payment(PLAYER_ID, "STATUS-APPROVED-1", status="APPROVED", minutes_ago=120,
                               reviewed_by=REVIEWER_ID)
    rejected = _insert_payment(PLAYER_ID, "STATUS-REJECTED-1", status="REJECTED", minutes_ago=100,
                               reviewed_by=REVIEWER_ID, review_reason="Payment not found in statement")

    approved_text, _ = _reply_text(asyncio.run(_run_paystatus(SimpleNamespace(id=PLAYER_ID),
                                                              record=bot.payment_label(approved))))
    assert "APPROVED" in approved_text
    assert f"<@{REVIEWER_ID}>" in approved_text

    rejected_text, _ = _reply_text(asyncio.run(_run_paystatus(SimpleNamespace(id=PLAYER_ID),
                                                              record=bot.payment_label(rejected))))
    assert "REJECTED" in rejected_text
    assert "Payment not found in statement" in rejected_text
    assert "mistake" in rejected_text, "a rejected player needs a next step"


def test_paystatus_never_reveals_another_players_record():
    bot.init_db()
    other_id = _insert_payment(OTHER_PLAYER_ID, "STATUS-SECRET-TRX", minutes_ago=15,
                               registration_ref="OTHER-SECRET-REG", tournament="Secret Cup")
    other_label = bot.payment_label(other_id)

    for query in (other_label, other_label.lower(), "STATUS-SECRET-TRX", str(other_id)):
        response = asyncio.run(_run_paystatus(SimpleNamespace(id=PLAYER_ID), record=query))
        text, kwargs = _reply_text(response)
        assert "No payment record found for you" in text, f"{query!r} must not expose another player's row"
        assert "only check your own payments" in text
        # The answer echoes the query itself, but none of the other player's details leak.
        assert "OTHER-SECRET-REG" not in text and "Secret Cup" not in text
        if query != "STATUS-SECRET-TRX":
            assert "STATUS-SECRET-TRX" not in text
        assert "PENDING" not in text.replace(str(other_id), "")
        assert kwargs.get("ephemeral") is True


def test_paystatus_without_any_submission_explains_how_to_submit():
    bot.init_db()
    response = asyncio.run(_run_paystatus(SimpleNamespace(id=987654321)))
    text, kwargs = _reply_text(response)
    assert "not submitted a payment yet" in text
    assert kwargs.get("ephemeral") is True


def test_reminder_columns_are_added_to_an_older_database():
    """An existing SQLite file from before this feature keeps working and gets the new columns."""
    main_path = bot.DB_PATH
    legacy_path = os.path.join(_TMP, "legacy-payments.sqlite3")
    bot.DB_PATH = legacy_path
    try:
        with bot.db() as conn:
            conn.execute(
                """CREATE TABLE payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    registration_ref TEXT NOT NULL,
                    tournament TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    method TEXT NOT NULL,
                    sender_number TEXT NOT NULL,
                    transaction_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    submitted_at TEXT NOT NULL,
                    reviewed_by INTEGER,
                    reviewed_at TEXT,
                    review_reason TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO payments
                   (user_id, username, registration_ref, tournament, amount, method, sender_number,
                    transaction_id, status, submitted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (PLAYER_ID, "player#7101", "FF-OLD-1", "Legacy Cup", 100, "Nagad", "01800000000",
                 "LEGACY-REMIND-1", "PENDING",
                 (_now() - timedelta(minutes=61)).isoformat()),
            )

        bot.init_db()
        bot.init_db()  # must stay idempotent on an upgraded database
        with bot.db() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(payments)")}
            row = conn.execute("SELECT * FROM payments WHERE transaction_id='LEGACY-REMIND-1'").fetchone()
        assert {"reminder_count", "last_reminded_at"} <= columns
        assert row["reminder_count"] == 0, "existing rows must default to zero reminders"
        assert row["last_reminded_at"] is None
        # A row created before this feature is still remindable.
        assert row["id"] in {due["id"] for due in bot.pending_reminders_due()}
    finally:
        bot.DB_PATH = main_path


def test_reminder_cadence_constants_and_sweep_are_configured():
    assert bot.PENDING_REMINDER_MINUTES == 30
    assert bot.PENDING_REMINDER_INTERVAL_MINUTES == 30
    assert bot.PENDING_REMINDER_SWEEP_MINUTES <= 30, "the sweep must be at least as frequent as the cadence"
    assert bot.pending_reminder_task.minutes == bot.PENDING_REMINDER_SWEEP_MINUTES


def test_pending_reminders_are_due_only_after_thirty_minutes():
    bot.init_db()
    now = _now()
    _quarantine_pending(now)
    early = _insert_payment(PLAYER_ID, "REMIND-EARLY", minutes_ago=29)
    on_time = _insert_payment(PLAYER_ID, "REMIND-ONTIME", minutes_ago=31)
    reviewed = _insert_payment(PLAYER_ID, "REMIND-DONE", status="APPROVED", minutes_ago=600,
                               reviewed_by=REVIEWER_ID)

    due_ids = {row["id"] for row in bot.pending_reminders_due(now)}
    assert on_time in due_ids, "30 minutes pending must trigger a reminder"
    assert early not in due_ids, "29 minutes pending must not"
    assert reviewed not in due_ids, "reviewed payments must never be reminded"


def test_repeat_reminders_wait_for_the_interval():
    bot.init_db()
    now = _now()
    _quarantine_pending(now)
    reminded_recently = _insert_payment(PLAYER_ID, "REMIND-RECENT", minutes_ago=90,
                                        last_reminded_minutes_ago=29, reminder_count=2)
    overdue = _insert_payment(PLAYER_ID, "REMIND-OVERDUE", minutes_ago=90,
                              last_reminded_minutes_ago=31, reminder_count=2)

    due_ids = {row["id"] for row in bot.pending_reminders_due(now)}
    assert overdue in due_ids, "a repeat reminder is due after the interval"
    assert reminded_recently not in due_ids, "repeat reminders must respect the 30-minute interval"


async def _scenario_reminder_dm_repeats():
    bot.init_db()
    review_channel = FakeChannel(REVIEW_CHANNEL_ID)
    reviewer = FakeUser(REVIEWER_ID, "reviewer#7303")
    original_client = bot.bot
    bot.bot = FakeClient({REVIEW_CHANNEL_ID: review_channel}, {REVIEWER_ID: reviewer})
    try:
        bot.set_setting("review_channel_id", REVIEW_CHANNEL_ID)
        bot.set_setting("notification_user_id", REVIEWER_ID)
        now = _now()
        _quarantine_pending(now)
        payment_id = _insert_payment(PLAYER_ID, "REMIND-LOOP-1", minutes_ago=45)

        assert await bot.send_pending_reminders(now) == 1, "a 45-minute-old payment must be reminded"
        assert len(reviewer.dms) == 1
        dm = reviewer.dms[0]
        assert dm.embed is not None
        assert bot.payment_label(payment_id) in str(dm.embed.to_dict())
        assert "still" in (dm.content or "").lower()
        assert dm.view is None, "reminders are informational; Approve/Reject stays on the submission DM"
        fields = {field.name: field.value for field in dm.embed.fields}
        assert "cannot" in fields["⚠️ Manual check required"].lower()
        assert "wallet statement" in fields["Next step"]
        assert "30" in fields["Next step"] or "30" in dm.embed.description

        with bot.db() as conn:
            row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        assert row["reminder_count"] == 1
        assert bot.parse_timestamp(row["last_reminded_at"]) is not None

        # Same sweep again: nothing is due yet.
        assert await bot.send_pending_reminders(now) == 0, "a reminder must not repeat immediately"
        assert len(reviewer.dms) == 1

        # 31 minutes later the reminder repeats.
        later = now + timedelta(minutes=31)
        _quarantine_pending(later, keep=payment_id)
        assert await bot.send_pending_reminders(later) == 1
        assert len(reviewer.dms) == 2
        with bot.db() as conn:
            assert conn.execute("SELECT reminder_count FROM payments WHERE id=?", (payment_id,)).fetchone()[0] == 2

        # Reviewing stops the reminders for good.
        with bot.db() as conn:
            conn.execute("UPDATE payments SET status='APPROVED' WHERE id=?", (payment_id,))
        much_later = now + timedelta(minutes=90)
        _quarantine_pending(much_later, keep=payment_id)
        assert await bot.send_pending_reminders(much_later) == 0
        assert len(reviewer.dms) == 2
    finally:
        bot.bot = original_client


async def _scenario_reminder_edge_cases():
    bot.init_db()
    review_channel = FakeChannel(REVIEW_CHANNEL_ID + 1)
    reviewer = FakeUser(REVIEWER_ID, "reviewer#7303", dms_blocked=True)
    original_client = bot.bot
    bot.bot = FakeClient({review_channel.id: review_channel}, {REVIEWER_ID: reviewer})
    try:
        bot.set_setting("review_channel_id", review_channel.id)
        _quarantine_pending()
        blocked_id = _insert_payment(PLAYER_ID + 1, "REMIND-NODM-1", minutes_ago=60)

        # No recipient configured: reminders are skipped, nothing crashes.
        bot.set_setting("notification_user_id", "")
        assert bot.configured_reviewer_id() is None
        assert await bot.send_pending_reminders(_now()) == 0
        assert review_channel.sent == []

        # Closed DMs: the attempt is recorded (so it is not retried every sweep) and
        # the review channel is warned exactly once, on the first attempt.
        bot.set_setting("notification_user_id", REVIEWER_ID)
        now = _now()
        assert await bot.send_pending_reminders(now) == 0
        with bot.db() as conn:
            row = conn.execute("SELECT * FROM payments WHERE id=?", (blocked_id,)).fetchone()
        assert row["reminder_count"] == 0, "an undelivered reminder must not be counted"
        assert bot.parse_timestamp(row["last_reminded_at"]) is not None, "the attempt must be timestamped"
        assert len(review_channel.sent) == 1
        assert "Could not DM" in review_channel.sent[0].content

        assert await bot.send_pending_reminders(now + timedelta(minutes=5)) == 0
        assert len(review_channel.sent) == 1, "the channel warning must not repeat every sweep"
    finally:
        bot.bot = original_client


def test_reminder_dm_repeats_every_thirty_minutes():
    asyncio.run(_scenario_reminder_dm_repeats())


def test_reminder_sweep_handles_missing_recipient_and_blocked_dms():
    asyncio.run(_scenario_reminder_edge_cases())


def test_reminder_embed_states_the_manual_check_requirement():
    bot.init_db()
    payment_id = _insert_payment(PLAYER_ID + 2, "REMIND-EMBED-1", minutes_ago=45, reminder_count=1)
    with bot.db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
    embed = bot.build_reminder_embed(row)
    fields = {field.name: field.value for field in embed.fields}
    assert bot.payment_label(payment_id) in embed.title + embed.description + str(embed.to_dict())
    assert "45" in embed.description, "the admin should see how long it has been waiting"
    assert fields["Reminders sent"] == "1"
    assert "cannot" in fields["⚠️ Manual check required"].lower()
    assert "automatically" in embed.footer.text.lower()


def _run_all():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
