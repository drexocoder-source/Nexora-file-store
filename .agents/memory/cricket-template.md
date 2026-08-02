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
- Text intake for wizard steps (any user) → checked in `intake()` handler before owner-only checks

## DB models (all cascade-delete from bots)
- `cricket_tours` — active/inactive tournaments per bot
- `cricket_players` — registrations; status: pending/approved/rejected/waitlisted/deregistered
- `cricket_questions` — owner-configurable extra questions (role + base_price are hardcoded steps 1&2)
- `cricket_settings` — per-bot toggles + reg_end_date (stored UTC)

## Wizard state machine
Stored in `clone_pending[(bot_id, user_id)]` as `PendingAction`:
- action=`crik_wizard`, step=`role` → show role keyboard
- action=`crik_wizard`, step=`base_price` → await text input
- action=`crik_wizard`, step=`question` → advance through enabled DB questions
- action=`crik_confirm`, step=`confirm` → summary shown, await submit/redo

## IST date picker
- `own:crik:setdate` → stores epoch in clone_pending action=`crik_setdate`
- `crikd:+d/-d/+h/-h/+m/-m` → mutate epoch, redraw keyboard
- `crikd:ok` → save UTC to cricket_settings.reg_end_date

**Why:** All timestamps stored UTC; IST display = UTC + 19800s. reg_end_date is checked on every /start and wizard init.

## Auto-setup on creation
`_configure_clone_bot_profile()` in mainbot/handlers.py sets bot commands + description + short description (about) immediately after clone starts. Failures are non-fatal (try/except with warning log).
