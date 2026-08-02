"""Cricket Tournament template — full handler implementation.

Exports
-------
register_cricket_handlers(app, bot_id)
    Registers crik:* callback handlers + wizard text intake on `app`.
    Call once per clone client inside register_clone_handlers().

handle_cricket_start(client, message, bot_id, bot_row, is_owner, first_time)
    Entry-point called from the shared /start handler when bot_type=="cricket".

dispatch_cricket_owner_action(client, user_id, target, action, bot_id)
    Handles own:crik:* owner-panel actions dispatched from owner_callback.

handle_cricket_wizard_message(client, message, bot_id, user_id, pending)
    Handles free-text / numeric replies during the registration wizard.

seed_default_questions(bot_id)
    Inserts the default optional question set for a freshly created cricket bot.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from math import ceil

from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from database.engine import AsyncSessionLocal
from database.models import (
    Bot as BotModel,
    CricketPlayer,
    CricketQuestion,
    CricketSettings,
    CricketTour,
    OwnerLog,
    Owner,
)
from keyboards import (
    BLUE, DANGER, DEFAULT, GREEN, PRIMARY, RED, SUCCESS, YELLOW,
    EMOJI_BELL, EMOJI_CHART, EMOJI_CHECK, EMOJI_CROWN, EMOJI_FIRE,
    EMOJI_GUARD, EMOJI_MIC, EMOJI_OCTAGON, EMOJI_SIREN,
    EMOJI_SPARKLE, EMOJI_STAR, EMOJI_TOOLS, EMOJI_TRASH, EMOJI_TROPHY,
    EMOJI_X, cricket_owner_panel_kb,
    TXT_ERR, TXT_INFO, TXT_WARN, TXT_OK,
    btn, quote, yes_no_kb,
)
from utils.state import PendingAction, clone_pending

log = logging.getLogger("nexora.cricket")

# ── Constants ─────────────────────────────────────────────────────────────────
IST = timedelta(hours=5, minutes=30)
PAGE_SIZE = 8

ROLES = [
    ("bat",  "🏏 Batter"),
    ("bowl", "🎳 Bowler"),
    ("ar",   "⚡ All-rounder"),
    ("wk",   "🧤 Wicket-keeper"),
    ("fld",  "🏃 Fielder"),
]
ROLE_LABELS = {k: v for k, v in ROLES}

STATUS_EMOJI = {
    "pending":      "⏳",
    "approved":     "✅",
    "rejected":     "❌",
    "waitlisted":   "⏸️",
    "deregistered": "🗑️",
}

DEFAULT_QUESTIONS = [
    {"key": "batting_hand",  "label": "Batting Hand",       "input_type": "choice",
     "choices": ["Left-handed", "Right-handed"],            "enabled": True,  "required": False, "order_index": 0},
    {"key": "bowling_type",  "label": "Bowling Type",       "input_type": "choice",
     "choices": ["Fast", "Medium", "Spin", "N/A"],          "enabled": True,  "required": False, "order_index": 1},
    {"key": "experience",    "label": "Years of Experience","input_type": "number",
     "choices": [],                                          "enabled": False, "required": False, "order_index": 2},
    {"key": "city",          "label": "City / Location",    "input_type": "text",
     "choices": [],                                          "enabled": False, "required": False, "order_index": 3},
]


# ── IST helpers ───────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + IST


def _fmt_ist(dt_utc: datetime) -> str:
    ist = dt_utc.replace(tzinfo=timezone.utc) + IST
    return ist.strftime("%d %b %Y, %I:%M %p IST")


def _epoch_to_utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _utc_to_epoch(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_or_create_settings(bot_id: int, session) -> CricketSettings:
    s = await session.get(CricketSettings, bot_id)
    if s is None:
        s = CricketSettings(bot_id=bot_id)
        session.add(s)
        await session.flush()
    return s


async def _get_active_tour(bot_id: int) -> CricketTour | None:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(CricketTour)
            .where(CricketTour.bot_id == bot_id, CricketTour.active.is_(True))
            .order_by(CricketTour.created_at.desc()).limit(1)
        )
        return r.scalar_one_or_none()


async def _get_enabled_questions(bot_id: int) -> list[CricketQuestion]:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(CricketQuestion)
            .where(CricketQuestion.bot_id == bot_id, CricketQuestion.enabled.is_(True))
            .order_by(CricketQuestion.order_index)
        )
        return list(r.scalars().all())


async def _get_all_questions(bot_id: int) -> list[CricketQuestion]:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(CricketQuestion)
            .where(CricketQuestion.bot_id == bot_id)
            .order_by(CricketQuestion.order_index)
        )
        return list(r.scalars().all())


async def _get_owner_tg_id(bot_id: int) -> int | None:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(Owner.telegram_id)
            .join(BotModel, BotModel.owner_id == Owner.id)
            .where(BotModel.id == bot_id)
        )
        return r.scalar_one_or_none()


async def _log_cricket(client: Client, bot_id: int, text: str) -> None:
    async with AsyncSessionLocal() as session:
        bot_row = await session.get(BotModel, bot_id)
        if bot_row and bot_row.log_channel:
            try:
                await client.send_message(bot_row.log_channel, text)
            except RPCError:
                pass


async def _record_action(bot_id: int, action: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(OwnerLog(bot_id=bot_id, action=action))
        await session.commit()


async def _player_of_user(bot_id: int, user_id: int) -> CricketPlayer | None:
    """Return the most recent non-deregistered player record for this user."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(CricketPlayer)
            .where(
                CricketPlayer.bot_id == bot_id,
                CricketPlayer.user_id == user_id,
                CricketPlayer.status != "deregistered",
            )
            .order_by(CricketPlayer.registered_at.desc())
            .limit(1)
        )
        return r.scalar_one_or_none()


# ── Seed default questions ────────────────────────────────────────────────────

async def seed_default_questions(bot_id: int) -> None:
    """Insert default questions for a freshly created cricket bot."""
    async with AsyncSessionLocal() as session:
        existing = await session.scalar(
            select(func.count()).select_from(CricketQuestion)
            .where(CricketQuestion.bot_id == bot_id)
        )
        if existing:
            return
        for q in DEFAULT_QUESTIONS:
            session.add(CricketQuestion(
                bot_id=bot_id,
                key=q["key"],
                label=q["label"],
                input_type=q["input_type"],
                choices=json.dumps(q["choices"]) if q["choices"] else None,
                enabled=q["enabled"],
                required=q["required"],
                order_index=q["order_index"],
            ))
        await session.commit()


# ── Welcome / start screen ────────────────────────────────────────────────────

