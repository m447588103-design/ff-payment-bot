"""FF Esports payment verification bot.

Manual-verification workflow for bKash / Nagad / Rocket registration payments.

The bot never claims to verify a transfer on its own. Personal bKash / Nagad /
Rocket accounts have no official merchant API that this bot can query, so every
submission is reviewed by a human who must confirm the money actually arrived in
the wallet statement. The Approve / Reject buttons only exist to make that manual
review faster and to record its outcome.
"""

import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("payment-bot")

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
DB_PATH = os.getenv("DATABASE_PATH", "data/payments.sqlite3")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Persistent custom ID layout for the review buttons: pay:review:<action>:<payment_id>.
# The payment id is part of the custom ID so every submission keeps its own buttons
# and nothing depends on which view happened to be registered first.
REVIEW_CUSTOM_ID_RE = re.compile(r"^pay:review:(approve|reject):(\d+)$")
REVIEW_CUSTOM_ID = "pay:review:{action}:{payment_id}"
LEGACY_CUSTOM_IDS = {"pay:approve": "approve", "pay:reject": "reject"}

# Pending-payment reminders. The sweep only *looks* for due reminders every few
# minutes; the cadence each admin actually sees is controlled by the two values
# below: the first DM goes out once a payment has been pending for 30 minutes,
# and further DMs repeat at most once every 30 minutes until it is reviewed.
PENDING_REMINDER_MINUTES = 30
PENDING_REMINDER_INTERVAL_MINUTES = 30
PENDING_REMINDER_SWEEP_MINUTES = 5

