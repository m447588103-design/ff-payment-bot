# FF Esports Payment Verification Bot

Standalone Discord payment-verification bot for an existing tournament/sports bot. It does **not** create tournaments or change your existing sports bot.

## Features
- Player payment panel with bKash, Nagad and Rocket selection
- Shows configured registration fee and payment number
- Modal collects registration/player ID, tournament name, sender number and Transaction ID
- Posts each submission to the private admin review channel **and DMs the configured admin**
- The review DM contains the full submission plus **Approve / Reject buttons**, so an admin can act straight from the DM
- Reject asks for a reason; Approve records who reviewed it and when
- After a decision both the channel message and the DM lose their buttons and show the result, so a second admin cannot flip it
- If the DM cannot be delivered (DMs disabled), the submission still arrives in the review channel and the bot says so
- DMs the player the review result
- `/paystatus` lets a player privately check **their own** latest payment or a specific `PAY-000000` record (another player's record is reported as not found)
- DMs the configured admin again while a payment is still **PENDING** — first reminder after 30 minutes, then about every 30 minutes until it is reviewed
- Unique Transaction ID constraint blocks duplicate submissions
- SQLite persistence, stats and payment lookup slash commands
- Server admin / Manage Server checks and optional payment-admin role
- Slash commands are locked to server admins; DM buttons are locked to the configured recipient (see "Who can approve")

## Important — this is manual verification
This bot **cannot** verify a bKash / Nagad / Rocket transfer by itself. Personal wallet accounts have no official merchant API the bot could query, so there is no way to confirm a transfer automatically and this project deliberately does not pretend otherwise.

The Approve / Reject buttons are a **shortcut for a human decision**, nothing more. Before approving, an admin must check the actual wallet transaction — the bKash / Nagad / Rocket app or statement — and confirm:
- money actually arrived, and
- the amount matches the registration fee, and
- the Transaction ID matches the one submitted.

Every review embed repeats this reminder, and the bot never sends a player a message claiming automatic verification.

Approving this standalone bot does not automatically update your existing sports bot unless you later integrate a shared database or API.

## Pending payment reminders
The admin who receives the review DM is reminded until the submission is reviewed:
- the first reminder arrives once a payment has been pending for **30 minutes**;
- further reminders repeat about every **30 minutes** while it stays PENDING;
- reviewing the payment (Approve/Reject) stops the reminders immediately.

Reminder DMs are informational: they repeat the record number, player, amount, method, Transaction ID and how long it has been waiting, and they link back to the review message so the Approve/Reject buttons stay in one place. The bot checks for due reminders every 5 minutes, and each payment is stamped with its last reminder time so the same payment is never reminded twice inside 30 minutes — that also holds across a restart, because the stamp is stored in SQLite.

If the recipient's DMs are closed, the bot records the attempt (so it will not retry on every sweep), posts one warning in the review channel and keeps the submission reviewable from the channel. With no recipient configured, reminders are skipped and the submission stays reviewable in the channel, exactly like submission DMs.

## Who can approve
- **In the review channel:** server administrators, members with Manage Server, or members holding the configured payment-admin role.
- **In the DM:** the configured review recipient, plus any server admin the bot can still resolve through `GUILD_ID`.
- A DM carries no permissions of its own, so a member who is not in the list above is refused with "You are not authorized to review payments."

## Run locally (Windows / VS Code)
1. Install Python 3.11+.
2. Create a Discord application and bot at https://discord.com/developers/applications
3. Enable the bot and invite it with `bot` + `applications.commands` scopes. Grant Send Messages, Embed Links, Use Application Commands, and View Channels. Do not give Administrator unless necessary.
4. Copy `.env.example` to `.env`; set `DISCORD_TOKEN` and `GUILD_ID`.
5. Open a terminal in this folder and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python bot.py
   ```

6. In your Discord server, create a private channel such as `#payment-verification`. Give the bot View Channel, Send Messages, Embed Links and Read Message History permissions.
7. Run `/payadmin setup` as a server administrator. Set fee, all payment numbers, review channel, and optionally an admin role. By default, the admin who runs setup receives the Approve/Reject DM for every new payment submission; use the optional `notification_user` choice to send those DMs to another server member, or set `NOTIFICATION_USER_ID` to that member's Discord user ID.
   - The recipient must allow DMs from server members. If a DM is blocked, the submission still appears in the review channel and the bot posts a warning there.
8. Run `/payment_panel` in the channel where players should submit payments.

Restarting the bot is safe: pending submissions keep working, because the Approve/Reject buttons are persistent and carry their own payment ID.

## Slash commands
- `/payadmin setup` — configure fee, numbers, review channel, optional admin role and DM notification recipient (defaults to the admin running setup)
- `/payment_panel` — post the player payment panel
- `/paystatus [record:<PAY-000012 or TrxID>]` — **player command**, no admin rights needed: privately shows your own latest payment (no value) or one specific record you submitted. Every reply is ephemeral and every query is filtered to your own user ID, so nobody can read another player's submission with it; admins keep using `/payadmin lookup`
- `/payadmin stats` — show total/pending/approved/rejected counts
- `/payadmin lookup query:<TrxID or PAY-000001>` — look up any submission (admin only)

## Configuration
Everything can be set with `/payadmin setup`; environment variables are the defaults for a fresh database. See `.env.example`.

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Bot token. Keep secret; `.env` is git-ignored. |
| `GUILD_ID` | Server where slash commands are synced. |
| `DATABASE_PATH` | SQLite file, default `data/payments.sqlite3`. |
| `NOTIFICATION_USER_ID` | Member who receives the review DM. |
| `REVIEW_CHANNEL_ID`, `REGISTRATION_FEE`, `BKASH_NUMBER`, `NAGAD_NUMBER`, `ROCKET_NUMBER`, `ADMIN_ROLE_ID` | Initial settings values. |

Never upload `.env` or your token to GitHub.

## Tests
The repository ships offline checks that need no token and no Discord connection: schema/migration, button custom IDs, permission rules, embed wording, a full simulated flow (submit → review-channel post → reviewer DM with buttons → approve/reject → player notification), `/paystatus` privacy and the pending-reminder cadence (first reminder at 30 minutes, repeat gated to one per 30 minutes, stopped by a review, blocked-DM handling).

```powershell
pip install pytest
python tests/test_review_flow.py                    # no test runner required
python tests/test_end_to_end_flow.py
python tests/test_payment_status_and_reminders.py
python -m pytest tests -q
```

## Hosting
The included Dockerfile and Render worker blueprint are a starting point. SQLite data on ephemeral hosting storage may be lost on redeploy/restart. For persistent production use, attach a persistent disk or migrate to PostgreSQL before relying on long-term records.

## Existing sports bot integration
To make approval automatically update your tournament bot, upload that project's ZIP/source. The integration method depends on its framework and database; this standalone bot intentionally does not guess or modify that system.