async def handle_cricket_start(
    client: Client,
    message: Message,
    bot_id: int,
    bot_row: BotModel,
    is_owner: bool,
    first_time: bool,
) -> None:
    """Called from clone_start when bot_type == 'cricket'."""
    user_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        settings = await _get_or_create_settings(bot_id, session)
        auto_app = settings.auto_approve
        allow_cap = settings.allow_captain_reg
        reg_end = settings.reg_end_date
        await session.commit()

    # Check registration deadline
    reg_open = True
    deadline_text = ""
    if reg_end:
        if datetime.now(timezone.utc) > reg_end:
            reg_open = False
            deadline_text = f"\n\n🔒 **Registration closed** — ended {_fmt_ist(reg_end)}"
        else:
            deadline_text = f"\n\n📅 **Registration closes:** {_fmt_ist(reg_end)}"

    active_tour = await _get_active_tour(bot_id)
    tour_line = f"\n🏆 **Active Tour:** {active_tour.name}" if active_tour else ""

    bot_name = bot_row.bot_name or bot_row.bot_username or "Cricket Bot"

    # Check if user already registered
    existing = await _player_of_user(bot_id, user_id) if not is_owner else None

    greeting = (
        bot_row.welcome_caption
        or f"🏏 **Welcome to {bot_name}!**\n\nRegister for the cricket tournament and showcase your skills."
    )

    caption = f"{greeting}{tour_line}{deadline_text}"

    # Build keyboard
    rows = []

    if is_owner:
        rows.append([btn(PRIMARY, "👑 Owner Panel", "own:crik:home", icon=EMOJI_CROWN)])
    elif existing:
        st = STATUS_EMOJI.get(existing.status, "❓")
        role_label = ROLE_LABELS.get(existing.role or "", existing.role or "—")
        rows.append([btn(
            GREEN if existing.status == "approved" else YELLOW,
            f"{st} My Status — {role_label}",
            "crik:mystatus",
        )])
        if existing.status in ("rejected", "deregistered"):
            if reg_open:
                rows.append([btn(PRIMARY, "🏏 Register Again", "crik:ryes", icon=EMOJI_SPARKLE)])
    elif reg_open:
        rows.append([btn(PRIMARY, "🏏 Register as Player", "crik:ryes", icon=EMOJI_SPARKLE)])
        if allow_cap:
            rows.append([btn(YELLOW, "👑 Register as Captain", "crik:cap", icon=EMOJI_CROWN)])

    try:
        await message.reply_photo(
            bot_row.welcome_image or "https://graph.org/file/874c7523cf9fb087baae4-787a191131ca5d0bb7.jpg",
            caption=caption,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            reply_parameters=quote(message.id),
        )
    except RPCError:
        await message.reply_text(
            caption,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            reply_parameters=quote(message.id),
        )


# ── Registration wizard ───────────────────────────────────────────────────────

async def _start_wizard(
    client: Client,
    user_id: int,
    target: Message,
    bot_id: int,
    is_captain: bool,
) -> None:
    """Kick off the step-by-step registration wizard."""
    async with AsyncSessionLocal() as session:
        settings = await _get_or_create_settings(bot_id, session)
        reg_end = settings.reg_end_date
        max_p = settings.max_players
        max_c = settings.max_captains
        await session.commit()

    # Deadline check
    if reg_end and datetime.now(timezone.utc) > reg_end:
        await target.reply_text(f"🔒 Registration is **closed** — ended {_fmt_ist(reg_end)}.")
        return

    # Capacity check
    if is_captain and max_c > 0:
        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                select(func.count()).select_from(CricketPlayer)
                .where(
                    CricketPlayer.bot_id == bot_id,
                    CricketPlayer.is_captain.is_(True),
                    CricketPlayer.status.in_(["pending", "approved"]),
                )
            )
        if count >= max_c:
            await target.reply_text(f"⛔ Captain slots are full ({max_c}/{max_c}). Contact the admin.")
            return
    elif not is_captain and max_p > 0:
        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                select(func.count()).select_from(CricketPlayer)
                .where(
                    CricketPlayer.bot_id == bot_id,
                    CricketPlayer.is_captain.is_(False),
                    CricketPlayer.status.in_(["pending", "approved"]),
                )
            )
        if count >= max_p:
            await target.reply_text(f"⛔ Player slots are full ({max_p}/{max_p}). Contact the admin.")
            return

    questions = await _get_enabled_questions(bot_id)
    clone_pending[(bot_id, user_id)] = PendingAction("crik_wizard", {
        "step": "role",
        "is_captain": is_captain,
        "answers": {},
        "questions": [q.id for q in questions],
        "q_index": 0,
    })

    cap_label = "👑 **Captain**" if is_captain else "🏏 **Player**"
    await target.reply_text(
        f"✨ **Starting Registration — {cap_label}**\n\n"
        "Step 1 of 2+\n\n"
        "🎯 **Select your role:**",
        reply_markup=_role_kb(),
    )


def _role_kb() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(ROLES), 2):
        row = [btn(PRIMARY, ROLES[i][1], f"crik:role:{ROLES[i][0]}")]
        if i + 1 < len(ROLES):
            row.append(btn(PRIMARY, ROLES[i + 1][1], f"crik:role:{ROLES[i + 1][0]}"))
        rows.append(row)
    rows.append([btn(DANGER, "❌ Cancel", "crik:cancel", icon=EMOJI_X)])
    return InlineKeyboardMarkup(rows)


async def _wizard_role_selected(
    client: Client, cq: CallbackQuery, bot_id: int, user_id: int,
    role_key: str, pending: PendingAction,
) -> None:
    pending.data["answers"]["role"] = role_key
    pending.data["step"] = "base_price"
    role_label = ROLE_LABELS.get(role_key, role_key)
    clone_pending[(bot_id, user_id)] = pending
    await cq.message.edit_text(
        f"✅ Role: **{role_label}**\n\n"
        "Step 2 of 2+\n\n"
        "💰 **Enter your base price (₹):**\n"
        "Send a number — e.g. `500`",
        reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "crik:cancel", icon=EMOJI_X)]]),
    )


async def handle_cricket_wizard_message(
    client: Client,
    message: Message,
    bot_id: int,
    user_id: int,
    pending: PendingAction,
) -> None:
    """Handle free-text input during the wizard (base_price, custom text/number questions)."""
    step = pending.data.get("step")
    text = (message.text or "").strip()

    if step == "base_price":
        # Validate numeric
        cleaned = text.replace("₹", "").replace(",", "").strip()
        if not cleaned.isdigit():
            await message.reply_text(
                f"{TXT_ERR} Please send a **number** only — e.g. `500`\n\nNo symbols needed.",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "crik:cancel", icon=EMOJI_X)]]),
            )
            return
        pending.data["answers"]["base_price"] = f"₹{int(cleaned)}"
        await _advance_to_next_question(client, message, bot_id, user_id, pending)

    elif step == "question":
        q_index = pending.data.get("q_index", 0)
        q_ids = pending.data.get("questions", [])
        if q_index >= len(q_ids):
            await _finish_wizard(client, message, bot_id, user_id, pending)
            return

        async with AsyncSessionLocal() as session:
            q = await session.get(CricketQuestion, q_ids[q_index])

        if q and q.input_type == "number":
            cleaned = text.replace(",", "").strip()
            if not cleaned.isdigit():
                await message.reply_text(
                    f"{TXT_ERR} Please send a **number** for \"{q.label}\".",
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "crik:cancel", icon=EMOJI_X)]]),
                )
                return
            pending.data["answers"][q.key] = cleaned
        else:
            pending.data["answers"][q.key if q else f"q{q_index}"] = text[:200]

        pending.data["q_index"] = q_index + 1
        await _advance_to_next_question(client, message, bot_id, user_id, pending)

    clone_pending[(bot_id, user_id)] = pending