MANUAL_CHECK_NOTE = (
    "Open your bKash / Nagad / Rocket app or statement and confirm the money actually "
    "arrived before approving.\n\nThis bot **cannot** verify personal-wallet transfers "
    "automatically — there is no official merchant API for bKash/Nagad/Rocket personal "
    "accounts. Approving only records the result of your own manual check."
)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    """Create tables and apply additive migrations. Safe to run on every start."""
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS payments (
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
            review_reason TEXT,
            review_channel_id INTEGER,
            channel_message_id INTEGER,
            dm_message_id INTEGER,
            dm_recipient_id INTEGER,
            reminder_count INTEGER NOT NULL DEFAULT 0,
            last_reminded_at TEXT
        )""")

        # Additive migration for databases created before the DM review columns existed.
        for column, ddl_type in (
            ("review_channel_id", "INTEGER"),
            ("channel_message_id", "INTEGER"),
            ("dm_message_id", "INTEGER"),
            ("dm_recipient_id", "INTEGER"),
            ("reminder_count", "INTEGER NOT NULL DEFAULT 0"),
            ("last_reminded_at", "TEXT"),
        ):
            if column not in _table_columns(conn, "payments"):
                conn.execute(f"ALTER TABLE payments ADD COLUMN {column} {ddl_type}")

        # Older builds stashed "message:<id>;channel:<id>" inside review_reason.
        # Move that data into the real columns so existing records keep working.
        rows = conn.execute(
            "SELECT id, review_reason FROM payments "
            "WHERE channel_message_id IS NULL AND review_reason LIKE 'message:%'"
        ).fetchall()
        for row in rows:
            match = re.match(r"message:(\d+);channel:(\d+)", row["review_reason"] or "")
            if match:
                conn.execute(
                    "UPDATE payments SET review_channel_id=?, channel_message_id=?, review_reason='' WHERE id=?",
                    (int(match.group(2)), int(match.group(1)), row["id"]),
                )

        defaults = {
            "fee": os.getenv("REGISTRATION_FEE", "100"),
            "bkash_number": os.getenv("BKASH_NUMBER", ""),
            "nagad_number": os.getenv("NAGAD_NUMBER", ""),
            "rocket_number": os.getenv("ROCKET_NUMBER", ""),
            "review_channel_id": os.getenv("REVIEW_CHANNEL_ID", ""),
            "admin_role_id": os.getenv("ADMIN_ROLE_ID", ""),
            "notification_user_id": os.getenv("NOTIFICATION_USER_ID", "").strip(),
            "payment_title": os.getenv("PAYMENT_TITLE", "FF Esports Registration"),
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))


def setting(key, default=""):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def money():
    try:
        return max(0, int(setting("fee", "100")))
    except ValueError:
        return 100


def payment_number(method):
    return setting({"bKash": "bkash_number", "Nagad": "nagad_number", "Rocket": "rocket_number"}[method], "").strip()


def payment_label(payment_id):
    return f"PAY-{int(payment_id):06d}"


def parse_timestamp(value):
    """Read one of our stored ISO timestamps back as an aware UTC datetime.

    Rows are written with datetime.now(timezone.utc).isoformat(); anything that
    cannot be parsed returns None so a hand-edited row never breaks a sweep.
    """
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def human_time(value):
    stamp = parse_timestamp(value)
    if stamp is None:
        return str(value) if value else "—"
    return stamp.strftime("%Y-%m-%d %H:%M UTC")


def find_player_payment(query, user_id):
    """Look up *one player's own* payment row: their latest, or a specific record.

    The user_id filter is part of every query on purpose — /paystatus must never
    be able to read somebody else's submission, so a record that exists but
    belongs to another player simply comes back as no match.
    """
    query = (query or "").strip()
    with db() as conn:
        if not query:
            return conn.execute(
                "SELECT * FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
            ).fetchone()
        match = re.fullmatch(r"PAY-?(\d+)", query, flags=re.IGNORECASE)
        if match:
            return conn.execute(
                "SELECT * FROM payments WHERE id=? AND user_id=?", (int(match.group(1)), user_id)
            ).fetchone()
        row = conn.execute(
            "SELECT * FROM payments WHERE transaction_id=? AND user_id=?", (query.upper(), user_id)
        ).fetchone()
        if row is None and query.isdigit():
            # Convenience only: a bare number is the record id when no Transaction
            # ID matches, which is how players usually read "Record: PAY-000012".
            row = conn.execute(
                "SELECT * FROM payments WHERE id=? AND user_id=?", (int(query), user_id)
            ).fetchone()
        return row


def player_status_embed(row):
    """Private status card for the player who owns the record."""
    status = row["status"]
    if status == "APPROVED":
        color, label = discord.Color.green(), "✅ APPROVED"
    elif status == "REJECTED":
        color, label = discord.Color.red(), "❌ REJECTED"
    else:
        color, label = discord.Color.orange(), "⏳ PENDING — manual verification"

    embed = discord.Embed(
        title=f"Payment {payment_label(row['id'])}",
        color=color,
        description=f"Status: **{label}**",
    )
    embed.add_field(name="Registration ID", value=row["registration_ref"], inline=True)
    embed.add_field(name="Tournament", value=row["tournament"], inline=True)
    embed.add_field(name="Amount", value=f"৳{row['amount']}", inline=True)
    embed.add_field(name="Method", value=row["method"], inline=True)
    embed.add_field(name="Transaction ID", value=f"`{row['transaction_id']}`", inline=True)
    embed.add_field(name="Submitted at", value=human_time(row["submitted_at"]), inline=True)

    if status == "PENDING":
        embed.add_field(
            name="What happens next",
            value=(
                "An admin still has to confirm your transfer in the bKash / Nagad / Rocket "
                "statement. This bot cannot verify a personal-wallet transfer automatically. "
                f"Admins are reminded about it about every {PENDING_REMINDER_INTERVAL_MINUTES} minutes "
                "until it is reviewed."
            ),
            inline=False,
        )
    else:
        embed.add_field(name="Reviewed at", value=human_time(row["reviewed_at"]), inline=True)
        if row["reviewed_by"]:
            embed.add_field(name="Reviewed by", value=f"<@{row['reviewed_by']}>", inline=True)
        if status == "REJECTED":
            embed.add_field(
                name="Reason",
                value=row["review_reason"] or "No reason was recorded. Please contact a tournament admin.",
                inline=False,
            )
            embed.add_field(
                name="Next step",
                value="Contact a tournament admin if you believe this is a mistake.",
                inline=False,
            )
        else:
            embed.add_field(
                name="Result",
                value="Your payment was checked manually and approved.",
                inline=False,
            )
    embed.set_footer(text="Only you can see this message — you can only check your own payments.")
    return embed


def configured_reviewer_id():
    """Discord user id that receives the review DM, or None when not configured."""
    raw = (setting("notification_user_id", "") or os.getenv("NOTIFICATION_USER_ID", "")).strip()
    try:
        reviewer_id = int(raw)
    except (TypeError, ValueError):
        return None
    return reviewer_id if reviewer_id > 0 else None


def is_admin(interaction: discord.Interaction) -> bool:
    """Guild-side permission check used by the slash commands."""
    return is_guild_admin(interaction.user, interaction.guild)


def is_guild_admin(user, guild) -> bool:
    if guild is None or user is None:
        return False
    permissions = getattr(user, "guild_permissions", None)
    if permissions is not None and (permissions.administrator or permissions.manage_guild):
        return True
    role_id = int(setting("admin_role_id", "0") or 0)
    if not role_id or not isinstance(user, discord.Member):
        return False
    return any(role.id == role_id for role in user.roles)


def can_review(interaction: discord.Interaction) -> bool:
    """Who may press Approve / Reject.

    In a server: administrators, Manage Server holders or the configured payment-admin role.
    In a DM: the configured review-DM recipient, plus any server admin we can still resolve
    through GUILD_ID (the DM itself carries no permissions to check).
    """
    if interaction.guild is not None:
        return is_guild_admin(interaction.user, interaction.guild)
    if interaction.user is not None and interaction.user.id == configured_reviewer_id():
        return True
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    if guild is not None:
        return is_guild_admin(guild.get_member(interaction.user.id), guild)
    return False


def payment_id_from_message(message):
    """Legacy fallback: read the payment id out of the embed's 'Payment record' field."""
    if not message or not getattr(message, "embeds", None):
        return None
    for field in message.embeds[0].fields:
        match = re.search(r"PAY-(\d+)", field.value or "")
        if match:
            return int(match.group(1))
    return None


