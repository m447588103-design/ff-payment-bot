"""Offline checks for the payment-review flow.

No Discord connection or token is needed: these tests exercise the database
migration, the persistent review buttons and the permission rules.

Run with either:
    python tests/test_review_flow.py
    python -m pytest tests -q
"""

import os
import sys
import tempfile
from types import SimpleNamespace

# Configure an isolated database *before* importing the bot module.
_TMP = tempfile.mkdtemp(prefix="ffpay-test-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "payments.sqlite3")
os.environ["GUILD_ID"] = "0"
os.environ.pop("NOTIFICATION_USER_ID", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402  (import after env setup)

# Tests must be order-independent, so create the schema up front.
bot.init_db()


def _dm_interaction(user_id, message=None, custom_id=None):
    data = {"custom_id": custom_id} if custom_id else {}
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=None,
        data=data,
        message=message,
    )


def _guild_interaction(user_id, administrator=False, manage_guild=False, member=True):
    user = SimpleNamespace(
        id=user_id,
        guild_permissions=SimpleNamespace(administrator=administrator, manage_guild=manage_guild),
    )
    if member:
        user = SimpleNamespace(**{**user.__dict__, "roles": []})
    return SimpleNamespace(user=user, guild=SimpleNamespace(id=4242), data={}, message=None)


def _message_with_payment_field(payment_id):
    embed = SimpleNamespace(fields=[SimpleNamespace(name="Payment record",
                                                   value=f"`{bot.payment_label(payment_id)}` • **PENDING**")])
    return SimpleNamespace(id=555, embeds=[embed])


def test_schema_has_review_columns_and_is_idempotent():
    bot.init_db()
    bot.init_db()  # must not raise on an existing database
    with bot.db() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(payments)")}
        settings = {row["key"] for row in conn.execute("SELECT key FROM settings")}
    for column in ("review_channel_id", "channel_message_id", "dm_message_id", "dm_recipient_id"):
        assert column in columns, f"missing column {column}"
    assert {"bkash_number", "nagad_number", "rocket_number", "review_channel_id",
            "admin_role_id", "notification_user_id"} <= settings


def test_legacy_review_reason_is_migrated():
    with bot.db() as conn:
        conn.execute(
            """INSERT INTO payments
               (user_id, username, registration_ref, tournament, amount, method, sender_number,
                transaction_id, status, submitted_at, review_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (1, "legacy#1", "FF-1", "Legacy Cup", 100, "bKash", "01700000000",
             "LEGACYTRX01", "PENDING", "2026-01-01T00:00:00+00:00", "message:9001;channel:7002"),
        )
    bot.init_db()  # backfills the new columns
    with bot.db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE transaction_id='LEGACYTRX01'").fetchone()
    assert row["channel_message_id"] == 9001
    assert row["review_channel_id"] == 7002
    assert (row["review_reason"] or "") == ""


def test_review_buttons_have_unique_persistent_custom_ids():
    first, second = bot.ReviewView(7), bot.ReviewView(8)
    first_ids = [item.custom_id for item in first.children]
    second_ids = [item.custom_id for item in second.children]
    assert first_ids == ["pay:review:approve:7", "pay:review:reject:7"]
    assert second_ids == ["pay:review:approve:8", "pay:review:reject:8"]
    assert not set(first_ids) & set(second_ids)
    assert first.is_persistent(), "review view must survive bot restarts"
    labels = {item.label: item.style for item in first.children}
    assert labels["Approve"] is bot.discord.ButtonStyle.success
    assert labels["Reject"] is bot.discord.ButtonStyle.danger


def test_resolve_payment_id_from_custom_id():
    assert bot.resolve_payment_id(_dm_interaction(1, custom_id="pay:review:approve:42")) == 42
    assert bot.resolve_payment_id(_dm_interaction(1, custom_id="pay:review:reject:1337")) == 1337


def test_resolve_payment_id_falls_back_to_embed_for_legacy_buttons():
    message = _message_with_payment_field(12)
    assert bot.resolve_payment_id(_dm_interaction(1, message=message, custom_id="pay:approve")) == 12
    assert bot.resolve_payment_id(_dm_interaction(1, message=message)) == 12
    assert bot.resolve_payment_id(_dm_interaction(1), fallback=99) == 99


def test_dm_recipient_can_review_but_other_dm_users_cannot():
    bot.set_setting("notification_user_id", 123456789)
    assert bot.configured_reviewer_id() == 123456789
    assert bot.can_review(_dm_interaction(123456789)) is True
    assert bot.can_review(_dm_interaction(987654321)) is False


def test_invalid_or_empty_recipient_is_ignored():
    bot.set_setting("notification_user_id", "not-a-number")
    assert bot.configured_reviewer_id() is None
    assert bot.can_review(_dm_interaction(5)) is False
    bot.set_setting("notification_user_id", 0)
    assert bot.configured_reviewer_id() is None
    bot.set_setting("notification_user_id", 123456789)  # restore for later tests


def test_guild_permissions_decide_channel_reviews():
    bot.set_setting("admin_role_id", "")
    assert bot.can_review(_guild_interaction(1, administrator=True)) is True
    assert bot.can_review(_guild_interaction(1, manage_guild=True)) is True
    assert bot.can_review(_guild_interaction(1)) is False
    assert bot.is_admin(SimpleNamespace(user=SimpleNamespace(id=1), guild=None)) is False


def test_submission_embed_states_manual_check_requirement():
    embed = bot.build_submission_embed(
        player_id=5, player_name="player#5", registration_ref="FF-2026-0042",
        tournament="University FF Championship", method="bKash", sender_number="01700000000",
        transaction_id="TRX123456", amount=100, payment_id=42,
    )
    fields = {field.name: field.value for field in embed.fields}
    assert "PAY-000042" in fields["Payment record"]
    assert "PENDING" in fields["Payment record"]
    note = fields["⚠️ Manual check required"].lower()
    assert "confirm" in note
    assert "cannot" in note, "embed must not imply automatic verification"
    assert "TRX123456" in fields["Transaction ID"]


def test_reviewed_embed_records_outcome():
    base = bot.build_submission_embed(
        player_id=5, player_name="player#5", registration_ref="FF-2026-0042",
        tournament="Cup", method="Nagad", sender_number="01800000000",
        transaction_id="TRX999", amount=100, payment_id=3,
    )
    approved = bot.reviewed_embed(base, "APPROVED", "<@1>", "")
    assert approved.color == bot.discord.Color.green()
    assert any(field.name == "Review result" and "APPROVED" in field.value for field in approved.fields)
    rejected = bot.reviewed_embed(base, "REJECTED", "<@1>", "Payment not found")
    assert rejected.color == bot.discord.Color.red()
    assert any(field.name == "Admin note" and field.value == "Payment not found" for field in rejected.fields)
    assert len(base.fields) == 9, "the original embed must not be mutated"


def test_only_one_review_can_win_per_payment():
    with bot.db() as conn:
        cur = conn.execute(
            """INSERT INTO payments
               (user_id, username, registration_ref, tournament, amount, method, sender_number,
                transaction_id, status, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (2, "player#2", "FF-2", "Cup", 100, "Rocket", "01900000000",
             "RACETRX01", "PENDING", "2026-01-01T00:00:00+00:00"),
        )
        payment_id = cur.lastrowid
        first = conn.execute(
            "UPDATE payments SET status='APPROVED' WHERE id=? AND status='PENDING'", (payment_id,))
        second = conn.execute(
            "UPDATE payments SET status='REJECTED' WHERE id=? AND status='PENDING'", (payment_id,))
        row = conn.execute("SELECT status FROM payments WHERE id=?", (payment_id,)).fetchone()
    assert first.rowcount == 1
    assert second.rowcount == 0, "a second click must not overwrite the first decision"
    assert row["status"] == "APPROVED"


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