async def _advance_to_next_question(
    client: Client,
    target: Message,
    bot_id: int,
    user_id: int,
    pending: PendingAction,
) -> None:
    """Move to the next enabled question or finish."""
    q_ids = pending.data.get("questions", [])
    q_index = pending.data.get("q_index", 0)

    if q_index >= len(q_ids):
        await _finish_wizard(client, target, bot_id, user_id, pending)
        return

    async with AsyncSessionLocal() as session:
        q = await session.get(CricketQuestion, q_ids[q_index])

    if q is None:
        pending.data["q_index"] = q_index + 1
        await _advance_to_next_question(client, target, bot_id, user_id, pending)
        return

    pending.data["step"] = "question"
    clone_pending[(bot_id, user_id)] = pending
    total_steps = 2 + len(q_ids)
    step_num = 2 + q_index + 1

    if q.input_type == "choice" and q.choices:
        choices = json.loads(q.choices)
        rows = []
        for i in range(0, len(choices), 2):
            row = [btn(PRIMARY, choices[i], f"crik:ch:{q_index}:{i}")]
            if i + 1 < len(choices):
                row.append(btn(PRIMARY, choices[i + 1], f"crik:ch:{q_index}:{i+1}"))
            rows.append(row)
        if not q.required:
            rows.append([btn(DEFAULT, "⏭ Skip", f"crik:skip:{q_index}")])
        rows.append([btn(DANGER, "❌ Cancel", "crik:cancel", icon=EMOJI_X)])
        await target.reply_text(
            f"Step {step_num} of {total_steps}\n\n"
            f"❓ **{q.label}:**",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    else:
        type_hint = "number" if q.input_type == "number" else "text"
        opt_note = " _(optional — send /skip to skip)_" if not q.required else ""
        await target.reply_text(
            f"Step {step_num} of {total_steps}\n\n"
            f"❓ **{q.label}:**{opt_note}\n"
            f"Send a {type_hint}.",
            reply_markup=InlineKeyboardMarkup(
                [[btn(DEFAULT, "⏭ Skip", f"crik:skip:{q_index}")]] if not q.required
                else [] + [[btn(DANGER, "❌ Cancel", "crik:cancel", icon=EMOJI_X)]]
            ),
        )


async def _finish_wizard(
    client: Client,
    target: Message,
    bot_id: int,
    user_id: int,
    pending: PendingAction,
) -> None:
    """Show summary card and ask for confirmation."""
    answers = pending.data.get("answers", {})
    is_captain = pending.data.get("is_captain", False)

    role_key = answers.get("role", "?")
    role_label = ROLE_LABELS.get(role_key, role_key)
    base_price = answers.get("base_price", "—")

    lines = [
        "📋 **Registration Summary**\n",
        f"🎭 Type: {'👑 Captain' if is_captain else '🏏 Player'}",
        f"🎯 Role: {role_label}",
        f"💰 Base Price: {base_price}",
    ]

    # Additional answers
    for k, v in answers.items():
        if k in ("role", "base_price"):
            continue
        # Try to get the label from DB
        lines.append(f"• {k.replace('_', ' ').title()}: {v}")

    lines.append("\n✅ **Confirm and submit your registration?**")

    clone_pending[(bot_id, user_id)] = PendingAction("crik_confirm", {
        **pending.data,
        "step": "confirm",
    })

    await target.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [
                btn(GREEN,  "✅ Submit",  "crik:submit",  icon=EMOJI_CHECK),
                btn(DANGER, "✏️ Redo",   "crik:redo",    icon=EMOJI_X),
            ]
        ]),
    )


async def _submit_registration(
    client: Client,
    cq: CallbackQuery,
    bot_id: int,
    user_id: int,
    pending: PendingAction,
) -> None:
    """Save the player to DB, notify owner, send confirmation."""
    answers = pending.data.get("answers", {})
    is_captain = pending.data.get("is_captain", False)

    async with AsyncSessionLocal() as session:
        settings = await _get_or_create_settings(bot_id, session)
        auto_approve = settings.auto_approve
        active_tour_q = await session.execute(
            select(CricketTour)
            .where(CricketTour.bot_id == bot_id, CricketTour.active.is_(True))
            .order_by(CricketTour.created_at.desc()).limit(1)
        )
        tour = active_tour_q.scalar_one_or_none()
        await session.commit()

    role_key = answers.get("role")
    base_price = answers.get("base_price")
    extras = {k: v for k, v in answers.items() if k not in ("role", "base_price")}

    initial_status = "approved" if auto_approve else "pending"

    async with AsyncSessionLocal() as session:
        player = CricketPlayer(
            bot_id=bot_id,
            tour_id=tour.id if tour else None,
            user_id=user_id,
            username=cq.from_user.username,
            full_name=cq.from_user.first_name,
            role=role_key,
            is_captain=is_captain,
            base_price=base_price,
            status=initial_status,
            answers=json.dumps(extras) if extras else None,
        )
        session.add(player)
        await session.flush()
        player_id = player.id
        await session.commit()

    clone_pending.pop((bot_id, user_id), None)

    role_label = ROLE_LABELS.get(role_key or "", role_key or "—")
    cap_label = "👑 Captain" if is_captain else "🏏 Player"

    if auto_approve:
        await cq.message.edit_text(
            f"✅ **Registration Approved!**\n\n"
            f"Welcome aboard, {cq.from_user.first_name}! 🎉\n\n"
            f"🎭 Type: {cap_label}\n"
            f"🎯 Role: {role_label}\n"
            f"💰 Base Price: {base_price}\n"
            + (f"🏆 Tour: {tour.name}" if tour else ""),
        )
    else:
        await cq.message.edit_text(
            f"⏳ **Registration Submitted!**\n\n"
            f"Hi {cq.from_user.first_name}, your registration is under review.\n"
            "You'll be notified once the admin approves it.\n\n"
            f"🎭 Type: {cap_label}\n"
            f"🎯 Role: {role_label}\n"
            f"💰 Base Price: {base_price}",
        )

    # Notify owner
    handle = f"@{cq.from_user.username}" if cq.from_user.username else f"id:{user_id}"
    owner_tg_id = await _get_owner_tg_id(bot_id)

    notify_text = (
        f"🏏 **New {'Captain' if is_captain else 'Player'} Registration**\n\n"
        f"👤 Name: {cq.from_user.first_name} ({handle})\n"
        f"🎯 Role: {role_label}\n"
        f"💰 Base Price: {base_price}\n"
        + (f"🏆 Tour: {tour.name}\n" if tour else "")
        + (f"📋 Status: {'Auto-approved ✅' if auto_approve else 'Pending approval ⏳'}")
    )

    if not auto_approve and owner_tg_id:
        try:
            await client.send_message(
                owner_tg_id,
                notify_text,
                reply_markup=InlineKeyboardMarkup([
                    [
                        btn(GREEN,  "✅ Approve",   f"crik:apr:{player_id}", icon=EMOJI_CHECK),
                        btn(DANGER, "❌ Reject",    f"crik:rej:{player_id}", icon=EMOJI_X),
                    ],
                    [btn(YELLOW, "⏸️ Waitlist",    f"crik:wl:{player_id}",  icon=EMOJI_BELL)],
                ]),
            )
        except RPCError:
            pass

    await _log_cricket(client, bot_id, notify_text + f"\nPlayer DB ID: {player_id}")
    await _record_action(bot_id, f"{'Captain' if is_captain else 'Player'} registered: {cq.from_user.first_name} ({role_label})")


