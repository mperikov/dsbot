# Discord Reaction Role Bot

This bot watches reactions on a moderator-selected message:

- when a user adds the configured reaction -> the bot gives a configured role;
- when a user removes the configured reaction -> the bot removes that role.
- moderator can configure everything via Discord slash command.

## Setup

1. Create a Discord bot in the Developer Portal and invite it to your server.
2. Give the bot permissions:
   - Manage Roles
   - Read Message History
   - View Channels
4. Ensure the bot's role is higher than the role it should assign.

## Configuration

Create `.env` and set values:

- `DISCORD_TOKEN` - bot token
- `CONFIG_PATH` - optional path to the file with per-server bindings

## Run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set DISCORD_TOKEN=your_bot_token
python bot.py
```

## Moderator commands (inside Discord)

- `/bind_reaction channel:<#channel> message_ref:<id_or_link> role:<@role> emoji:<emoji>`
  - ID example: `/bind_reaction #roles 123456789012345678 @Member ✅`
  - Link example: `/bind_reaction #roles https://discord.com/channels/<guild>/<channel>/<message> @Member ✅`
- `/reaction_bind_info` - show current binding for this server
- `/unbind_reaction` - remove current binding
