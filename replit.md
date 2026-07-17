# Nexora File Store

A Telegram bot platform that lets users create their own file-store or link-protect bots with no coding required.

## Stack

- **Runtime:** Python 3.12
- **Telegram libraries:** Pyrogram 2.0 + Kurigram 2.2 (MTProto, real button colours)
- **Database:** SQLAlchemy 2 async + asyncpg → Neon Postgres
- **Web dashboard:** Flask (live log viewer on port 5000)

## How to run

```
python main.py       # starts the main bot + all registered clone bots
python app.py        # starts the Flask log dashboard (port 5000)
```

## Required secrets (Replit Secrets)

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | From my.telegram.org — MTProto API ID |
| `TELEGRAM_API_HASH` | From my.telegram.org — MTProto API hash |
| `TELEGRAM_BOT_TOKEN` | Main bot token from @BotFather |
| `NEON_DATABASE_URL` | Neon Postgres connection string (postgres://…) |

## Optional secrets

| Variable | Default | Description |
|---|---|---|
| `MAIN_BOT_OWNER_ID` | `8186068163` | Telegram ID of the superadmin |
| `MAIN_LOG_CHANNEL_ID` | `0` (disabled) | Channel ID for platform-wide logs |

## Features

### Main bot
- `/start` — welcome screen with image
- `/newbot` — register a new clone bot (validates token via Telegram)
- Template selection: **File Store** or **Link Protect**
- `/mybots` — manage your bots
- `/admin` — superadmin panel (main owner only)

### Superadmin panel (`/admin`)
- Platform-wide stats (owners, bots, users)
- All bots / all owners listing
- Top bots by user count
- Broadcast to every user across all bots
- Recent owner action logs

### Clone bot — File Store
- Stores files (video, document, audio, photo, animation)
- Force-subscribe gate with verification
- Owner panel: channels, upload, broadcast, stats, logs, backup, settings

### Clone bot — Link Protect
- Owner adds URLs with titles; bot generates unique alias links
- Users access links via `/start lp_<token>` — gated behind force-subscribe
- Click tracking per link
- Owner panel: manage links, broadcast, stats, logs, backup, settings

## Bot images
- Welcome: `https://graph.org/file/4e9cfe6722a743d0a791e-010fd8c5e3567948b8.jpg`
- Admin panel: `https://graph.org/file/e8087a3300ad254ff93d7-c9325cbef36a60e2b4.jpg`
- Clone bot start: `https://graph.org/file/874c7523cf9fb087baae4-787a191131ca5d0bb7.jpg`

## Project structure

```
main.py             — entrypoint: starts main bot + all clone bots
app.py              — Flask live-log dashboard
config.py           — settings from env vars
bot_manager.py      — manages clone bot Client instances
keyboards.py        — shared button/keyboard helpers + emoji constants
mainbot/
  handlers.py       — main bot handlers + superadmin panel
clonebot/
  handlers.py       — clone bot handlers (file store + link protect)
database/
  engine.py         — async SQLAlchemy engine (Neon)
  models.py         — ORM models
utils/
  fsub.py           — force-subscribe membership check
  state.py          — in-memory conversation state
```

## User preferences
- Main owner Telegram ID: 8186068163
- Use button colours and emojis everywhere; avoid the orange shield emoji
- All three graph.org images used in welcome/admin/clone screens
- Keep existing project structure