# ── Approval callbacks ────────────────────────────────────────────────────────

async def _handle_approval(
    client: Client, cq: CallbackQuery, action: str, player_id: int, bot_id: int
) -> None:
    status_map = {"apr": "approved", "rej": "rejected", "wl": "waitlisted"}
    new_status = status_map.get(action)
    if not new_status:
        return

    async with AsyncSessionLocal() as session:
        player = await session.get(CricketPlayer, player_id)
        if player is None or player.bot_id != bot_id:
            await cq.answer("Player not found.", show_alert=True)
            return
        old_status = player.status
        player.status = new_status
        await session.commit()
        p_name = player.full_name or f"id:{player.user_id}"
        p_uid = player.user_id
        role_label = ROLE_LABELS.get(player.role or "", player.role or "—")
        is_cap = player.is_captain

    await cq.answer(f"{'✅' if new_status == 'approved' else '❌'} {new_status.title()}!", show_alert=False)

    status_emoji = STATUS_EMOJI.get(new_status, "")
    cap_label = "Captain" if is_cap else "Player"

    # Edit the owner notification message
    try:
        await cq.message.edit_text(
            cq.message.text + f"\n\n{status_emoji} **{new_status.upper()}** by admin",
        )
    except RPCError:
        pass

    # DM the player
    if new_status == "approved":
        dm_text = (
            f"🎉 **Registration Approved!**\n\n"
            f"Hi {p_name}! Your {cap_label.lower()} registration has been approved.\n"
            f"Role: {role_label}\nWelcome to the tournament! 🏏"
        )
    elif new_status == "waitlisted":
        dm_text = (
            f"⏸️ **You've been Waitlisted**\n\n"
            f"Hi {p_name}, you're on the waitlist for this tournament.\n"
            "You'll be notified if a spot opens up. Stay ready! 🏏"
        )
    else:
        dm_text = (
            f"❌ **Registration Not Approved**\n\n"
            f"Hi {p_name}, unfortunately your {cap_label.lower()} registration\n"
            "was not approved at this time. You may register again for future tournaments."
        )

    try:
        await client.send_message(p_uid, dm_text)
    except RPCError:
        pass

    await _log_cricket(
        client, bot_id,
        f"{status_emoji} **Player {new_status.title()}**\n"
        f"Name: {p_name}  Role: {role_label}  Type: {cap_label}\n"
        f"By admin on {_now_ist().strftime('%d %b %Y, %I:%M %p IST')}"
    )
    await _record_action(bot_id, f"{cap_label} {new_status}: {p_name} ({role_label})")


# ── My status callback ────────────────────────────────────────────────────────

async def _handle_my_status(client: Client, cq: CallbackQuery, bot_id: int) -> None:
    player = await _player_of_user(bot_id, cq.from_user.id)
    if not player:
        await cq.answer("No registration found.", show_alert=True)
        return
    role_label = ROLE_LABELS.get(player.role or "", player.role or "—")
    st = STATUS_EMOJI.get(player.status, "?")
    extras = json.loads(player.answers) if player.answers else {}
    lines = [
        f"📋 **Your Registration**\n",
        f"🎭 Type: {'👑 Captain' if player.is_captain else '🏏 Player'}",
        f"🎯 Role: {role_label}",
        f"💰 Base Price: {player.base_price or '—'}",
    ]
    for k, v in extras.items():
        lines.append(f"• {k.replace('_', ' ').title()}: {v}")
    lines.append(f"\n{st} Status: **{player.status.title()}**")
    lines.append(f"📅 Registered: {_fmt_ist(player.registered_at)}")
    await cq.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "crik:back", icon=EMOJI_OCTAGON)]]),
    )


# ── Paginated list helper ─────────────────────────────────────────────────────

