"""Offline checks for the neon checkout panel, its optional banner and the slash commands.

No Discord connection or token is needed. The /payment_panel command is driven with
fake interactions/channels so the banner fallback can be exercised: with Attach Files,
without it, with the banner disabled, with the file missing, and with a hosted URL.

Run with either:
    python tests/test_payment_panel.py
    python -m pytest tests -q
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

# Configure an isolated database *before* importing the bot module.
_TMP = tempfile.mkdtemp(prefix="ffpay-panel-")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "payments.sqlite3")
os.environ["GUILD_ID"] = "0"
os.environ.pop("NOTIFICATION_USER_ID", None)
os.environ.pop("PAYMENT_BANNER_PATH", None)
os.environ.pop("PAYMENT_BANNER_URL", None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord  # noqa: E402
import bot  # noqa: E402  (import after env setup)

bot.init_db()

ADMIN_ID = 8101
PLAYER_ID = 8202
CHANNEL_ID = 8303
BANNER_NAME = "payment_banner.png"


def forbidden():
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True

    def is_done(self):
        return self.deferred or bool(self.messages)


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeChannel:
    """Channel that can be told to refuse file uploads or all messages."""

    def __init__(self, channel_id=CHANNEL_ID, allow_files=True, allow_send=True):
        self.id = channel_id
        self.allow_files = allow_files
        self.allow_send = allow_send
        self.attempts = []
        self.sent = []

    async def send(self, content=None, embed=None, view=None, file=None, **kwargs):
        self.attempts.append({"embed": embed, "view": view, "file": file})
        try:
            if not self.allow_send:
                raise forbidden()
            if file is not None and not self.allow_files:
                raise forbidden()
        finally:
            if file is not None:
                file.close()  # discord.py closes files after every attempt, success or not
        message = SimpleNamespace(
            id=700000 + len(self.sent),
            embeds=[embed] if embed is not None else [],
            view=view,
            attachments=[file.filename] if file is not None else [],
        )
        self.sent.append(message)
        return message


def _interaction(channel, admin=True, user_id=ADMIN_ID):
    user = SimpleNamespace(
        id=user_id,
        guild_permissions=SimpleNamespace(administrator=admin, manage_guild=False),
        roles=[],
    )
    return SimpleNamespace(
        user=user,
        guild=SimpleNamespace(id=4242),
        channel=channel,
        data={},
        message=None,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def _post(channel, admin=True, banner=True):
    interaction = _interaction(channel, admin=admin)
    asyncio.run(bot.payment_panel.callback(interaction, banner=banner))
    return interaction


def _reply(interaction):
    return (interaction.followup.messages or interaction.response.messages)[-1][0]


def _configure_wallets():
    bot.set_setting("fee", 150)
    bot.set_setting("bkash_number", "01700000001")
    bot.set_setting("nagad_number", "01800000002")
    bot.set_setting("rocket_number", "")
    bot.set_setting("payment_title", "FF Esports Registration")


# --- persistent views ----------------------------------------------------------------

def test_payment_panel_view_is_persistent_with_fixed_custom_id():
    view = bot.PaymentPanelView()
    assert view.timeout is None
    assert view.is_persistent(), "the panel must survive bot restarts"
    assert [item.custom_id for item in view.children] == [bot.PANEL_SELECT_CUSTOM_ID]
    assert bot.PANEL_SELECT_CUSTOM_ID == "pay:panel:method"
    select = view.children[0]
    assert [option.value for option in select.options] == ["bKash", "Nagad", "Rocket"]
    assert all(option.description for option in select.options)


def test_register_persistent_views_covers_panel_and_pending_reviews():
    with bot.db() as conn:
        conn.execute("DELETE FROM payments")
        for n, status in enumerate(("PENDING", "PENDING", "APPROVED")):
            conn.execute(
                """INSERT INTO payments
                   (user_id, username, registration_ref, tournament, amount, method, sender_number,
                    transaction_id, status, submitted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (PLAYER_ID, "player", "FF-1", "Cup", 100, "bKash", "01700000000",
                 f"PANELTRX{n}", status, "2026-01-01T00:00:00+00:00"),
            )
    # bot.bot is a real commands.Bot; add_view() works without a gateway connection and
    # raises ValueError for anything that is not persistent.
    assert bot.register_persistent_views() == 3
    registered = {view.__class__.__name__ for view in bot.bot.persistent_views}
    assert {"PaymentPanelView", "ReviewView"} <= registered
    with bot.db() as conn:
        conn.execute("DELETE FROM payments")


