"""End-to-end simulation of the manual review flow without a Discord connection.

Drives the real handlers with fake Discord objects:
  submit modal -> review-channel post -> reviewer DM with Approve/Reject
  -> unauthorized click rejected -> authorized Approve -> both messages updated
  -> player notified.

Run with either:
    python tests/test_end_to_end_flow.py
    python -m pytest tests -q
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ffpay-e2e-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "payments.sqlite3")
os.environ["GUILD_ID"] = "0"
os.environ.pop("NOTIFICATION_USER_ID", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
import bot  # noqa: E402  (import after env setup)

PLAYER_ID = 1001
REVIEWER_ID = 2002
REVIEW_CHANNEL_ID = 3003
INTRUDER_ID = 4004


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.modals = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)

    def is_done(self):
        return bool(self.messages or self.modals)


class FakeMessage:
    def __init__(self, channel, message_id, embed=None, view=None):
        self.channel = channel
        self.id = message_id
        self.embeds = [embed] if embed is not None else []
        self.view = view
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]]
        if "view" in kwargs:
            self.view = kwargs["view"]


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []
        self._next_id = 900000

    async def send(self, content=None, embed=None, view=None, **kwargs):
        self._next_id += 1
        message = FakeMessage(self, self._next_id, embed=embed, view=view)
        message.content = content
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
            raise http_error(403)
        self.dms.append(SimpleNamespace(content=content, embed=embed, view=view))
        return FakeMessage(self.dm_channel, 800000 + len(self.dms), embed=embed, view=view)

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


def _interaction(user, response=None, data=None, message=None):
    return SimpleNamespace(user=user, response=response or FakeResponse(),
                           data=data or {}, message=message, guild=None)


async def _scenario():
    bot.init_db()
    review_channel = FakeChannel(REVIEW_CHANNEL_ID)
    player = FakeUser(PLAYER_ID, "player#1001")
    reviewer = FakeUser(REVIEWER_ID, "reviewer#2002")
    intruder = FakeUser(INTRUDER_ID, "intruder#4004")

    original_client = bot.bot
    original_message_ref = bot.message_ref
    bot.bot = FakeClient({REVIEW_CHANNEL_ID: review_channel}, {PLAYER_ID: player, REVIEWER_ID: reviewer, INTRUDER_ID: intruder})
    # message_ref() is patched so sync_review_messages() edits fake messages instead of
    # building real API routes.
    refs = {}

    def fake_message_ref(channel, message_id):
        return refs[(channel.id, int(message_id))]

    bot.message_ref = fake_message_ref
    try:
        bot.set_setting("fee", 150)
        bot.set_setting("bkash_number", "01700000001")
        bot.set_setting("review_channel_id", REVIEW_CHANNEL_ID)
        bot.set_setting("notification_user_id", REVIEWER_ID)

        # --- 1. Player submits the payment details -------------------------------
        modal = bot.PaymentSubmitModal("bKash")
        class _Value:
            def __init__(self, value):
                self.value = value
        for name, value in (("registration_ref", "FF-2026-0042"), ("tournament", "University FF Cup"),
                            ("sender_number", "01712345678"), ("transaction_id", "trx8ab12cd")):
            setattr(modal, name, _Value(value))
        player_response = FakeResponse()
        await modal.on_submit(_interaction(player, player_response))

        assert len(review_channel.sent) == 1, "submission must be posted to the review channel"
        channel_message = review_channel.sent[0]
        assert isinstance(channel_message.view, bot.ReviewView), "review channel must carry the review buttons"

        with bot.db() as conn:
            row = conn.execute("SELECT * FROM payments WHERE transaction_id='TRX8AB12CD'").fetchone()
        assert row is not None, "valid transaction id must be stored (upper-cased)"
        payment_id = row["id"]
        assert row["status"] == "PENDING"
        assert row["channel_message_id"] == channel_message.id
        assert row["review_channel_id"] == REVIEW_CHANNEL_ID
        assert player_response.messages, "player must get a confirmation"

        # --- 2. Reviewer DM carries the same submission + buttons ---------------
        assert len(reviewer.dms) == 1, "configured admin must receive a DM for the submission"
        dm = reviewer.dms[0]
        assert dm.embed is not None and "TRX8AB12CD" in str(dm.embed.to_dict())
        assert dm.view is not None, "the DM must offer Approve/Reject buttons"
        dm_buttons = {item.label: item for item in dm.view.children}
        assert set(dm_buttons) == {"Approve", "Reject"}
        assert dm_buttons["Approve"].custom_id == f"pay:review:approve:{payment_id}"
        assert dm_buttons["Reject"].custom_id == f"pay:review:reject:{payment_id}"
        assert row["dm_recipient_id"] == REVIEWER_ID and row["dm_message_id"] is not None

        dm_message = FakeMessage(reviewer.dm_channel, row["dm_message_id"], embed=dm.embed, view=dm.view)
        channel_ref = FakeMessage(review_channel, channel_message.id, embed=channel_message.embeds[0], view=channel_message.view)
        refs[(reviewer.dm_channel.id, row["dm_message_id"])] = dm_message
        refs[(REVIEW_CHANNEL_ID, row["channel_message_id"])] = channel_ref

        # --- 3. An unauthorized DM user cannot review ---------------------------
        intruder_response = FakeResponse()
        await dm_buttons["Approve"].callback(_interaction(
            intruder, intruder_response, data={"custom_id": dm_buttons["Approve"].custom_id}, message=dm_message))
        with bot.db() as conn:
            assert conn.execute("SELECT status FROM payments WHERE id=?", (payment_id,)).fetchone()["status"] == "PENDING"
        assert "not authorized" in intruder_response.messages[0][0]

        # --- 4. Reviewer approves from the DM -----------------------------------
        reviewer_response = FakeResponse()
        await dm_buttons["Approve"].callback(_interaction(
            reviewer, reviewer_response, data={"custom_id": dm_buttons["Approve"].custom_id}, message=dm_message))

        with bot.db() as conn:
            reviewed = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        assert reviewed["status"] == "APPROVED", "approval must be recorded"
        assert reviewed["reviewed_by"] == REVIEWER_ID
        assert "PAY-%06d" % payment_id in reviewer_response.messages[0][0]

        assert dm_message.edits and dm_message.edits[-1].get("view") is None, "DM buttons must be cleared after review"
        assert channel_ref.edits and channel_ref.edits[-1].get("view") is None, "channel buttons must be cleared too"
        assert any(field.name == "Review result" for field in dm_message.embeds[0].fields)

        # --- 5. Player is informed, and the decision cannot be flipped ----------
        assert player.dms, "player must be DM'd the review result"
        assert "Approved" in str(player.dms[-1].embed.title)
        retry_response = FakeResponse()
        await dm_buttons["Approve"].callback(_interaction(
            reviewer, retry_response, data={"custom_id": dm_buttons["Approve"].custom_id}, message=dm_message))
        assert "already" in retry_response.messages[0][0].lower()

        # --- 6. A second submission gets its own buttons -------------------------
        second = bot.ReviewView(payment_id + 500)
        assert [i.custom_id for i in second.children] != [i.custom_id for i in dm.view.children]
    finally:
        bot.bot = original_client
        bot.message_ref = original_message_ref


def http_error(status=403):
    """Build a discord.HTTPException the way the library would."""
    return discord.HTTPException(SimpleNamespace(status=status, reason="Forbidden"), "simulated failure")


class _Value:
    def __init__(self, value):
        self.value = value


def _modal_fields(modal, transaction_id, sender="01712345678", player_id="FF-2026-0042"):
    for name, value in (("registration_ref", player_id), ("tournament", "University FF Cup"),
                        ("sender_number", sender), ("transaction_id", transaction_id)):
        setattr(modal, name, _Value(value))
    return modal


async def _submit(client, review_channel, player, transaction_id="TRX-ONE-001"):
    bot.bot = client
    modal = _modal_fields(bot.PaymentSubmitModal("bKash"), transaction_id)
    response = FakeResponse()
    await modal.on_submit(_interaction(player, response))
    with bot.db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE transaction_id=?", (transaction_id.upper(),)).fetchone()
    # The submission post is the one carrying the review buttons (warnings may follow it).
    submission = next(message for message in reversed(review_channel.sent) if message.view is not None)
    return submission, row, response


async def _scenario_reject_path():
    """Rejecting from the DM asks for a reason, tells the player and clears the buttons."""
    bot.init_db()
    review_channel = FakeChannel(REVIEW_CHANNEL_ID)
    player = FakeUser(PLAYER_ID + 10, "player#1011")
    reviewer = FakeUser(REVIEWER_ID, "reviewer#2002")
    client = FakeClient({REVIEW_CHANNEL_ID: review_channel}, {PLAYER_ID + 10: player, REVIEWER_ID: reviewer})
    original_client, original_ref = bot.bot, bot.message_ref
    refs = {}
    bot.message_ref = lambda channel, message_id: refs[(channel.id, int(message_id))]
    try:
        bot.set_setting("review_channel_id", REVIEW_CHANNEL_ID)
        bot.set_setting("notification_user_id", REVIEWER_ID)
        channel_message, row, _ = await _submit(client, review_channel, player, "TRX-REJECT-9")
        payment_id = row["id"]

        dm = reviewer.dms[-1]
        dm_message = FakeMessage(reviewer.dm_channel, row["dm_message_id"], embed=dm.embed, view=dm.view)
        channel_ref = FakeMessage(review_channel, channel_message.id, embed=channel_message.embeds[0])
        refs[(reviewer.dm_channel.id, row["dm_message_id"])] = dm_message
        refs[(REVIEW_CHANNEL_ID, row["channel_message_id"])] = channel_ref

        reject_button = {item.label: item for item in dm.view.children}["Reject"]
        reject_response = FakeResponse()
        await reject_button.callback(_interaction(
            reviewer, reject_response, data={"custom_id": reject_button.custom_id}, message=dm_message))
        assert reject_response.modals, "Reject must ask for a reason"
        reject_modal = reject_response.modals[0]
        reject_modal.reason = _Value("Payment not found in statement")

        await reject_modal.on_submit(_interaction(reviewer, FakeResponse()))
        with bot.db() as conn:
            reviewed = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        assert reviewed["status"] == "REJECTED"
        assert reviewed["review_reason"] == "Payment not found in statement"
        assert "Rejected" in str(player.dms[-1].embed.title)
        assert dm_message.edits[-1].get("view") is None
        assert channel_ref.edits[-1].get("view") is None
    finally:
        bot.bot, bot.message_ref = original_client, original_ref


async def _scenario_blocked_dm_still_reviewable():
    """If the reviewer has DMs closed the submission still lands in the channel, with a warning."""
    bot.init_db()
    review_channel = FakeChannel(REVIEW_CHANNEL_ID + 1)
    player = FakeUser(PLAYER_ID + 20, "player#1021")
    reviewer = FakeUser(REVIEWER_ID, "reviewer#2002", dms_blocked=True)
    client = FakeClient({review_channel.id: review_channel}, {PLAYER_ID + 20: player, REVIEWER_ID: reviewer})
    original_client, original_ref = bot.bot, bot.message_ref
    refs = {}
    bot.message_ref = lambda channel, message_id: refs[(channel.id, int(message_id))]
    try:
        bot.set_setting("review_channel_id", review_channel.id)
        bot.set_setting("notification_user_id", REVIEWER_ID)
        channel_message, row, player_response = await _submit(client, review_channel, player, "TRX-NODM-7")

        assert row["dm_message_id"] is None, "a failed DM must not be recorded as delivered"
        assert player_response.messages, "the player is still told the submission is pending"
        assert len(review_channel.sent) == 2, "a warning must be posted so admins know the DM failed"
        assert "Could not DM" in review_channel.sent[-1].content

        # Manual review still works from the review channel itself.
        channel_ref = FakeMessage(review_channel, channel_message.id, embed=channel_message.embeds[0],
                                  view=channel_message.view)
        refs[(review_channel.id, row["channel_message_id"])] = channel_ref
        approve = {item.label: item for item in channel_message.view.children}["Approve"]
        await approve.callback(_interaction(
            reviewer, FakeResponse(), data={"custom_id": approve.custom_id}, message=channel_ref))
        with bot.db() as conn:
            assert conn.execute("SELECT status FROM payments WHERE id=?", (row["id"],)).fetchone()["status"] == "APPROVED"
        assert player.dms, "the player still receives the outcome"
    finally:
        bot.bot, bot.message_ref = original_client, original_ref


def test_end_to_end_manual_review_flow():
    asyncio.run(_scenario())


def test_end_to_end_reject_path():
    asyncio.run(_scenario_reject_path())


def test_end_to_end_blocked_dm_still_reviewable():
    asyncio.run(_scenario_blocked_dm_still_reviewable())


if __name__ == "__main__":
    test_end_to_end_manual_review_flow()
    test_end_to_end_reject_path()
    test_end_to_end_blocked_dm_still_reviewable()
    print("ok   all end-to-end scenarios")