def _paginate_kb(action_prefix: str, page: int, total: int, back_cb: str) -> list:
    """Return prev/next row + back button for pagination."""
    rows = []
    nav = []
    if page > 0:
        nav.append(btn(PRIMARY, "◀ Prev", f"{action_prefix}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(btn(PRIMARY, "Next ▶", f"{action_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn(YELLOW, "🔙 Back", back_cb, icon=EMOJI_OCTAGON)])
    return rows


# ── Owner panel sections ──────────────────────────────────────────────────────

async def _render_home(target: Message) -> None:
    try:
        await target.edit_text(
            "🏏 **Cricket Owner Panel**\n\nManage your tournament.",
            reply_markup=cricket_owner_panel_kb(),
        )
    except RPCError:
        await target.reply_text(
            "🏏 **Cricket Owner Panel**\n\nManage your tournament.",
            reply_markup=cricket_owner_panel_kb(),
        )


async def _render_tours(client: Client, target: Message, bot_id: int) -> None:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(CricketTour)
            .where(CricketTour.bot_id == bot_id)
            .order_by(CricketTour.active.desc(), CricketTour.created_at.desc())
            .limit(20)
        )
        tours = list(r.scalars().all())

    lines = ["🏆 **Tournaments**\n"]
    rows = []
    if not tours:
        lines.append("No tournaments yet.")
    for t in tours:
        status = "🟢 Active" if t.active else "🔴 Ended"
        lines.append(f"{status} — **{t.name}**")
        if t.prize_pool:
            lines.append(f"  💰 Prize: {t.prize_pool}")
        if t.active:
            rows.append([
                btn(YELLOW, f"🔚 End: {t.name[:20]}", f"own:crik:endtour:{t.id}", icon=EMOJI_OCTAGON),
            ])

    rows.append([btn(GREEN, "🏆 Start New Tour", "own:crik:newtour", icon=EMOJI_FIRE)])
    rows.append([btn(YELLOW, "🔙 Back", "own:crik:home", icon=EMOJI_OCTAGON)])

    try:
        await target.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _render_players(
    target: Message, bot_id: int, page: int,
    is_captain: bool = False, status_filter: str | None = None,
) -> None:
    cond = [CricketPlayer.bot_id == bot_id]
    cond.append(CricketPlayer.is_captain.is_(is_captain))
    if status_filter:
        cond.append(CricketPlayer.status == status_filter)
    else:
        cond.append(CricketPlayer.status != "deregistered")

    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count()).select_from(CricketPlayer).where(*cond)
        )
        r = await session.execute(
            select(CricketPlayer).where(*cond)
            .order_by(CricketPlayer.registered_at.desc())
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        players = list(r.scalars().all())

    cap_label = "👑 Captains" if is_captain else "👥 Players"
    lines = [f"{cap_label} — {total} total\n"]
    rows = []

    for p in players:
        st = STATUS_EMOJI.get(p.status, "?")
        rl = ROLE_LABELS.get(p.role or "", p.role or "—")
        handle = f"@{p.username}" if p.username else f"id:{p.user_id}"
        lines.append(f"{st} **{p.full_name or handle}** — {rl} — {p.base_price or '—'}")
        if p.status == "approved":
            rows.append([btn(RED, f"🗑 Deregister: {(p.full_name or handle)[:20]}", f"own:crik:dereg:{p.id}", icon=EMOJI_TRASH)])

    action_prefix = "own:crik:captains" if is_captain else "own:crik:players"
    rows.extend(_paginate_kb(action_prefix, page, total, "own:crik:home"))

    text = "\n".join(lines) if lines else f"No {cap_label.lower()} yet."
    try:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _render_pending(target: Message, bot_id: int, page: int) -> None:
    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status == "pending")
        )
        r = await session.execute(
            select(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status == "pending")
            .order_by(CricketPlayer.registered_at.asc())
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        players = list(r.scalars().all())

    lines = [f"⏳ **Pending Approvals** — {total} total\n"]
    rows = []

    for p in players:
        rl = ROLE_LABELS.get(p.role or "", p.role or "—")
        handle = f"@{p.username}" if p.username else f"id:{p.user_id}"
        cap_tag = " 👑" if p.is_captain else ""
        lines.append(f"• **{p.full_name or handle}**{cap_tag} — {rl} — {p.base_price or '—'}")
        rows.append([
            btn(GREEN,  "✅",           f"crik:apr:{p.id}", icon=EMOJI_CHECK),
            btn(DANGER, "❌",           f"crik:rej:{p.id}", icon=EMOJI_X),
            btn(YELLOW, "⏸️ Waitlist",  f"crik:wl:{p.id}",  icon=EMOJI_BELL),
        ])

    rows.extend(_paginate_kb("own:crik:pending", page, total, "own:crik:home"))

    text = "\n".join(lines) if players else "⏳ No pending registrations."
    try:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _render_questions(target: Message, bot_id: int) -> None:
    questions = await _get_all_questions(bot_id)
    lines = ["🎯 **Registration Questions**\n",
             "These appear after Role and Base Price in the wizard.\n"]
    rows = []

    if not questions:
        lines.append("No extra questions configured.")
    for q in questions:
        st = "✅" if q.enabled else "❌"
        req = " _(required)_" if q.required else ""
        lines.append(f"{st} **{q.label}** `[{q.input_type}]`{req}")
        rows.append([
            btn(GREEN if q.enabled else RED,
                f"{'✅ ON' if q.enabled else '❌ OFF'}: {q.label[:22]}",
                f"own:crik:qtoggle:{q.id}"),
            btn(RED, "🗑", f"own:crik:qdelete:{q.id}", icon=EMOJI_TRASH),
        ])

    rows.append([btn(PRIMARY, "➕ Add Question", "own:crik:qadd", icon=EMOJI_SPARKLE)])
    rows.append([btn(YELLOW, "🔙 Back", "own:crik:home", icon=EMOJI_OCTAGON)])

    try:
        await target.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _render_settings(target: Message, bot_id: int, edit: bool = False) -> None:
    async with AsyncSessionLocal() as session:
        s = await _get_or_create_settings(bot_id, session)
        auto_app = s.auto_approve
        allow_cap = s.allow_captain_reg
        max_p = s.max_players
        max_c = s.max_captains
        reg_end = s.reg_end_date
        await session.commit()

    def dot(v: bool) -> str:
        return BLUE if v else RED

    end_label = _fmt_ist(reg_end) if reg_end else "Not set"
    max_p_label = str(max_p) if max_p > 0 else "Unlimited"
    max_c_label = str(max_c) if max_c > 0 else "Unlimited"

    rows = [
        [btn(dot(auto_app),   f"🤖 Auto-Approve: {'✅ ON' if auto_app else '❌ OFF'}",         "own:crik:stoggle:auto_approve")],
        [btn(dot(allow_cap),  f"👑 Captain Reg: {'✅ ON' if allow_cap else '❌ OFF'}",          "own:crik:stoggle:allow_captain_reg")],
        [btn(YELLOW,          f"📅 Reg End Date: {end_label}",                                  "own:crik:setdate")],
        [btn(BLUE,            f"👥 Max Players: {max_p_label}",                                 "own:crik:setmax:players")],
        [btn(BLUE,            f"👑 Max Captains: {max_c_label}",                                "own:crik:setmax:captains")],
        [btn(YELLOW,          "🔙 Back",                                                        "own:crik:home", icon=EMOJI_OCTAGON)],
    ]

    text = "⚙️ **Cricket Settings**\n\nTap any option to change it."
    try:
        if edit:
            await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
        else:
            await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def _render_stats(target: Message, bot_id: int) -> None:
    async with AsyncSessionLocal() as session:
        total_p = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status != "deregistered",
                   CricketPlayer.is_captain.is_(False))
        )
        total_c = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status != "deregistered",
                   CricketPlayer.is_captain.is_(True))
        )
        pending = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status == "pending")
        )
        approved = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status == "approved")
        )
        rejected = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status == "rejected")
        )
        waitlisted = await session.scalar(
            select(func.count()).select_from(CricketPlayer)
            .where(CricketPlayer.bot_id == bot_id, CricketPlayer.status == "waitlisted")
        )
        tour_q = await session.execute(
            select(CricketTour)
            .where(CricketTour.bot_id == bot_id, CricketTour.active.is_(True))
            .limit(1)
        )
        active_tour = tour_q.scalar_one_or_none()

    text = (
        "📊 **Cricket Statistics**\n\n"
        + (f"🏆 Active Tour: **{active_tour.name}**\n\n" if active_tour else "")
        + f"🏏 Players: **{total_p}**\n"
        f"👑 Captains: **{total_c}**\n\n"
        f"✅ Approved: **{approved}**\n"
        f"⏳ Pending: **{pending}**\n"
        f"⏸️ Waitlisted: **{waitlisted}**\n"
        f"❌ Rejected: **{rejected}**"
    )
    try:
        await target.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:home", icon=EMOJI_OCTAGON)]]),
        )
    except RPCError:
        await target.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:home", icon=EMOJI_OCTAGON)]]),
        )