# --- embeds ----------------------------------------------------------------------------

def test_panel_embed_uses_neon_design_and_states_manual_approval():
    _configure_wallets()
    embed = bot.build_payment_panel_embed(banner_url=f"attachment://{BANNER_NAME}")
    assert embed.title == "🎮 FF Esports Registration"
    assert embed.color == discord.Color(bot.NEON_CYAN)
    assert embed.image.url == f"attachment://{BANNER_NAME}"
    fields = {field.name: field.value for field in embed.fields}
    assert fields["💰 Registration fee"] == "**৳150**"
    assert "bKash" in fields["📲 Pay with"] and "Nagad" in fields["📲 Pay with"]
    assert "Rocket" not in fields["📲 Pay with"], "unconfigured wallets must not be advertised"
    assert "/paystatus" in fields["🔎 Track it"]
    text = str(embed.to_dict()).lower()
    assert "manually approves" in text, "panel must say approval is a manual admin decision"
    assert "automatically" not in text and "auto-verif" not in text
    assert len(embed) <= 6000, "Discord embed character limit"

    plain = bot.build_payment_panel_embed()
    assert plain.image.url is None, "fallback panel has no image"
    assert [f.name for f in plain.fields] == [f.name for f in embed.fields], "same content without the banner"


def test_panel_embed_handles_custom_title_and_no_wallets():
    bot.set_setting("payment_title", "  Winter Cup Checkout ")
    bot.set_setting("bkash_number", "")
    bot.set_setting("nagad_number", "")
    bot.set_setting("rocket_number", "")
    embed = bot.build_payment_panel_embed()
    assert embed.title == "🎮 Winter Cup Checkout"
    fields = {field.name: field.value for field in embed.fields}
    assert "/payadmin setup" in fields["📲 Pay with"]
    bot.set_setting("payment_title", "")
    assert bot.payment_title() == bot.DEFAULT_PAYMENT_TITLE
    _configure_wallets()


def test_method_embed_is_branded_per_wallet_and_keeps_manual_note():
    for method, number in (("bKash", "01700000001"), ("Nagad", "01800000002"), ("Rocket", "01900000003")):
        embed = bot.build_method_embed(method, number, 150)
        assert embed.title == f"{bot.METHOD_STYLE[method]['emoji']} Pay via {method}"
        assert embed.color == discord.Color(bot.METHOD_STYLE[method]["color"])
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Pay to"] == f"`{number}`"
        assert fields["Amount"] == "**৳150**"
        assert number in fields["How to pay"] and "TrxID" in fields["How to pay"]
        assert "manually verifies" in embed.footer.text


# --- banner ----------------------------------------------------------------------------

