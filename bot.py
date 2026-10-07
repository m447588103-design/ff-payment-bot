import os
import re
import sqlite3
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
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

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
            review_reason TEXT
        )""")
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
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    if interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild:
        return True
    role_id = int(setting("admin_role_id", "0") or 0)
    return bool(role_id and isinstance(interaction.user, discord.Member) and any(r.id == role_id for r in interaction.user.roles))

def money():
    try:
        return max(0, int(setting("fee", "100")))
    except ValueError:
        return 100

def payment_number(method):
    return setting({"bKash":"bkash_number", "Nagad":"nagad_number", "Rocket":"rocket_number"}[method], "").strip()

async def notify_admin_of_submission(channel, embed):
    recipient_id = setting("notification_user_id", "").strip() or os.getenv("NOTIFICATION_USER_ID", "").strip()
    try:
        recipient_id = int(recipient_id)
    except ValueError:
        log.warning("Payment DM notification recipient is not configured with a valid Discord user ID")
        return
    if recipient_id <= 0:
        log.warning("Payment DM notification recipient must be a positive Discord user ID")
        return

    recipient = bot.get_user(recipient_id)
    if recipient is None:
        try:
            recipient = await bot.fetch_user(recipient_id)
        except discord.HTTPException as exc:
            log.warning("Could not find payment DM notification recipient %s: %s", recipient_id, exc)
            return

    try:
        await recipient.send(
            content=f"📥 A new payment submission is waiting for review in {channel.mention}.",
            embed=embed,
        )
    except discord.HTTPException as exc:
        log.warning("Could not DM payment notification to user %s: %s", recipient_id, exc)

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
                    f"⚠️ This Transaction ID has already been submitted. Existing status: **{exists['status']}**.", ephemeral=True
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
                "⚠️ Admin verification channel is not configured or unavailable. Please contact an admin; no submission was saved.", ephemeral=True
            )

        embed = discord.Embed(title="🚨 New Payment Verification", color=discord.Color.orange(),
                              description="Review the payment in your bKash/Nagad/Rocket account before approving.")
        embed.add_field(name="Player", value=f"{interaction.user.mention}\n`{interaction.user.id}`", inline=True)
        embed.add_field(name="Amount", value=f"৳{money()}", inline=True)
        embed.add_field(name="Method", value=self.method, inline=True)
        embed.add_field(name="Registration / Player ID", value=str(self.registration_ref.value), inline=True)
        embed.add_field(name="Tournament", value=str(self.tournament.value), inline=True)
        embed.add_field(name="Sender number", value=f"`{number}`", inline=True)
        embed.add_field(name="Transaction ID", value=f"`{trx}`", inline=False)
        embed.add_field(name="Payment record", value=f"`PAY-{payment_id:06d}` • **PENDING**", inline=False)
        embed.set_footer(text=f"Submitted by {interaction.user} • {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        try:
            msg = await channel.send(embed=embed, view=ReviewView(payment_id))
            with db() as conn:
                conn.execute("UPDATE payments SET review_reason=? WHERE id=?", (f"message:{msg.id};channel:{channel.id}", payment_id))
        except discord.HTTPException:
            with db() as conn:
                conn.execute("DELETE FROM payments WHERE id=? AND status='PENDING'", (payment_id,))
            return await interaction.response.send_message("❌ Couldn't send the review request. Please contact an admin.", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Payment details submitted for manual verification.\nRecord: `PAY-{payment_id:06d}`\nStatus: **PENDING**\nAn admin will review it.", ephemeral=True
        )
        await notify_admin_of_submission(channel, embed)

class RejectReasonModal(discord.ui.Modal, title="Reject Payment"):
    reason = discord.ui.TextInput(label="Reason for rejection", placeholder="e.g. Payment not found / wrong amount", max_length=300)

    def __init__(self, payment_id: int, message: discord.Message):
        super().__init__()
        self.payment_id = payment_id
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        await review_payment(interaction, self.payment_id, "REJECTED", str(self.reason.value).strip(), self.message)

async def review_payment(interaction, payment_id, status, reason, message):
    if not is_admin(interaction):
        return await interaction.response.send_message("⛔ You are not authorized to review payments.", ephemeral=True)
    with db() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row:
            return await interaction.response.send_message("Payment record not found.", ephemeral=True)
        if row["status"] != "PENDING":
            return await interaction.response.send_message(f"This payment is already **{row['status']}**.", ephemeral=True)
        conn.execute("UPDATE payments SET status=?, reviewed_by=?, reviewed_at=?, review_reason=? WHERE id=?",
                     (status, interaction.user.id, datetime.now(timezone.utc).isoformat(), reason, payment_id))
    try:
        embed = message.embeds[0] if message.embeds else discord.Embed(title="Payment review")
        embed.color = discord.Color.green() if status == "APPROVED" else discord.Color.red()
        embed.add_field(name="Review result", value=f"**{status}** by {interaction.user.mention}", inline=False)
        if reason:
            embed.add_field(name="Admin note", value=reason, inline=False)
        await message.edit(embed=embed, view=None)
    except (discord.HTTPException, IndexError):
        pass
    user = bot.get_user(row["user_id"])
    if user is None:
        try:
            user = await bot.fetch_user(row["user_id"])
        except discord.HTTPException:
            user = None
    if user:
        try:
            if status == "APPROVED":
                dm = discord.Embed(title="✅ Payment Approved", color=discord.Color.green(),
                                   description="Your payment has been manually verified by the tournament admin.")
                dm.add_field(name="Record", value=f"`PAY-{payment_id:06d}`", inline=True)
                dm.add_field(name="Registration ID", value=row["registration_ref"], inline=True)
                dm.add_field(name="Tournament", value=row["tournament"], inline=False)
                dm.add_field(name="Amount", value=f"৳{row['amount']}", inline=True)
                dm.add_field(name="Status", value="**PAID / APPROVED**", inline=True)
            else:
                dm = discord.Embed(title="❌ Payment Rejected", color=discord.Color.red(),
                                   description="Your payment submission was not approved.")
                dm.add_field(name="Record", value=f"`PAY-{payment_id:06d}`", inline=True)
                dm.add_field(name="Reason", value=reason or "Please contact a tournament admin.", inline=False)
                dm.add_field(name="Next step", value="Contact the admin if you believe this is a mistake.", inline=False)
            await user.send(embed=dm)
        except discord.HTTPException:
            pass
    await interaction.response.send_message(f"✅ Payment `PAY-{payment_id:06d}` marked **{status}**.", ephemeral=True)

class ReviewView(discord.ui.View):
    def __init__(self, payment_id: int):
        super().__init__(timeout=None)
        self.payment_id = payment_id

    def payment_id_from_message(self, message):
        if not message or not message.embeds:
            return None
        for field in message.embeds[0].fields:
            if field.name == "Payment record":
                import re
                match = re.search(r"PAY-(\\d+)", field.value)
                if match:
                    return int(match.group(1))
        return None

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅", custom_id="pay:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            return await interaction.response.send_message("⛔ You are not authorized to review payments.", ephemeral=True)
        payment_id = self.payment_id_from_message(interaction.message)
        if not payment_id:
            return await interaction.response.send_message("Could not identify this payment record.", ephemeral=True)
        await review_payment(interaction, payment_id, "APPROVED", "", interaction.message)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌", custom_id="pay:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            return await interaction.response.send_message("⛔ You are not authorized to review payments.", ephemeral=True)
        payment_id = self.payment_id_from_message(interaction.message)
        if not payment_id:
            return await interaction.response.send_message("Could not identify this payment record.", ephemeral=True)
        await interaction.response.send_modal(RejectReasonModal(payment_id, interaction.message))

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
        embed.add_field(name="Payment instruction", value="Use the payment type specified by the tournament admin. Keep your transaction ID private and enter it accurately.", inline=False)
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
        # Payment-specific review views are registered dynamically from IDs stored in DB below.
        with db() as conn:
            rows = conn.execute("SELECT id FROM payments WHERE status='PENDING'").fetchall()
        for row in rows:
            bot.add_view(ReviewView(row["id"]))
        bot._persistent_views_added = True
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

admin = app_commands.Group(name="payadmin", description="Payment bot administration")

@admin.command(name="setup", description="Configure payments, review channel, admin role and DM notification recipient")
@app_commands.describe(fee="Registration fee in BDT", bkash="bKash number", nagad="Nagad number", rocket="Rocket number",
                       review_channel="Private channel where payment requests are sent", admin_role="Optional role allowed to review payments",
                       notification_user="Member who receives a DM for every submission (defaults to you)")
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
        f"✅ Payment bot configured.\nFee: ৳{fee}\nReview channel: {review_channel.mention}\nAdmin role: {admin_role.mention if admin_role else 'Server admins only'}\nDM notifications: {notification_user.mention} (make sure this member allows server DMs)",
        ephemeral=True
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
    embed = discord.Embed(title=f"Payment PAY-{row['id']:06d}", color=discord.Color.blurple())
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