async def _render_logs(target: Message, bot_id: int, page: int) -> None:
    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count()).select_from(OwnerLog).where(OwnerLog.bot_id == bot_id)
        )
        r = await session.execute(
            select(OwnerLog)
            .where(OwnerLog.bot_id == bot_id)
            .order_by(OwnerLog.time.desc())
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        logs = list(r.scalars().all())

    lines = [f"🚨 **Activity Logs** — {total} total\n"]
    for entry in logs:
        ist_time = _fmt_ist(entry.time)
        lines.append(f"• {ist_time}\n  {entry.action[:80]}")

    rows = list(_paginate_kb("own:crik:logs", page, total, "own:crik:home"))
    text = "\n".join(lines) if logs else "🚨 No activity logs yet."
    try:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ── IST Date Picker ───────────────────────────────────────────────────────────

def _date_picker_kb(epoch: float) -> InlineKeyboardMarkup:
    dt_ist = _epoch_to_utc(epoch) + IST
    date_str = dt_ist.strftime("%d %b %Y")
    time_str = dt_ist.strftime("%I:%M %p")
    return InlineKeyboardMarkup([
        [
            btn(PRIMARY, "◀ -1d",  "crikd:-d"),
            btn(DEFAULT, f"📅 {date_str}", "crikd:noop"),
            btn(PRIMARY, "+1d ▶",  "crikd:+d"),
        ],
        [
            btn(PRIMARY, "◀ -1h",  "crikd:-h"),
            btn(DEFAULT, f"🕐 {time_str} IST", "crikd:noop"),
            btn(PRIMARY, "+1h ▶",  "crikd:+h"),
        ],
        [
            btn(PRIMARY, "◀ -30m", "crikd:-m"),
            btn(DEFAULT, "Adjust minutes", "crikd:noop"),
            btn(PRIMARY, "+30m ▶", "crikd:+m"),
        ],
        [
            btn(GREEN,  "✅ Confirm", "crikd:ok",  icon=EMOJI_CHECK),
            btn(DANGER, "❌ Cancel",  "crikd:cl",  icon=EMOJI_X),
        ],
    ])


async def _render_date_picker(client: Client, user_id: int, target: Message, bot_id: int) -> None:
    now_utc_epoch = datetime.now(timezone.utc).timestamp()
    # Default to 7 days from now at midnight IST
    now_ist = _now_ist()
    default_ist = now_ist.replace(hour=23, minute=59, second=0, microsecond=0) + timedelta(days=7)
    default_utc = default_ist - IST
    epoch = default_utc.timestamp()

    clone_pending[(bot_id, user_id)] = PendingAction("crik_setdate", {"epoch": epoch})
    await target.reply_text(
        "📅 **Set Registration End Date (IST)**\n\n"
        "Use the buttons to pick the date and time.\n"
        "Tap **Confirm** when done.",
        reply_markup=_date_picker_kb(epoch),
    )


# ── New tour wizard ───────────────────────────────────────────────────────────

async def _start_tour_wizard(client: Client, user_id: int, target: Message, bot_id: int) -> None:
    clone_pending[(bot_id, user_id)] = PendingAction("crik_newtour", {"step": "name"})
    await target.reply_text(
        "🏆 **New Tournament**\n\nStep 1 — Send the **tournament name:**",
        reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:tours", icon=EMOJI_X)]]),
    )


async def handle_cricket_owner_message(
    client: Client, message: Message, bot_id: int, user_id: int, pending: PendingAction
) -> None:
    """Handle owner text input during cricket-owner wizard states."""
    text = (message.text or "").strip()
    action = pending.action

    if action == "crik_newtour":
        step = pending.data.get("step")
        if step == "name":
            pending.data["name"] = text[:255]
            pending.data["step"] = "details"
            clone_pending[(bot_id, user_id)] = pending
            await message.reply_text(
                f"✅ Name: **{text[:50]}**\n\n"
                "Step 2 — Send a **description** (optional — send /skip to skip):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:tours", icon=EMOJI_X)]]),
            )
        elif step == "details":
            details = None if text.lower() == "/skip" else text[:500]
            pending.data["details"] = details
            pending.data["step"] = "prize"
            clone_pending[(bot_id, user_id)] = pending
            await message.reply_text(
                "Step 3 — Send the **prize pool** (optional — send /skip):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:tours", icon=EMOJI_X)]]),
            )
        elif step == "prize":
            prize = None if text.lower() == "/skip" else text[:128]
            name = pending.data["name"]
            details = pending.data.get("details")
            clone_pending.pop((bot_id, user_id), None)
            async with AsyncSessionLocal() as session:
                # Deactivate existing active tours
                r = await session.execute(
                    select(CricketTour)
                    .where(CricketTour.bot_id == bot_id, CricketTour.active.is_(True))
                )
                for old in r.scalars().all():
                    old.active = False
                tour = CricketTour(bot_id=bot_id, name=name, details=details, prize_pool=prize, active=True)
                session.add(tour)
                await session.commit()
            await message.reply_text(
                f"🏆 **Tournament Started!**\n\n"
                f"Name: **{name}**\n"
                + (f"Details: {details}\n" if details else "")
                + (f"Prize: {prize}" if prize else ""),
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back to Tours", "own:crik:tours", icon=EMOJI_OCTAGON)]]),
            )
            await _record_action(bot_id, f"Tour started: {name}")
            await _log_cricket(client, bot_id, f"🏆 **New Tour Started:** {name}\nPrize: {prize or '—'}")

    elif action == "crik_qadd":
        step = pending.data.get("step")
        if step == "label":
            pending.data["label"] = text[:256]
            pending.data["step"] = "type"
            clone_pending[(bot_id, user_id)] = pending
            await message.reply_text(
                f"✅ Label: **{text[:60]}**\n\n"
                "Choose input type:",
                reply_markup=InlineKeyboardMarkup([
                    [btn(PRIMARY, "📝 Text",   "own:crik:qtype:text")],
                    [btn(PRIMARY, "🔢 Number", "own:crik:qtype:number")],
                    [btn(PRIMARY, "📋 Choice", "own:crik:qtype:choice")],
                    [btn(DANGER, "❌ Cancel",  "own:crik:questions", icon=EMOJI_X)],
                ]),
            )
        elif step == "choices":
            choices = [c.strip() for c in text.split(",") if c.strip()]
            if len(choices) < 2:
                await message.reply_text(
                    f"{TXT_ERR} Please provide at least 2 options, separated by commas.\n"
                    "Example: `Left-handed, Right-handed`"
                )
                return
            label = pending.data["label"]
            clone_pending.pop((bot_id, user_id), None)
            async with AsyncSessionLocal() as session:
                max_idx = await session.scalar(
                    select(func.max(CricketQuestion.order_index))
                    .where(CricketQuestion.bot_id == bot_id)
                ) or 0
                session.add(CricketQuestion(
                    bot_id=bot_id, key=label.lower().replace(" ", "_")[:64],
                    label=label, input_type="choice",
                    choices=json.dumps(choices),
                    enabled=True, required=False,
                    order_index=max_idx + 1,
                ))
                await session.commit()
            await message.reply_text(
                f"✅ Question **{label}** added with {len(choices)} choices.",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:questions", icon=EMOJI_OCTAGON)]]),
            )

    elif action == "crik_setmax":
        field = pending.data.get("field")
        clone_pending.pop((bot_id, user_id), None)
        if not text.isdigit():
            await message.reply_text(f"{TXT_ERR} Please send a number (0 = unlimited).")
            return
        value = int(text)
        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            if field == "players":
                s.max_players = value
            else:
                s.max_captains = value
            await session.commit()
        label = "players" if field == "players" else "captains"
        cap_str = str(value) if value > 0 else "Unlimited"
        await message.reply_text(
            f"✅ Max {label} set to **{cap_str}**.",
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
        )


