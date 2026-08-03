---
name: Cricket template architecture
description: How the cricket tournament template is wired into the Nexora clone bot system.
---

# Cricket Template Architecture

## Entry points
- `templates/cricket/handlers.py` — all cricket logic; exports 5 functions
- `templates/cricket/__init__.py` — re-exports those functions
- `clonebot/handlers.py` — calls `register_cricket_handlers(app, bot_id)` inside `register_clone_handlers()`
- `mainbot/handlers.py` — seeds default questions on bot creation; auto-sets commands via `_configure_clone_bot_profile()`

## Routing rules
- `/start` in cricket bot → `handle_cricket_start()`
- `crik:*` callbacks → registered by `register_cricket_handlers()`
- `crikd:*` callbacks → date picker, also registered by `register_cricket_handlers()`
- `own:crik:*` callbacks → dispatched to `dispatch_cricket_owner_action()` from `_dispatch_owner_action()` in clonebot/handlers.py
- Text intake for wizard steps → checked in `intake()` before owner-only checks
- Owner text during admin setup (GC, image URL) → handled in `handle_cricket_owner_message()`

## DB models (all cascade-delete from bots)
- `cricket_tours` — active/inactive tournaments per bot
- `cricket_players` — registrations; status: pending/approved/rejected/waitlisted/deregistered
- `cricket_questions` — owner-configurable extra questions (role + base_price are hardcoded steps 1&2)
- `cricket_settings` — per-bot toggles + reg_end_date (stored UTC) + admin_gc (BigInt, nullable) + welcome_image_disabled (bool)

## Roles — exactly 3, no more
`ROLES = [("bat","Batter"), ("bowl","Bowler"), ("ar","All-rounder")]`
**Why:** User explicitly wanted only 3 roles. Do not add more without explicit approval.

## Admin GC feature
- `CricketSettings.admin_gc` stores a Telegram chat ID (group or channel).
- `_notify_admin()` sends to both owner DM and admin_gc if set (independent try/except).
- Approve/reject inline buttons are only sent to owner DM, not GC.
- Owner sets GC via `own:crik:setgc` → pending action `crik_set_admingc` → handles forward or @username or raw ID.

## Start image control
- `CricketSettings.welcome_image_disabled` — if True, skip photo send and send text only.
- `Bot.welcome_image` stores custom image URL; falls back to `DEFAULT_WELCOME_IMAGE` constant.
- Owner manages via `own:crik:imgmenu` → toggle disable / set URL / clear custom.

## Double emoji fix
- `btn()` with both text emoji in label AND `icon=` shows two emojis (Unicode + premium) when styled buttons are supported.
- Fix: never put emoji in text label when also passing `icon=`. cricket_owner_panel_kb() uses clean labels ("Tours", "Players", etc.) with icon= only.

## Wizard state machine
Stored in `clone_pending[(bot_id, user_id)]` as `PendingAction`:
- action=`crik_wizard`, step=`role` → show 3-role keyboard
- action=`crik_wizard`, step=`base_price` → await text input
- action=`crik_wizard`, step=`question` → advance through enabled DB questions
- action=`crik_confirm`, step=`confirm` → summary shown, await submit/redo
- action=`crik_set_admingc` → owner text handler for GC setup
- action=`crik_set_welcome_img` → owner text handler for image URL

## IST date picker
- `own:crik:setdate` → stores epoch in clone_pending action=`crik_setdate`
- `crikd:+d/-d/+h/-h/+m/-m` → mutate epoch, redraw keyboard
- `crikd:ok` → save UTC to cricket_settings.reg_end_date

**Why:** All timestamps stored UTC; IST display = UTC + 19800s.

## Auto-setup on creation
`_configure_clone_bot_profile()` in mainbot/handlers.py sets bot commands + description + short description immediately after clone starts. Failures are non-fatal.
