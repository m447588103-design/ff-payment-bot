# FF Esports Payment Verification Bot

Standalone Discord payment-verification bot for an existing tournament/sports bot. It does **not** create tournaments or change your existing sports bot.

## Features
- Player payment panel with bKash, Nagad and Rocket selection
- Shows configured registration fee and payment number
- Modal collects registration/player ID, tournament name, sender number and Transaction ID
- Sends each submission to a private admin review channel
- Admin Approve / Reject-with-reason buttons
- DMs player with review result
- Unique Transaction ID constraint blocks duplicate submissions
- SQLite persistence, stats and payment lookup slash commands
- Server admin / Manage Server checks and optional payment-admin role

## Important
This is **manual verification**. The bot does not independently verify a bKash/Nagad/Rocket transfer. An admin must check the actual account/app/merchant statement before approving. Approving this standalone bot does not automatically update your existing sports bot unless you later integrate a shared database or API.

## Run locally (Windows / VS Code)
1. Install Python 3.11+.
2. Create a Discord application and bot at https://discord.com/developers/applications
3. Enable the bot and invite it with `bot` + `applications.commands` scopes. Grant Send Messages, Embed Links, Use Application Commands, and View Channels. Do not give Administrator unless necessary.
4. Copy `.env.example` to `.env`; set `DISCORD_TOKEN` and `GUILD_ID`.
5. Open terminal in this folder and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python bot.py
   ```

6. In your Discord server, create a private channel such as `#payment-verification`. Give the bot View Channel, Send Messages, Embed Links and Read Message History permissions.
7. Run `/payadmin setup` as a server administrator. Set fee, all payment numbers, review channel, and optionally an admin role.
8. Run `/payment_panel` in the channel where players should submit payments.

## Slash commands
- `/payadmin setup` — configure fee, numbers, review channel, optional admin role
- `/payment_panel` — post the player payment panel
- `/payadmin stats` — show total/pending/approved/rejected counts
- `/payadmin lookup query:<TrxID or PAY-000001>` — look up a submission

## Hosting
The included Dockerfile and Render worker blueprint are a starting point. SQLite data on ephemeral hosting storage may be lost on redeploy/restart. For persistent production use, attach a persistent disk or migrate to PostgreSQL before relying on long-term records. Keep `DISCORD_TOKEN` secret; never upload `.env` to GitHub.

## Existing sports bot integration
To make approval automatically update your tournament bot, upload that project's ZIP/source. The integration method depends on its framework and database; this standalone bot intentionally does not guess or modify that system.