def resolve_payment_id(interaction, fallback=None):
    """Work out which payment a button press belongs to.

    The custom ID is authoritative; the embed field is kept as a fallback so buttons
    posted by an older build (custom ids 'pay:approve' / 'pay:reject') still resolve.
    """
    data = interaction.data if isinstance(getattr(interaction, "data", None), dict) else {}
    custom_id = str(data.get("custom_id") or "")
    match = REVIEW_CUSTOM_ID_RE.match(custom_id)
    if match:
        return int(match.group(2))
    if custom_id in LEGACY_CUSTOM_IDS:
        return payment_id_from_message(getattr(interaction, "message", None)) or fallback
    return payment_id_from_message(getattr(interaction, "message", None)) or fallback


def build_submission_embed(player_id, player_name, registration_ref, tournament, method,
                           sender_number, transaction_id, amount, payment_id):
    embed = discord.Embed(
        title="🚨 New Payment Verification",
        color=discord.Color.orange(),
        description="Review the payment in your bKash/Nagad/Rocket account before approving.",
    )
    embed.add_field(name="Player", value=f"<@{player_id}>\n`{player_id}`", inline=True)
    embed.add_field(name="Amount", value=f"৳{amount}", inline=True)
    embed.add_field(name="Method", value=method, inline=True)
    embed.add_field(name="Registration / Player ID", value=registration_ref, inline=True)
    embed.add_field(name="Tournament", value=tournament, inline=True)
    embed.add_field(name="Sender number", value=f"`{sender_number}`", inline=True)
    embed.add_field(name="Transaction ID", value=f"`{transaction_id}`", inline=False)
    embed.add_field(name="Payment record", value=f"`{payment_label(payment_id)}` • **PENDING**", inline=False)
    embed.add_field(name="⚠️ Manual check required", value=MANUAL_CHECK_NOTE, inline=False)
    embed.set_footer(
        text=f"Submitted by {player_name} • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    return embed


async def resolve_user(user_id):
    user = bot.get_user(user_id)
    if user is None:
        try:
            user = await bot.fetch_user(user_id)
        except discord.HTTPException as exc:
            log.warning("Could not resolve Discord user %s: %s", user_id, exc)
            return None
    return user


async def notify_admin_of_submission(channel, embed, payment_id):
    """DM the configured reviewer the submission together with Approve/Reject buttons.

    The DM is a convenience copy of the review-channel message; both use the same
    persistent view, so either set of buttons resolves to the same payment record.
    Returns True when the DM was delivered.
    """
    reviewer_id = configured_reviewer_id()
    if not reviewer_id:
        log.warning("No review-DM recipient configured; submission only posted to the review channel")
        # Warn once per run instead of on every submission, to avoid channel spam.
        if not getattr(bot, "_dm_recipient_warned", False):
            bot._dm_recipient_warned = True
            await _warn_review_channel(
                channel,
                "⚠️ No review-DM recipient is configured, so submissions are only posted here. "
                "An admin can run `/payadmin setup` and set `notification_user` to receive "
                "Approve/Reject DMs.",
            )
        return False

    reviewer = await resolve_user(reviewer_id)
    if reviewer is None:
        await _warn_review_channel(
            channel,
            f"⚠️ Could not resolve the configured review-DM recipient (`{reviewer_id}`) for "
            f"{payment_label(payment_id)}. Review it here.",
        )
        return False

    try:
        message = await reviewer.send(
            content=(
                f"📥 New payment submission `{payment_label(payment_id)}` needs manual review.\n"
                "Check your wallet statement first, then use the buttons below."
            ),
            embed=embed,
            view=ReviewView(payment_id),
        )
    except discord.HTTPException as exc:
        log.warning("Could not DM payment review to user %s: %s", reviewer_id, exc)
        await _warn_review_channel(
            channel,
            f"⚠️ Could not DM <@{reviewer_id}> about {payment_label(payment_id)} "
            f"({type(exc).__name__}: the member may have DMs disabled). Review it here.",
        )
        return False

    with db() as conn:
        conn.execute(
            "UPDATE payments SET dm_message_id=?, dm_recipient_id=? WHERE id=?",
            (message.id, reviewer_id, payment_id),
        )
    return True


async def _warn_review_channel(channel, text):
    if channel is None:
        return
    try:
        await channel.send(text)
    except discord.HTTPException as exc:
        log.warning("Could not post review-channel warning: %s", exc)


def review_channel():
    """The configured review channel, or None when it is unset/unavailable."""
    channel_id = int(setting("review_channel_id", "0") or 0)
    return bot.get_channel(channel_id) if channel_id else None


def review_message_link(row):
    """Deep link to the review-channel copy of a submission (empty when unknown)."""
    channel_id = row["review_channel_id"] or int(setting("review_channel_id", "0") or 0)
    if not channel_id or not row["channel_message_id"] or not GUILD_ID:
        return ""
    return f"https://discord.com/channels/{GUILD_ID}/{int(channel_id)}/{int(row['channel_message_id'])}"


def pending_minutes(row, now=None):
    submitted = parse_timestamp(row["submitted_at"])
    if submitted is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - submitted).total_seconds() // 60))