# ── Owner panel dispatcher ────────────────────────────────────────────────────

async def dispatch_cricket_owner_action(
    client: Client, user_id: int, target: Message, action: str, bot_id: int
) -> None:
    """Routes own:crik:* actions from the owner callback handler."""

    if action == "own:crik:home":
        await _render_home(target)

    elif action == "own:crik:tours":
        await _render_tours(client, target, bot_id)

    elif action == "own:crik:newtour":
        await _start_tour_wizard(client, user_id, target, bot_id)

    elif action.startswith("own:crik:endtour:"):
        tour_id = int(action.split(":")[3])
        async with AsyncSessionLocal() as session:
            tour = await session.get(CricketTour, tour_id)
            if tour and tour.bot_id == bot_id:
                tour.active = False
                name = tour.name
                await session.commit()
        await _record_action(bot_id, f"Tour ended: {name}")
        await _log_cricket(client, bot_id, f"🔚 **Tour Ended:** {name}")
        await _render_tours(client, target, bot_id)

    elif action == "own:crik:players" or action.startswith("own:crik:players:"):
        page = int(action.split(":")[-1]) if action != "own:crik:players" else 0
        await _render_players(target, bot_id, page, is_captain=False)

    elif action == "own:crik:captains" or action.startswith("own:crik:captains:"):
        page = int(action.split(":")[-1]) if action != "own:crik:captains" else 0
        await _render_players(target, bot_id, page, is_captain=True)

    elif action == "own:crik:pending" or action.startswith("own:crik:pending:"):
        page = int(action.split(":")[-1]) if action != "own:crik:pending" else 0
        await _render_pending(target, bot_id, page)

    elif action.startswith("own:crik:dereg:"):
        player_id = int(action.split(":")[3])
        async with AsyncSessionLocal() as session:
            p = await session.get(CricketPlayer, player_id)
            if p and p.bot_id == bot_id:
                p.status = "deregistered"
                p_name = p.full_name or f"id:{p.user_id}"
                await session.commit()
        await _record_action(bot_id, f"Player deregistered: {p_name}")
        await _render_players(target, bot_id, 0, is_captain=False)

    elif action == "own:crik:questions":
        await _render_questions(target, bot_id)

    elif action.startswith("own:crik:qtoggle:"):
        q_id = int(action.split(":")[3])
        async with AsyncSessionLocal() as session:
            q = await session.get(CricketQuestion, q_id)
            if q and q.bot_id == bot_id:
                q.enabled = not q.enabled
                await session.commit()
        await _render_questions(target, bot_id)

    elif action.startswith("own:crik:qdelete:"):
        q_id = int(action.split(":")[3])
        async with AsyncSessionLocal() as session:
            q = await session.get(CricketQuestion, q_id)
            if q and q.bot_id == bot_id:
                await session.delete(q)
                await session.commit()
        await _render_questions(target, bot_id)

    elif action == "own:crik:qadd":
        clone_pending[(bot_id, user_id)] = PendingAction("crik_qadd", {"step": "label"})
        try:
            await target.edit_text(
                f"{TXT_INFO} Send the **question label** (e.g. \"Batting Hand\"):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:questions", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Send the **question label** (e.g. \"Batting Hand\"):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:questions", icon=EMOJI_X)]]),
            )

    elif action.startswith("own:crik:qtype:"):
        q_type = action.split(":")[3]
        pending = clone_pending.get((bot_id, user_id))
        if not pending or pending.action != "crik_qadd":
            return
        if q_type == "choice":
            pending.data["step"] = "choices"
            pending.data["type"] = "choice"
            clone_pending[(bot_id, user_id)] = pending
            try:
                await target.edit_text(
                    f"{TXT_INFO} Send the **choices** separated by commas:\n"
                    "Example: `Left-handed, Right-handed, Ambidextrous`",
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:questions", icon=EMOJI_X)]]),
                )
            except RPCError:
                await target.reply_text(
                    f"{TXT_INFO} Send choices separated by commas:",
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:questions", icon=EMOJI_X)]]),
                )
        else:
            label = pending.data["label"]
            clone_pending.pop((bot_id, user_id), None)
            async with AsyncSessionLocal() as session:
                max_idx = await session.scalar(
                    select(func.max(CricketQuestion.order_index))
                    .where(CricketQuestion.bot_id == bot_id)
                ) or 0
                session.add(CricketQuestion(
                    bot_id=bot_id, key=label.lower().replace(" ", "_")[:64],
                    label=label, input_type=q_type,
                    enabled=True, required=False, order_index=max_idx + 1,
                ))
                await session.commit()
            try:
                await target.edit_text(
                    f"✅ Question **{label}** ({q_type}) added.",
                    reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:questions", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                await target.reply_text(f"✅ Question **{label}** ({q_type}) added.")

    elif action == "own:crik:settings":
        await _render_settings(target, bot_id, edit=True)

    elif action.startswith("own:crik:stoggle:"):
        field = action.split(":")[3]
        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            if hasattr(s, field):
                setattr(s, field, not getattr(s, field))
            await session.commit()
        await _record_action(bot_id, f"Cricket setting toggled: {field}")
        await _render_settings(target, bot_id, edit=True)

    elif action == "own:crik:setdate":
        await _render_date_picker(client, user_id, target, bot_id)

    elif action.startswith("own:crik:setmax:"):
        field = action.split(":")[3]  # players or captains
        clone_pending[(bot_id, user_id)] = PendingAction("crik_setmax", {"field": field})
        label = "players" if field == "players" else "captains"
        try:
            await target.edit_text(
                f"{TXT_INFO} Send the **max number of {label}** (send `0` for unlimited):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "❌ Cancel", "own:crik:settings", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Send max number of {label} (0 = unlimited):",
            )

    elif action == "own:crik:stats":
        await _render_stats(target, bot_id)

    elif action == "own:crik:logs" or action.startswith("own:crik:logs:"):
        page = int(action.split(":")[-1]) if action != "own:crik:logs" else 0
        await _render_logs(target, bot_id, page)


# ── Date picker callback handler ──────────────────────────────────────────────

async def handle_date_picker_callback(
    client: Client, cq: CallbackQuery, bot_id: int, user_id: int
) -> None:
    """Handles crikd:* date-picker button presses."""
    data = cq.data
    pending = clone_pending.get((bot_id, user_id))
    if not pending or pending.action != "crik_setdate":
        await cq.answer("Session expired.", show_alert=True)
        return

    epoch = pending.data.get("epoch", datetime.now(timezone.utc).timestamp())

    if data == "crikd:noop":
        await cq.answer()
        return
    elif data == "crikd:+d":
        epoch += 86400
    elif data == "crikd:-d":
        epoch -= 86400
    elif data == "crikd:+h":
        epoch += 3600
    elif data == "crikd:-h":
        epoch -= 3600
    elif data == "crikd:+m":
        epoch += 1800
    elif data == "crikd:-m":
        epoch -= 1800
    elif data == "crikd:cl":
        clone_pending.pop((bot_id, user_id), None)
        await cq.answer("Cancelled.")
        try:
            await cq.message.edit_text(
                "❌ Date picker cancelled.",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
            )
        except RPCError:
            pass
        return
    elif data == "crikd:ok":
        clone_pending.pop((bot_id, user_id), None)
        dt_utc = _epoch_to_utc(epoch)
        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            s.reg_end_date = dt_utc
            await session.commit()
        await _record_action(bot_id, f"Registration end date set: {_fmt_ist(dt_utc)}")
        await cq.answer("✅ Date saved!")
        try:
            await cq.message.edit_text(
                f"✅ **Registration end date set!**\n\n📅 {_fmt_ist(dt_utc)}",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
            )
        except RPCError:
            pass
        return

    pending.data["epoch"] = epoch
    clone_pending[(bot_id, user_id)] = pending
    await cq.answer()
    try:
        await cq.message.edit_reply_markup(_date_picker_kb(epoch))
    except RPCError:
        pass


# ── Handler registration ──────────────────────────────────────────────────────

def register_cricket_handlers(app: Client, bot_id: int) -> None:
    """Register all cricket-specific callback and message handlers on `app`."""

    @app.on_callback_query(filters.regex(r"^crik:"))
    async def cricket_callback(client: Client, cq: CallbackQuery) -> None:
        data = cq.data
        user_id = cq.from_user.id

        # ── My status ─────────────────────────────────────────────────────────
        if data == "crik:mystatus":
            await _handle_my_status(client, cq, bot_id)

        # ── Start registration as player ──────────────────────────────────────
        elif data == "crik:ryes":
            await _start_wizard(client, user_id, cq.message, bot_id, is_captain=False)

        # ── Start registration as captain ─────────────────────────────────────
        elif data == "crik:cap":
            await _start_wizard(client, user_id, cq.message, bot_id, is_captain=True)

        # ── Cancel wizard ─────────────────────────────────────────────────────
        elif data == "crik:cancel":
            clone_pending.pop((bot_id, user_id), None)
            await cq.message.edit_text("❌ Registration cancelled.")

        # ── Back to start ─────────────────────────────────────────────────────
        elif data == "crik:back":
            await cq.message.delete()

        # ── Role selection ────────────────────────────────────────────────────
        elif data.startswith("crik:role:"):
            role_key = data.split(":")[2]
            pending = clone_pending.get((bot_id, user_id))
            if not pending or pending.action not in ("crik_wizard",):
                await cq.answer("Session expired. Start again.", show_alert=True)
                return
            await _wizard_role_selected(client, cq, bot_id, user_id, role_key, pending)

        # ── Choice question answer ────────────────────────────────────────────
        elif data.startswith("crik:ch:"):
            parts = data.split(":")
            q_index = int(parts[2])
            choice_index = int(parts[3])
            pending = clone_pending.get((bot_id, user_id))
            if not pending:
                await cq.answer("Session expired.", show_alert=True)
                return
            q_ids = pending.data.get("questions", [])
            if q_index < len(q_ids):
                async with AsyncSessionLocal() as session:
                    q = await session.get(CricketQuestion, q_ids[q_index])
                if q and q.choices:
                    choices = json.loads(q.choices)
                    if choice_index < len(choices):
                        pending.data["answers"][q.key] = choices[choice_index]
                        pending.data["q_index"] = q_index + 1
                        clone_pending[(bot_id, user_id)] = pending
                        await _advance_to_next_question(client, cq.message, bot_id, user_id, pending)

        # ── Skip optional question ────────────────────────────────────────────
        elif data.startswith("crik:skip:"):
            q_index = int(data.split(":")[2])
            pending = clone_pending.get((bot_id, user_id))
            if not pending:
                await cq.answer("Session expired.", show_alert=True)
                return
            pending.data["q_index"] = q_index + 1
            clone_pending[(bot_id, user_id)] = pending
            await _advance_to_next_question(client, cq.message, bot_id, user_id, pending)

        # ── Submit registration ───────────────────────────────────────────────
        elif data == "crik:submit":
            pending = clone_pending.get((bot_id, user_id))
            if not pending or pending.action != "crik_confirm":
                await cq.answer("Session expired.", show_alert=True)
                return
            await _submit_registration(client, cq, bot_id, user_id, pending)

        # ── Redo registration wizard ──────────────────────────────────────────
        elif data == "crik:redo":
            pending = clone_pending.pop((bot_id, user_id), None)
            is_cap = pending.data.get("is_captain", False) if pending else False
            await _start_wizard(client, user_id, cq.message, bot_id, is_captain=is_cap)

        # ── Admin approval buttons ────────────────────────────────────────────
        elif data.startswith("crik:apr:") or data.startswith("crik:rej:") or data.startswith("crik:wl:"):
            parts = data.split(":")
            action = parts[1]   # apr / rej / wl
            player_id = int(parts[2])
            await _handle_approval(client, cq, action, player_id, bot_id)

        await cq.answer()

    @app.on_callback_query(filters.regex(r"^crikd:"))
    async def cricket_datepicker_callback(client: Client, cq: CallbackQuery) -> None:
        await handle_date_picker_callback(client, cq, bot_id, cq.from_user.id)