def test_banner_asset_ships_with_the_repository_and_loads():
    path = bot.banner_file_path()
    assert path.endswith(os.path.join("assets", BANNER_NAME))
    assert os.path.isfile(path), "assets/payment_banner.png must be committed"
    with open(path, "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n", "banner must be a PNG"
    assert os.path.getsize(path) < 8 * 1024 * 1024, "must stay under Discord's upload limit"
    file, reason = bot.banner_attachment()
    assert file is not None and reason == ""
    assert file.filename == BANNER_NAME
    file.close()


def test_missing_or_disabled_banner_is_not_an_error():
    original = bot.BANNER_PATH
    try:
        bot.BANNER_PATH = os.path.join(_TMP, "does-not-exist.png")
        file, reason = bot.banner_attachment()
        assert file is None and "not found" in reason
        bot.BANNER_PATH = ""
        assert bot.banner_file_path() == ""
        file, reason = bot.banner_attachment()
        assert file is None and "PAYMENT_BANNER_PATH" in reason
    finally:
        bot.BANNER_PATH = original


# --- /payment_panel ----------------------------------------------------------------------

def test_payment_panel_posts_with_banner_when_files_are_allowed():
    _configure_wallets()
    channel = FakeChannel()
    interaction = _post(channel)
    assert interaction.response.deferred, "the command must acknowledge before uploading"
    assert len(channel.sent) == 1
    message = channel.sent[0]
    assert message.attachments == [BANNER_NAME]
    assert message.embeds[0].image.url == f"attachment://{BANNER_NAME}"
    assert isinstance(message.view, bot.PaymentPanelView) and message.view.is_persistent()
    assert "with the checkout banner" in _reply(interaction)


def test_payment_panel_falls_back_without_banner_when_attach_files_is_missing():
    channel = FakeChannel(allow_files=False)
    interaction = _post(channel)
    assert len(channel.attempts) == 2, "one upload attempt, then one retry without the file"
    assert channel.attempts[0]["file"] is not None
    assert channel.attempts[1]["file"] is None
    assert len(channel.sent) == 1, "the panel must still be posted"
    message = channel.sent[0]
    assert message.attachments == []
    assert message.embeds[0].image.url is None, "no dangling attachment:// reference"
    assert isinstance(message.view, bot.PaymentPanelView)
    reply = _reply(interaction)
    assert "without the banner" in reply and "Attach Files" in reply


def test_payment_panel_banner_option_can_skip_the_upload():
    channel = FakeChannel()
    interaction = _post(channel, banner=False)
    assert len(channel.attempts) == 1 and channel.attempts[0]["file"] is None
    assert channel.sent[0].embeds[0].image.url is None
    assert "without the banner" in _reply(interaction)


def test_payment_panel_posts_without_banner_when_file_is_missing():
    original = bot.BANNER_PATH
    try:
        bot.BANNER_PATH = os.path.join(_TMP, "missing-banner.png")
        channel = FakeChannel()
        interaction = _post(channel)
        assert len(channel.attempts) == 1 and channel.attempts[0]["file"] is None
        assert len(channel.sent) == 1
        assert "not found" in _reply(interaction)
    finally:
        bot.BANNER_PATH = original


def test_payment_panel_uses_hosted_banner_url_without_uploading():
    original = bot.BANNER_URL
    try:
        bot.BANNER_URL = "https://cdn.example.com/ff/payment_banner.png"
        channel = FakeChannel(allow_files=False)  # no Attach Files needed for a hosted image
        interaction = _post(channel)
        assert len(channel.attempts) == 1 and channel.attempts[0]["file"] is None
        assert channel.sent[0].embeds[0].image.url == bot.BANNER_URL
        assert "with the checkout banner" in _reply(interaction)
    finally:
        bot.BANNER_URL = original


def test_payment_panel_reports_when_nothing_can_be_posted():
    channel = FakeChannel(allow_send=False)
    interaction = _post(channel)
    assert channel.sent == []
    reply = _reply(interaction)
    assert reply.startswith("❌") and "Embed Links" in reply


def test_payment_panel_is_admin_only():
    channel = FakeChannel()
    interaction = _post(channel, admin=False)
    assert channel.attempts == []
    assert "Admin only" in interaction.response.messages[0][0]


# --- slash commands --------------------------------------------------------------------

def test_slash_commands_are_registered_with_valid_metadata():
    tree = bot.bot.tree
    names = {command.name for command in tree.get_commands()}
    assert names == {"payment_panel", "paystatus", "payadmin"}
    payadmin = tree.get_command("payadmin")
    assert {sub.name for sub in payadmin.commands} == {"setup", "stats", "lookup"}

    panel = tree.get_command("payment_panel")
    banner = {param.name: param for param in panel.parameters}["banner"]
    assert banner.type is discord.AppCommandOptionType.boolean and not banner.required

    # Discord rejects descriptions over 100 characters; serialise the whole tree the way
    # a sync would so option/description problems show up offline.
    for command in list(tree.get_commands()) + list(payadmin.commands):
        payload = command.to_dict(tree)
        assert 1 <= len(payload["description"]) <= 100, command.name
        for option in payload.get("options", []):
            assert 1 <= len(option["description"]) <= 100, (command.name, option["name"])


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