def pending_reminders_due(now=None):
    """Pending payments whose admin reminder is due.

    Due means: still PENDING, submitted at least PENDING_REMINDER_MINUTES ago and
    not reminded within the last PENDING_REMINDER_INTERVAL_MINUTES (never reminded
    counts as due). Timestamps are compared as datetimes, not strings.
    """
    now = now or datetime.now(timezone.utc)
    with db() as conn:
        rows = conn.execute("SELECT * FROM payments WHERE status='PENDING' ORDER BY id").fetchall()

    due = []
    for row in rows:
        submitted = parse_timestamp(row["submitted_at"])
        if submitted is None or now - submitted < timedelta(minutes=PENDING_REMINDER_MINUTES):
            continue
        last = parse_timestamp(row["last_reminded_at"])
        if last is not None and now - last < timedelta(minutes=PENDING_REMINDER_INTERVAL_MINUTES):
            continue
        due.append(row)
    return due


def record_reminder_attempt(payment_id, delivered, now=None):
    """Remember when we reminded (or tried to).

    last_reminded_at is written on every attempt so a reviewer with DMs closed is
    not retried on every sweep; reminder_count only counts delivered DMs.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    with db() as conn:
        if delivered:
            conn.execute(
                "UPDATE payments SET reminder_count=reminder_count+1, last_reminded_at=? "
                "WHERE id=? AND status='PENDING'",
                (stamp, payment_id),
            )
        else:
            conn.execute(
                "UPDATE payments SET last_reminded_at=? WHERE id=? AND status='PENDING'",
                (stamp, payment_id),
            )


def build_reminder_embed(row, now=None):
    """Reminder DM body — same manual-check rules as the original submission DM."""
    now = now or datetime.now(timezone.utc)
    waited = pending_minutes(row, now)
    waited_text = f"about {waited} minutes" if waited is not None else "a while"
    embed = discord.Embed(
        title="⏰ Payment still pending",
        color=discord.Color.orange(),
        description=(
            f"`{payment_label(row['id'])}` has been waiting for manual review for {waited_text}.\n"
            f"This reminder repeats about every {PENDING_REMINDER_INTERVAL_MINUTES} minutes until it is reviewed."
        ),
    )
    embed.add_field(name="Player", value=f"<@{row['user_id']}>\n`{row['user_id']}`", inline=True)
    embed.add_field(name="Amount", value=f"৳{row['amount']}", inline=True)
    embed.add_field(name="Method", value=row["method"], inline=True)
    embed.add_field(name="Registration / Player ID", value=row["registration_ref"], inline=True)
    embed.add_field(name="Tournament", value=row["tournament"], inline=True)
    embed.add_field(name="Transaction ID", value=f"`{row['transaction_id']}`", inline=False)
    embed.add_field(name="Submitted at", value=human_time(row["submitted_at"]), inline=True)
    embed.add_field(name="Reminders sent", value=str(int(row["reminder_count"] or 0)), inline=True)

    link = review_message_link(row)
    if link:
        pointer = f"[Open the review message]({link})"
    else:
        channel_id = row["review_channel_id"] or int(setting("review_channel_id", "0") or 0)
        pointer = f"Open the review channel (<#{channel_id}>)" if channel_id else "Open the review channel"
    embed.add_field(
        name="Next step",
        value=f"{pointer} and confirm the transfer in your wallet statement, then Approve or Reject.",
        inline=False,
    )
    embed.add_field(name="⚠️ Manual check required", value=MANUAL_CHECK_NOTE, inline=False)
    embed.set_footer(text="Reminder only — this bot cannot verify personal-wallet transfers automatically.")
    return embed


async def send_pending_reminders(now=None):
    """DM the configured reviewer about every pending payment that is due.

    Returns the number of reminders delivered. Called by the background sweep and
    directly by the offline tests.
    """
    now = now or datetime.now(timezone.utc)
    due = pending_reminders_due(now)
    if not due:
        return 0

    reviewer_id = configured_reviewer_id()
    if not reviewer_id:
        log.warning("Pending reminders skipped: no review-DM recipient configured")
        return 0

    reviewer = await resolve_user(reviewer_id)
    if reviewer is None:
        log.warning("Pending reminders skipped: could not resolve review-DM recipient %s", reviewer_id)
        return 0

    channel = review_channel()
    delivered = 0
    for row in due:
        # The payment may have been reviewed between the sweep query and this DM.
        with db() as conn:
            fresh = conn.execute("SELECT status FROM payments WHERE id=?", (row["id"],)).fetchone()
        if not fresh or fresh["status"] != "PENDING":
            continue

        try:
            await reviewer.send(
                content=(
                    f"⏰ `{payment_label(row['id'])}` is still **PENDING** manual review. "
                    "Check your wallet statement before approving or rejecting."
                ),
                embed=build_reminder_embed(row, now),
            )
        except discord.HTTPException as exc:
            log.warning("Could not DM pending reminder for %s: %s", payment_label(row["id"]), exc)
            was_first = not int(row["reminder_count"] or 0)
            record_reminder_attempt(row["id"], delivered=False, now=now)
            if was_first:
                await _warn_review_channel(
                    channel,
                    f"⚠️ Could not DM <@{reviewer_id}> a pending reminder for "
                    f"{payment_label(row['id'])} ({type(exc).__name__}: the member may have DMs "
                    "disabled). Please review it here.",
                )
            continue

        record_reminder_attempt(row["id"], delivered=True, now=now)
        delivered += 1

    if delivered:
        log.info("Sent %d pending-payment reminder(s)", delivered)
    return delivered


@tasks.loop(minutes=PENDING_REMINDER_SWEEP_MINUTES)
async def pending_reminder_task():
    """Sweep for payments that are overdue for their admin reminder."""
    try:
        await send_pending_reminders()
    except Exception:  # a broken sweep must never kill the loop
        log.exception("Pending reminder sweep failed")


class RejectReasonModal(discord.ui.Modal, title="Reject Payment"):
    reason = discord.ui.TextInput(
        label="Reason for rejection",
        placeholder="e.g. Payment not found / wrong amount",
        max_length=300,
    )

    def __init__(self, payment_id: int, message: discord.Message):
        super().__init__()
        self.payment_id = int(payment_id)
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await review_payment(interaction, self.payment_id, "REJECTED", str(self.reason.value).strip(), self.message)


def clone_embed(embed):
    """Isolated copy of an embed.

    discord.Embed.copy() is documented as shallow and in discord.py 2.x the copy
    keeps sharing the internal field list, so adding a field to the copy also
    mutates the original. Going through to_dict() with fresh field dicts avoids
    that, which matters because we reuse the original review embed.
    """
    data = embed.to_dict()
    data["fields"] = [dict(field) for field in data.get("fields", [])]
    return discord.Embed.from_dict(data)


def reviewed_embed(base_embed, status, reviewer_mention, reason):
    embed = clone_embed(base_embed) if base_embed is not None else discord.Embed(title="Payment review")
    embed.color = discord.Color.green() if status == "APPROVED" else discord.Color.red()
    embed.add_field(name="Review result", value=f"**{status}** by {reviewer_mention}", inline=False)
    if reason:
        embed.add_field(name="Admin note", value=reason, inline=False)
    if status == "APPROVED":
        embed.set_footer(text="Recorded after manual check of the wallet statement.")
    return embed


async def review_payment(interaction, payment_id, status, reason, message):
    """Apply a manual review decision and sync every copy of the review message."""
    if not can_review(interaction):
        return await interaction.response.send_message(
            "⛔ You are not authorized to review payments.", ephemeral=True
        )

    with db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            return await interaction.response.send_message("Payment record not found.", ephemeral=True)
        if row["status"] != "PENDING":
            return await interaction.response.send_message(
                f"This payment is already **{row['status']}**.", ephemeral=True
            )
        # Conditional update: two near-simultaneous clicks (DM + channel) cannot both win.
        updated = conn.execute(
            "UPDATE payments SET status=?, reviewed_by=?, reviewed_at=?, review_reason=? "
            "WHERE id=? AND status='PENDING'",
            (status, interaction.user.id, datetime.now(timezone.utc).isoformat(), reason, payment_id),
        )
        if updated.rowcount == 0:
            return await interaction.response.send_message(
                "This payment was just reviewed by someone else. Refresh your view.", ephemeral=True
            )

    embed = reviewed_embed(message.embeds[0] if message and message.embeds else None,
                           status, interaction.user.mention, reason)
    await sync_review_messages(row, message, embed)

    player = await resolve_user(row["user_id"])
    if player:
        try:
            if status == "APPROVED":
                dm = discord.Embed(
                    title="✅ Payment Approved",
                    color=discord.Color.green(),
                    description="Your payment has been manually verified by the tournament admin.",
                )
                dm.add_field(name="Record", value=f"`{payment_label(payment_id)}`", inline=True)
                dm.add_field(name="Registration ID", value=row["registration_ref"], inline=True)
                dm.add_field(name="Tournament", value=row["tournament"], inline=False)
                dm.add_field(name="Amount", value=f"৳{row['amount']}", inline=True)
                dm.add_field(name="Status", value="**PAID / APPROVED**", inline=True)
            else:
                dm = discord.Embed(
                    title="❌ Payment Rejected",
                    color=discord.Color.red(),
                    description="Your payment submission was not approved.",
                )
                dm.add_field(name="Record", value=f"`{payment_label(payment_id)}`", inline=True)
                dm.add_field(name="Reason", value=reason or "Please contact a tournament admin.", inline=False)
                dm.add_field(name="Next step", value="Contact the admin if you believe this is a mistake.", inline=False)
            await player.send(embed=dm)
        except discord.HTTPException as exc:
            log.warning("Could not DM review result to user %s: %s", row["user_id"], exc)

    await interaction.response.send_message(
        f"✅ Payment `{payment_label(payment_id)}` marked **{status}**.", ephemeral=True
    )


def message_ref(channel, message_id):
    """Lightweight reference to an existing message (no extra fetch)."""
    return discord.PartialMessage(channel=channel, id=int(message_id))


async def sync_review_messages(row, interacted_message, embed):
    """Update the review-channel message *and* the reviewer DM, clearing both button sets."""
    targets = []
    if interacted_message is not None:
        targets.append(interacted_message)

    channel = None
    channel_id = row["review_channel_id"] or int(setting("review_channel_id", "0") or 0)
    if channel_id:
        channel = bot.get_channel(int(channel_id))
    if channel is not None and row["channel_message_id"]:
        targets.append(message_ref(channel, row["channel_message_id"]))

    if row["dm_message_id"] and row["dm_recipient_id"]:
        reviewer = await resolve_user(row["dm_recipient_id"])
        if reviewer is not None:
            dm_channel = reviewer.dm_channel or await reviewer.create_dm()
            targets.append(message_ref(dm_channel, row["dm_message_id"]))

    seen = set()
    for target in targets:
        if target.id in seen:
            continue
        seen.add(target.id)
        try:
            await target.edit(embed=embed, view=None)
        except discord.HTTPException as exc:
            log.warning("Could not update review message %s: %s", target.id, exc)


class ReviewView(discord.ui.View):
    """Approve / Reject buttons, used for both the review channel and the reviewer DM.

    Persistent (timeout=None) with per-payment custom ids, so buttons keep working
    after a restart and never resolve to the wrong submission.
    """

    def __init__(self, payment_id: int):
        super().__init__(timeout=None)
        self.payment_id = int(payment_id)
        approve = discord.ui.Button(
            label="Approve",
            style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=REVIEW_CUSTOM_ID.format(action="approve", payment_id=self.payment_id),
        )
        reject = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
            emoji="❌",
            custom_id=REVIEW_CUSTOM_ID.format(action="reject", payment_id=self.payment_id),
        )
        approve.callback = self._on_approve
        reject.callback = self._on_reject
        self.add_item(approve)
        self.add_item(reject)

    async def _on_approve(self, interaction: discord.Interaction):
        payment_id = resolve_payment_id(interaction, self.payment_id)
        if not payment_id:
            return await interaction.response.send_message("Could not identify this payment record.", ephemeral=True)
        await review_payment(interaction, payment_id, "APPROVED", "", interaction.message)

    async def _on_reject(self, interaction: discord.Interaction):
        payment_id = resolve_payment_id(interaction, self.payment_id)
        if not payment_id:
            return await interaction.response.send_message("Could not identify this payment record.", ephemeral=True)
        await interaction.response.send_modal(RejectReasonModal(payment_id, interaction.message))


class PaymentSubmitModal(discord.ui.Modal, title="Submit Payment Details"):
    registration_ref = discord.ui.TextInput(label="Registration / Player ID", placeholder="e.g. FF-2026-0042", max_length=80)
    tournament = discord.ui.TextInput(label="Tournament name", placeholder="e.g. University FF Championship", max_length=100)
    sender_number = discord.ui.TextInput(label="Sender mobile number", placeholder="01XXXXXXXXX", max_length=20)
    transaction_id = discord.ui.TextInput(label="Transaction ID (TrxID)", placeholder="Enter exact transaction ID", max_length=100)

    def __init__(self, method: str):
        super().__init__()
        self.method = method

    async def on_submit(self, interaction: discord.Interaction):
        trx = str(self.transaction_id.value).strip().upper()
        if len(trx) < 4 or not re.fullmatch(r"[A-Z0-9\-]+", trx):
            return await interaction.response.send_message(
                "❌ Transaction ID format looks invalid. Please check it and submit again.", ephemeral=True
            )
        number = str(self.sender_number.value).strip().replace(" ", "")
        if not re.fullmatch(r"[0-9+]{8,16}", number):
            return await interaction.response.send_message("❌ Please enter a valid sender mobile number.", ephemeral=True)
        if not payment_number(self.method):
            return await interaction.response.send_message(
                f"⚠️ {self.method} payment number is not configured yet. Contact a server admin.", ephemeral=True
            )

        with db() as conn:
            exists = conn.execute("SELECT id,status FROM payments WHERE transaction_id=?", (trx,)).fetchone()
            if exists:
                return await interaction.response.send_message(
                    f"⚠️ This Transaction ID has already been submitted. Existing status: **{exists['status']}**.",
                    ephemeral=True,
                )
            cur = conn.execute("""INSERT INTO payments
                (user_id, username, registration_ref, tournament, amount, method, sender_number, transaction_id, status, submitted_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (interaction.user.id, str(interaction.user), str(self.registration_ref.value).strip(),
                 str(self.tournament.value).strip(), money(), self.method, number, trx, "PENDING",
                 datetime.now(timezone.utc).isoformat()))
            payment_id = cur.lastrowid

        channel_id = int(setting("review_channel_id", "0") or 0)
        channel = bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            with db() as conn:
                conn.execute("DELETE FROM payments WHERE id=? AND status='PENDING'", (payment_id,))
            return await interaction.response.send_message(
                "⚠️ Admin verification channel is not configured or unavailable. Please contact an admin; no submission was saved.",
                ephemeral=True,
            )

        embed = build_submission_embed(
            player_id=interaction.user.id,
            player_name=str(interaction.user),
            registration_ref=str(self.registration_ref.value).strip(),
            tournament=str(self.tournament.value).strip(),
            method=self.method,
            sender_number=number,
            transaction_id=trx,
            amount=money(),
            payment_id=payment_id,
        )
        try:
            msg = await channel.send(embed=embed, view=ReviewView(payment_id))
            with db() as conn:
                conn.execute(
                    "UPDATE payments SET review_channel_id=?, channel_message_id=? WHERE id=?",
                    (channel.id, msg.id, payment_id),
                )
        except discord.HTTPException:
            with db() as conn:
                conn.execute("DELETE FROM payments WHERE id=? AND status='PENDING'", (payment_id,))
            return await interaction.response.send_message(
                "❌ Couldn't send the review request. Please contact an admin.", ephemeral=True
            )

        await interaction.response.send_message(
            f"✅ Payment details submitted for **manual verification**.\nRecord: `{payment_label(payment_id)}`\n"
            "Status: **PENDING**\nAn admin will check the wallet statement and approve or reject it.",
            ephemeral=True,
        )
        await notify_admin_of_submission(channel, embed, payment_id)


class MethodSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Choose payment method", min_values=1, max_values=1,
                         options=[
                             discord.SelectOption(label="bKash", value="bKash", emoji="💗"),
                             discord.SelectOption(label="Nagad", value="Nagad", emoji="🟠"),
                             discord.SelectOption(label="Rocket", value="Rocket", emoji="🟣"),
                         ])

    async def callback(self, interaction: discord.Interaction):
        method = self.values[0]
        number = payment_number(method)
        if not number:
            return await interaction.response.send_message(f"⚠️ {method} is not configured yet. Please contact an admin.", ephemeral=True)
        embed = discord.Embed(title=f"💳 Pay via {method}", color=discord.Color.blurple(),
                              description="Send the exact registration fee, then submit your transaction details below.")
        embed.add_field(name="Pay to", value=f"`{number}`", inline=True)
        embed.add_field(name="Amount", value=f"**৳{money()}**", inline=True)
        embed.add_field(name="Payment instruction",
                        value="Use the payment type specified by the tournament admin. Keep your transaction ID private and enter it accurately.",
                        inline=False)
        embed.set_footer(text="Payment is not confirmed until an admin manually verifies it.")
        await interaction.response.send_message(embed=embed, view=SubmitButtonView(method), ephemeral=True)


class PaymentPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(MethodSelect())


class SubmitButtonView(discord.ui.View):
    def __init__(self, method):
        super().__init__(timeout=300)
        self.method = method

    @discord.ui.button(label="Submit Transaction ID", style=discord.ButtonStyle.primary, emoji="🧾")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PaymentSubmitModal(self.method))


@bot.event
async def on_ready():
    init_db()
    if not getattr(bot, "_persistent_views_added", False):
        bot.add_view(PaymentPanelView())
        # Re-register Approve/Reject for every pending payment so buttons survive restarts.
        with db() as conn:
            rows = conn.execute("SELECT id FROM payments WHERE status='PENDING'").fetchall()
        for row in rows:
            bot.add_view(ReviewView(row["id"]))
        bot._persistent_views_added = True
    if not getattr(bot, "_reminder_sweep_started", False):
        pending_reminder_task.start()
        bot._reminder_sweep_started = True
    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        log.info("Logged in as %s; synced %d commands", bot.user, len(synced))
    except Exception:
        log.exception("Command sync failed")


@bot.tree.command(name="payment_panel", description="Post the payment submission panel")
async def payment_panel(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    embed = discord.Embed(title="💳 Tournament Registration Payment", color=discord.Color.blurple(),
                          description="Choose your payment method, pay the displayed fee, and submit your Transaction ID.\n\n**Your registration is not confirmed until an admin approves the payment.**")
    embed.add_field(name="Supported methods", value="bKash • Nagad • Rocket", inline=False)
    await interaction.channel.send(embed=embed, view=PaymentPanelView())
    await interaction.response.send_message("✅ Payment panel posted.", ephemeral=True)


@bot.tree.command(name="paystatus", description="Privately check your own payment status")
@app_commands.describe(record="Optional: PAY-000012 or your Transaction ID. Leave empty for your latest payment.")
async def paystatus(interaction: discord.Interaction, record: str = None):
    """Player-facing status check.

    Every reply is ephemeral and every query is filtered by the caller's user id,
    so a player can only ever see their own submissions — anyone else's record is
    reported as "not found", which also avoids confirming that it exists.
    """
    row = find_player_payment(record, interaction.user.id)
    if row is None:
        if (record or "").strip():
            return await interaction.response.send_message(
                f"No payment record found for you matching `{record.strip()}`.\n"
                "You can only check your own payments. Use `/paystatus` with no value for your "
                "latest submission, or ask an admin if you think something is wrong.",
                ephemeral=True,
            )
        return await interaction.response.send_message(
            "You have not submitted a payment yet. Use the payment panel to send your "
            "Transaction ID after paying the registration fee.",
            ephemeral=True,
        )
    await interaction.response.send_message(embed=player_status_embed(row), ephemeral=True)


admin = app_commands.Group(name="payadmin", description="Payment bot administration")


@admin.command(name="setup", description="Configure payments, review channel, admin role and DM notification recipient")
@app_commands.describe(fee="Registration fee in BDT", bkash="bKash number", nagad="Nagad number", rocket="Rocket number",
                       review_channel="Private channel where payment requests are sent", admin_role="Optional role allowed to review payments",
                       notification_user="Member who receives the Approve/Reject DM for every submission (defaults to you)")
async def setup(interaction: discord.Interaction, fee: app_commands.Range[int, 1, 100000],
                bkash: str, nagad: str, rocket: str, review_channel: discord.TextChannel,
                admin_role: discord.Role = None, notification_user: discord.Member = None):
    if interaction.guild is None or not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild):
        return await interaction.response.send_message("⛔ Server Administrator / Manage Server permission required for setup.", ephemeral=True)
    set_setting("fee", fee)
    set_setting("bkash_number", bkash.strip())
    set_setting("nagad_number", nagad.strip())
    set_setting("rocket_number", rocket.strip())
    set_setting("review_channel_id", review_channel.id)
    set_setting("admin_role_id", admin_role.id if admin_role else "")
    notification_user = notification_user or interaction.user
    set_setting("notification_user_id", notification_user.id)
    await interaction.response.send_message(
        f"✅ Payment bot configured.\nFee: ৳{fee}\nReview channel: {review_channel.mention}\n"
        f"Admin role: {admin_role.mention if admin_role else 'Server admins only'}\n"
        f"Approve/Reject DMs: {notification_user.mention} (this member must allow server DMs)\n"
        "Reminder: approval is manual — always confirm the transfer in the wallet statement first.",
        ephemeral=True,
    )


@admin.command(name="stats", description="Show payment verification statistics")
async def stats(interaction: discord.Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    with db() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM payments GROUP BY status").fetchall()
        totals = {r["status"]: r["n"] for r in rows}
        total = conn.execute("SELECT COUNT(*) AS n FROM payments").fetchone()["n"]
    embed = discord.Embed(title="📊 Payment Statistics", color=discord.Color.blurple())
    embed.add_field(name="Total submissions", value=str(total), inline=True)
    embed.add_field(name="Pending", value=str(totals.get("PENDING", 0)), inline=True)
    embed.add_field(name="Approved", value=str(totals.get("APPROVED", 0)), inline=True)
    embed.add_field(name="Rejected", value=str(totals.get("REJECTED", 0)), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@admin.command(name="lookup", description="Find a payment by Transaction ID or payment record ID")
@app_commands.describe(query="Transaction ID or payment record number, e.g. PAY-000012")
async def lookup(interaction: discord.Interaction, query: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    q = query.strip()
    with db() as conn:
        if q.upper().startswith("PAY-"):
            try:
                pid = int(q[4:])
            except ValueError:
                pid = -1
            row = conn.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM payments WHERE transaction_id=?", (q.upper(),)).fetchone()
    if not row:
        return await interaction.response.send_message("No payment record found.", ephemeral=True)
    embed = discord.Embed(title=f"Payment {payment_label(row['id'])}", color=discord.Color.blurple())
    for name, value in [
        ("Player", f"<@{row['user_id']}> (`{row['user_id']}`)"),
        ("Registration ID", row["registration_ref"]), ("Tournament", row["tournament"]),
        ("Amount", f"৳{row['amount']}"), ("Method", row["method"]),
        ("Sender number", row["sender_number"]), ("Transaction ID", row["transaction_id"]),
        ("Status", row["status"]), ("Submitted at", row["submitted_at"])
    ]:
        embed.add_field(name=name, value=value, inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


bot.tree.add_command(admin)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Add it to your .env file.")
    init_db()
    bot.run(TOKEN)
