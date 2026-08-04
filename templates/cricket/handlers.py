"""Cricket Tournament template — full handler implementation.

Exports
-------
register_cricket_handlers(app, bot_id)
handle_cricket_start(client, message, bot_id, bot_row, is_owner, first_time)
dispatch_cricket_owner_action(client, user_id, target, action, bot_id)
handle_cricket_wizard_message(client, message, bot_id, user_id, pending)
handle_cricket_owner_message(client, message, bot_id, user_id, pending)
seed_default_questions(bot_id)
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
    BotChannel,
    CricketPlayer,
    CricketQuestion,
    CricketSettings,
    CricketTour,
    OwnerLog,
    Owner,
)
from keyboards import (
    BLUE, DANGER, DEFAULT, GREEN, PRIMARY, RED, SUCCESS, YELLOW,
    EMOJI_BELL, EMOJI_CHART, EMOJI_CHECK, EMOJI_CROWN, EMOJI_DEVIL,
    EMOJI_FIRE, EMOJI_GUARD, EMOJI_MIC, EMOJI_OCTAGON, EMOJI_SIREN,
    EMOJI_SPARKLE, EMOJI_STAR, EMOJI_TOOLS, EMOJI_TRASH, EMOJI_TROPHY,
    EMOJI_X, cricket_owner_panel_kb,
    TXT_ERR, TXT_INFO, TXT_WARN, TXT_OK,
    btn, quote, yes_no_kb,
)
from utils.fsub import missing_channels
from utils.state import PendingAction, clone_pending

log = logging.getLogger("nexora.cricket")

# ── Constants ─────────────────────────────────────────────────────────────────
IST = timedelta(hours=5, minutes=30)
PAGE_SIZE = 8

# Exactly 3 roles — no more
ROLES = [
    ("bat",  "Batter"),
    ("bowl", "Bowler"),
    ("ar",   "All-rounder"),
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
    {"key": "batting_hand",  "label": "Batting Hand",        "input_type": "choice",
     "choices": ["Left-handed", "Right-handed"],             "enabled": True,  "required": False, "order_index": 0},
    {"key": "bowling_type",  "label": "Bowling Type",        "input_type": "choice",
     "choices": ["Fast", "Medium", "Spin", "N/A"],           "enabled": True,  "required": False, "order_index": 1},
    {"key": "experience",    "label": "Years of Experience", "input_type": "number",
     "choices": [],                                           "enabled": False, "required": False, "order_index": 2},
    {"key": "city",          "label": "City / Location",     "input_type": "text",
     "choices": [],                                           "enabled": False, "required": False, "order_index": 3},
]

DEFAULT_WELCOME_IMAGE = "https://graph.org/file/874c7523cf9fb087baae4-787a191131ca5d0bb7.jpg"

# Default base-price options (in CREDITS, not currency) shown to players during
# registration. The owner can override this list from the settings panel.
DEFAULT_BASE_PRICE_OPTIONS = [10, 50, 100]


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


async def _get_enabled_questions(bot_id: int, is_captain: bool = False) -> list[CricketQuestion]:
    """Return enabled questions. captain_only questions are excluded for regular players."""
    async with AsyncSessionLocal() as session:
        cond = [CricketQuestion.bot_id == bot_id, CricketQuestion.enabled.is_(True)]
        if not is_captain:
            cond.append(CricketQuestion.captain_only.is_(False))
        r = await session.execute(
            select(CricketQuestion).where(*cond).order_by(CricketQuestion.order_index)
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


async def _notify_admin(
    client: Client, bot_id: int, owner_tg_id: int | None,
    text: str, markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a notification to both owner DM and admin GC (if set)."""
    async with AsyncSessionLocal() as session:
        s = await _get_or_create_settings(bot_id, session)
        admin_gc = s.admin_gc
        await session.commit()

    # Owner DM
    if owner_tg_id:
        try:
            await client.send_message(owner_tg_id, text, reply_markup=markup)
        except RPCError:
            pass

    # Admin GC (independent — don't forward approve/reject buttons there)
    if admin_gc and admin_gc != owner_tg_id:
        try:
            await client.send_message(admin_gc, text)
        except RPCError:
            pass


async def _record_action(bot_id: int, action: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(OwnerLog(bot_id=bot_id, action=action))
        await session.commit()


def _parse_base_price_options(raw: str | None) -> list[int]:
    """Parse the owner-configured credit options JSON, falling back to defaults."""
    if not raw:
        return DEFAULT_BASE_PRICE_OPTIONS.copy()
    try:
        values = json.loads(raw)
        cleaned = sorted({int(v) for v in values if int(v) > 0})
        return cleaned if cleaned else DEFAULT_BASE_PRICE_OPTIONS.copy()
    except (json.JSONDecodeError, TypeError, ValueError):
        return DEFAULT_BASE_PRICE_OPTIONS.copy()


async def _get_base_price_options(bot_id: int) -> list[int]:
    async with AsyncSessionLocal() as session:
        s = await _get_or_create_settings(bot_id, session)
        options = _parse_base_price_options(s.base_price_options)
        await session.commit()
    return options


async def _player_of_user(bot_id: int, user_id: int) -> CricketPlayer | None:
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
    user_id = message.from_user.id

    # ── FSub check for registration ───────────────────────────────────────────
    if not is_owner:
        async with AsyncSessionLocal() as session:
            ch_result = await session.execute(
                select(BotChannel).where(BotChannel.bot_id == bot_id)
            )
            channels = list(ch_result.scalars().all())
        if channels:
            missing = await missing_channels(client, channels, user_id)
            if missing:
                rows = []
                for ch in missing:
                    label = ch.title or ch.username or "Channel"
                    link  = f"https://t.me/{ch.username}" if ch.username else None
                    rows.append([
                        btn(BLUE, f"Join {label}", url=link, icon=EMOJI_DEVIL)
                        if link else btn(BLUE, label, "crik:noop_fsub", icon=EMOJI_DEVIL)
                    ])
                rows.append([btn(GREEN, "✅ Verify Membership", "crik:fsub_verify")])
                await message.reply_text(
                    "😈 **Join the required channels first**\n\n"
                    "You must be a member to register for this tournament.\n"
                    "Join below, then press **Verify Membership**.",
                    reply_markup=InlineKeyboardMarkup(rows),
                    reply_parameters=quote(message.id),
                )
                return

    async with AsyncSessionLocal() as session:
        settings = await _get_or_create_settings(bot_id, session)
        auto_app   = settings.auto_approve
        allow_cap  = settings.allow_captain_reg
        reg_end    = settings.reg_end_date
        img_off    = settings.welcome_image_disabled
        await session.commit()

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
    existing = await _player_of_user(bot_id, user_id) if not is_owner else None

    greeting = (
        bot_row.welcome_caption
        or f"🏏 **Welcome to {bot_name}!**\n\nRegister for the cricket tournament and showcase your skills."
    )
    caption = f"{greeting}{tour_line}{deadline_text}"

    rows = []
    if is_owner:
        rows.append([btn(PRIMARY, "Owner Panel", "own:crik:home", icon=EMOJI_CROWN)])
    elif existing:
        st = STATUS_EMOJI.get(existing.status, "❓")
        role_label = ROLE_LABELS.get(existing.role or "", existing.role or "—")
        rows.append([btn(
            GREEN if existing.status == "approved" else YELLOW,
            f"{st} My Status — {role_label}",
            "crik:mystatus",
        )])
        if existing.status in ("rejected", "deregistered") and reg_open:
            rows.append([btn(PRIMARY, "Register Again", "crik:ryes", icon=EMOJI_SPARKLE)])
    elif reg_open:
        rows.append([btn(PRIMARY, "Register as Player", "crik:ryes", icon=EMOJI_SPARKLE)])
        if allow_cap:
            rows.append([btn(YELLOW, "Register as Captain", "crik:cap", icon=EMOJI_CROWN)])

    welcome_img = bot_row.welcome_image or DEFAULT_WELCOME_IMAGE
    if not img_off and welcome_img:
        try:
            await message.reply_photo(
                welcome_img,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(rows) if rows else None,
                reply_parameters=quote(message.id),
            )
            return
        except RPCError:
            pass
    await message.reply_text(
        caption,
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        reply_parameters=quote(message.id),
    )


# ── Registration wizard ───────────────────────────────────────────────────────

async def _start_wizard(
    client: Client, user_id: int, target: Message, bot_id: int, is_captain: bool,
) -> None:
    async with AsyncSessionLocal() as session:
        settings = await _get_or_create_settings(bot_id, session)
        reg_end = settings.reg_end_date
        max_p   = settings.max_players
        max_c   = settings.max_captains
        await session.commit()

    if reg_end and datetime.now(timezone.utc) > reg_end:
        await target.reply_text(f"🔒 Registration is **closed** — ended {_fmt_ist(reg_end)}.")
        return

    if is_captain and max_c > 0:
        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                select(func.count()).select_from(CricketPlayer)
                .where(CricketPlayer.bot_id == bot_id, CricketPlayer.is_captain.is_(True),
                       CricketPlayer.status.in_(["pending", "approved"]))
            )
        if count >= max_c:
            await target.reply_text(f"⛔ Captain slots are full ({max_c}/{max_c}). Contact the admin.")
            return
    elif not is_captain and max_p > 0:
        async with AsyncSessionLocal() as session:
            count = await session.scalar(
                select(func.count()).select_from(CricketPlayer)
                .where(CricketPlayer.bot_id == bot_id, CricketPlayer.is_captain.is_(False),
                       CricketPlayer.status.in_(["pending", "approved"]))
            )
        if count >= max_p:
            await target.reply_text(f"⛔ Player slots are full ({max_p}/{max_p}). Contact the admin.")
            return

    questions = await _get_enabled_questions(bot_id, is_captain=is_captain)
    clone_pending[(bot_id, user_id)] = PendingAction("crik_wizard", {
        "step": "role", "is_captain": is_captain,
        "answers": {}, "questions": [q.id for q in questions], "q_index": 0,
    })

    cap_label = "👑 **Captain**" if is_captain else "🏏 **Player**"
    await target.reply_text(
        f"✨ **Starting Registration — {cap_label}**\n\nStep 1 of 2+\n\n🎯 **Select your role:**",
        reply_markup=_role_kb(),
    )


def _role_kb() -> InlineKeyboardMarkup:
    """3-role keyboard — one button per row, clean labels, no double emoji."""
    rows = []
    for key, label in ROLES:
        rows.append([btn(PRIMARY, label, f"crik:role:{key}")])
    rows.append([btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)])
    return InlineKeyboardMarkup(rows)


def _base_price_kb(options: list[int]) -> InlineKeyboardMarkup:
    rows = [[btn(PRIMARY, f"{opt} Credits", f"crik:bp:{opt}")] for opt in options]
    rows.append([btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)])
    return InlineKeyboardMarkup(rows)


async def _wizard_role_selected(
    client: Client, cq: CallbackQuery, bot_id: int, user_id: int,
    role_key: str, pending: PendingAction,
) -> None:
    pending.data["answers"]["role"] = role_key
    pending.data["step"] = "base_price"
    role_label = ROLE_LABELS.get(role_key, role_key)
    clone_pending[(bot_id, user_id)] = pending

    options = await _get_base_price_options(bot_id)
    await cq.message.edit_text(
        f"✅ Role: **{role_label}**\n\nStep 2 of 2+\n\n"
        "💰 **Select your base price:**",
        reply_markup=_base_price_kb(options),
    )


async def _ask_team_name(target: Message, bot_id: int, user_id: int, pending: PendingAction) -> None:
    """Prompt captain for their team name."""
    pending.data["step"] = "team_name"
    clone_pending[(bot_id, user_id)] = pending
    await target.reply_text(
        "👑 **Captain Registration**\n\nStep — Team Details\n\n"
        "🏏 **What is your team name?**\nSend the name of the team you will be captaining.",
        reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)]]),
    )


async def _ask_team_logo(target: Message, bot_id: int, user_id: int, pending: PendingAction) -> None:
    """Prompt captain for their team logo URL (optional)."""
    pending.data["step"] = "team_logo"
    clone_pending[(bot_id, user_id)] = pending
    await target.reply_text(
        "🖼 **Team Logo** _(optional)_\n\n"
        "Send a direct image URL for your team logo (must start with `https://`),\n"
        "or tap **Skip** to continue without one.",
        reply_markup=InlineKeyboardMarkup([
            [btn(DEFAULT, "Skip", "crik:skip_team_logo")],
            [btn(DANGER,  "Cancel", "crik:cancel", icon=EMOJI_X)],
        ]),
    )


async def handle_cricket_wizard_message(
    client: Client, message: Message, bot_id: int, user_id: int, pending: PendingAction,
) -> None:
    step = pending.data.get("step")
    text = (message.text or "").strip()

    if step == "base_price":
        options = await _get_base_price_options(bot_id)
        await message.reply_text(
            f"{TXT_ERR} Please tap one of the **credit options** above instead of typing.",
            reply_markup=_base_price_kb(options),
        )
        return

    elif step == "team_name":
        if not text:
            await message.reply_text(
                f"{TXT_ERR} Please send your **team name**.",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)]]),
            )
            return
        pending.data["team_name"] = text[:128]
        clone_pending[(bot_id, user_id)] = pending
        await _ask_team_logo(message, bot_id, user_id, pending)
        return

    elif step == "team_logo":
        if text.startswith("http://") or text.startswith("https://"):
            pending.data["team_logo"] = text[:500]
        else:
            await message.reply_text(
                f"{TXT_ERR} Please send a valid image URL starting with `https://`, or tap **Skip**.",
                reply_markup=InlineKeyboardMarkup([
                    [btn(DEFAULT, "Skip", "crik:skip_team_logo")],
                    [btn(DANGER,  "Cancel", "crik:cancel", icon=EMOJI_X)],
                ]),
            )
            return
        clone_pending[(bot_id, user_id)] = pending
        await _advance_to_next_question(client, message, bot_id, user_id, pending)
        return

    elif step == "question":
        q_index = pending.data.get("q_index", 0)
        q_ids   = pending.data.get("questions", [])
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
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)]]),
                )
                return
            pending.data["answers"][q.key] = cleaned
        else:
            pending.data["answers"][q.key if q else f"q{q_index}"] = text[:200]

        pending.data["q_index"] = q_index + 1
        await _advance_to_next_question(client, message, bot_id, user_id, pending)

    clone_pending[(bot_id, user_id)] = pending


async def _advance_to_next_question(
    client: Client, target: Message, bot_id: int, user_id: int, pending: PendingAction,
) -> None:
    q_ids   = pending.data.get("questions", [])
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
    step_num    = 2 + q_index + 1

    if q.input_type == "choice" and q.choices:
        choices = json.loads(q.choices)
        rows = []
        for i in range(0, len(choices), 2):
            row = [btn(PRIMARY, choices[i], f"crik:ch:{q_index}:{i}")]
            if i + 1 < len(choices):
                row.append(btn(PRIMARY, choices[i + 1], f"crik:ch:{q_index}:{i+1}"))
            rows.append(row)
        if not q.required:
            rows.append([btn(DEFAULT, "Skip", f"crik:skip:{q_index}")])
        rows.append([btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)])
        await target.reply_text(
            f"Step {step_num} of {total_steps}\n\n❓ **{q.label}:**",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    else:
        type_hint = "number" if q.input_type == "number" else "text"
        opt_note  = " _(optional — send /skip to skip)_" if not q.required else ""
        skip_row  = [[btn(DEFAULT, "Skip", f"crik:skip:{q_index}")]] if not q.required else []
        cancel_row = [[btn(DANGER, "Cancel", "crik:cancel", icon=EMOJI_X)]]
        await target.reply_text(
            f"Step {step_num} of {total_steps}\n\n❓ **{q.label}:**{opt_note}\nSend a {type_hint}.",
            reply_markup=InlineKeyboardMarkup(skip_row + cancel_row),
        )


async def _finish_wizard(
    client: Client, target: Message, bot_id: int, user_id: int, pending: PendingAction,
) -> None:
    answers    = pending.data.get("answers", {})
    is_captain = pending.data.get("is_captain", False)

    role_key   = answers.get("role", "?")
    role_label = ROLE_LABELS.get(role_key, role_key)
    base_price = answers.get("base_price", "—")

    lines = [
        "📋 **Registration Summary**\n",
        f"🎭 Type: {'👑 Captain' if is_captain else '🏏 Player'}",
        f"🎯 Role: {role_label}",
        f"💰 Base Price: {base_price}",
    ]
    if is_captain:
        team_name = pending.data.get("team_name", "—")
        team_logo = pending.data.get("team_logo")
        lines.append(f"🏏 Team Name: {team_name}")
        if team_logo:
            lines.append(f"🖼 Team Logo: {team_logo}")
    for k, v in answers.items():
        if k in ("role", "base_price"):
            continue
        lines.append(f"• {k.replace('_', ' ').title()}: {v}")
    lines.append("\n✅ **Confirm and submit your registration?**")

    clone_pending[(bot_id, user_id)] = PendingAction("crik_confirm", {**pending.data, "step": "confirm"})

    await target.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[
            btn(GREEN,  "Submit", "crik:submit", icon=EMOJI_CHECK),
            btn(DANGER, "Redo",   "crik:redo",   icon=EMOJI_X),
        ]]),
    )


async def _submit_registration(
    client: Client, cq: CallbackQuery, bot_id: int, user_id: int, pending: PendingAction,
) -> None:
    answers    = pending.data.get("answers", {})
    is_captain = pending.data.get("is_captain", False)

    async with AsyncSessionLocal() as session:
        settings     = await _get_or_create_settings(bot_id, session)
        auto_approve = settings.auto_approve
        active_tour_q = await session.execute(
            select(CricketTour)
            .where(CricketTour.bot_id == bot_id, CricketTour.active.is_(True))
            .order_by(CricketTour.created_at.desc()).limit(1)
        )
        tour = active_tour_q.scalar_one_or_none()
        await session.commit()

    role_key   = answers.get("role")
    base_price = answers.get("base_price")
    extras     = {k: v for k, v in answers.items() if k not in ("role", "base_price")}

    # Captain-specific fields
    team_name = pending.data.get("team_name") if is_captain else None
    team_logo = pending.data.get("team_logo") if is_captain else None

    initial_status = "approved" if auto_approve else "pending"

    async with AsyncSessionLocal() as session:
        player = CricketPlayer(
            bot_id=bot_id, tour_id=tour.id if tour else None,
            user_id=user_id, username=cq.from_user.username,
            full_name=cq.from_user.first_name, role=role_key,
            is_captain=is_captain, base_price=base_price,
            team_name=team_name, team_logo=team_logo,
            status=initial_status, answers=json.dumps(extras) if extras else None,
        )
        session.add(player)
        await session.flush()
        player_id = player.id
        await session.commit()

    clone_pending.pop((bot_id, user_id), None)

    role_label = ROLE_LABELS.get(role_key or "", role_key or "—")
    cap_label  = "👑 Captain" if is_captain else "🏏 Player"
    team_line  = (f"\n🏏 Team: {team_name}" if team_name else "")

    if auto_approve:
        await cq.message.edit_text(
            f"✅ **Registration Approved!**\n\nWelcome aboard, {cq.from_user.first_name}! 🎉\n\n"
            f"🎭 Type: {cap_label}\n🎯 Role: {role_label}\n💰 Base Price: {base_price}"
            + team_line
            + ("\n" + f"🏆 Tour: {tour.name}" if tour else ""),
        )
    else:
        await cq.message.edit_text(
            f"⏳ **Registration Submitted!**\n\nHi {cq.from_user.first_name}, your registration is under review.\n"
            "You'll be notified once the admin approves it.\n\n"
            f"🎭 Type: {cap_label}\n🎯 Role: {role_label}\n💰 Base Price: {base_price}"
            + team_line,
        )

    handle       = f"@{cq.from_user.username}" if cq.from_user.username else f"id:{user_id}"
    owner_tg_id  = await _get_owner_tg_id(bot_id)

    notify_text = (
        f"🏏 **New {'Captain' if is_captain else 'Player'} Registration**\n\n"
        f"👤 Name: {cq.from_user.first_name} ({handle})\n"
        f"🎯 Role: {role_label}\n💰 Base Price: {base_price}\n"
        + (f"🏏 Team: {team_name}\n" if team_name else "")
        + (f"🖼 Logo: {team_logo}\n" if team_logo else "")
        + (f"🏆 Tour: {tour.name}\n" if tour else "")
        + f"📋 Status: {'Auto-approved ✅' if auto_approve else 'Pending approval ⏳'}"
    )

    if not auto_approve:
        markup = InlineKeyboardMarkup([
            [
                btn(GREEN,  "Approve",  f"crik:apr:{player_id}", icon=EMOJI_CHECK),
                btn(DANGER, "Reject",   f"crik:rej:{player_id}", icon=EMOJI_X),
            ],
            [btn(YELLOW, "Waitlist", f"crik:wl:{player_id}", icon=EMOJI_BELL)],
        ])
        await _notify_admin(client, bot_id, owner_tg_id, notify_text, markup)
    else:
        await _notify_admin(client, bot_id, owner_tg_id, notify_text)

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
        player.status = new_status
        await session.commit()
        p_name = player.full_name or f"id:{player.user_id}"
        p_uid  = player.user_id
        role_label = ROLE_LABELS.get(player.role or "", player.role or "—")
        is_cap = player.is_captain

    await cq.answer(f"{'✅' if new_status == 'approved' else '❌'} {new_status.title()}!")

    status_emoji = STATUS_EMOJI.get(new_status, "")
    cap_label    = "Captain" if is_cap else "Player"

    try:
        await cq.message.edit_text(
            cq.message.text + f"\n\n{status_emoji} **{new_status.upper()}** by admin",
        )
    except RPCError:
        pass

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
    st     = STATUS_EMOJI.get(player.status, "?")
    extras = json.loads(player.answers) if player.answers else {}
    lines  = [
        "📋 **Your Registration**\n",
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
        reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "crik:back", icon=EMOJI_OCTAGON)]]),
    )


# ── Paginated list helper ─────────────────────────────────────────────────────

def _paginate_kb(action_prefix: str, page: int, total: int, back_cb: str) -> list:
    rows = []
    nav  = []
    if page > 0:
        nav.append(btn(PRIMARY, "◀ Prev", f"{action_prefix}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(btn(PRIMARY, "Next ▶", f"{action_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn(YELLOW, "Back", back_cb, icon=EMOJI_OCTAGON)])
    return rows


# ── Cricket FSub channel management ──────────────────────────────────────────

async def _render_cricket_channels(target: Message, bot_id: int) -> None:
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(BotChannel).where(BotChannel.bot_id == bot_id)
        )
        channels = list(r.scalars().all())

    lines = ["😈 **Force-Subscribe Channels**\n",
             "Users must join these channels before they can register.\n"]
    rows  = []

    if not channels:
        lines.append("No channels configured yet.")
    for ch in channels:
        label = ch.title or ch.username or str(ch.chat_id)
        lines.append(f"• {label}")
        rows.append([btn(DANGER, f"Remove: {label[:30]}", f"own:crik:rmch:{ch.id}", icon=EMOJI_TRASH)])

    rows.append([btn(SUCCESS, "Add Channel",  "own:crik:addch",   icon=EMOJI_SPARKLE)])
    rows.append([btn(YELLOW,  "Back",         "own:crik:home",    icon=EMOJI_OCTAGON)])

    try:
        await target.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


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
    rows  = []
    if not tours:
        lines.append("No tournaments yet.")
    for t in tours:
        status = "🟢 Active" if t.active else "🔴 Ended"
        lines.append(f"{status} — **{t.name}**")
        if t.prize_pool:
            lines.append(f"  💰 Prize: {t.prize_pool}")
        if t.active:
            rows.append([btn(YELLOW, f"End: {t.name[:20]}", f"own:crik:endtour:{t.id}", icon=EMOJI_OCTAGON)])

    rows.append([btn(GREEN,  "Start New Tour", "own:crik:newtour", icon=EMOJI_FIRE)])
    rows.append([btn(YELLOW, "Back",           "own:crik:home",    icon=EMOJI_OCTAGON)])

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
        total = await session.scalar(select(func.count()).select_from(CricketPlayer).where(*cond))
        r = await session.execute(
            select(CricketPlayer).where(*cond)
            .order_by(CricketPlayer.registered_at.desc())
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        players = list(r.scalars().all())

    cap_label = "👑 Captains" if is_captain else "👥 Players"
    lines     = [f"{cap_label} — {total} total\n"]
    rows      = []

    for p in players:
        st     = STATUS_EMOJI.get(p.status, "?")
        rl     = ROLE_LABELS.get(p.role or "", p.role or "—")
        handle = f"@{p.username}" if p.username else f"id:{p.user_id}"
        lines.append(f"{st} **{p.full_name or handle}** — {rl} — {p.base_price or '—'}")
        if p.status == "approved":
            rows.append([btn(RED, f"Deregister: {(p.full_name or handle)[:20]}", f"own:crik:dereg:{p.id}", icon=EMOJI_TRASH)])

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
    rows  = []

    for p in players:
        rl     = ROLE_LABELS.get(p.role or "", p.role or "—")
        handle = f"@{p.username}" if p.username else f"id:{p.user_id}"
        cap_tag = " 👑" if p.is_captain else ""
        lines.append(f"• **{p.full_name or handle}**{cap_tag} — {rl} — {p.base_price or '—'}")
        rows.append([
            btn(GREEN,  "Approve",  f"crik:apr:{p.id}", icon=EMOJI_CHECK),
            btn(DANGER, "Reject",   f"crik:rej:{p.id}", icon=EMOJI_X),
            btn(YELLOW, "Waitlist", f"crik:wl:{p.id}",  icon=EMOJI_BELL),
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
             "These appear after Role and Base Price in the wizard.\n",
             "👑 = Captain-only question\n"]
    rows  = []

    if not questions:
        lines.append("No extra questions configured.")
    for q in questions:
        st   = "✅" if q.enabled else "❌"
        req  = " _(req)_" if q.required else ""
        cap  = " 👑" if q.captain_only else ""
        lines.append(f"{st} **{q.label}** `[{q.input_type}]`{req}{cap}")
        rows.append([
            btn(GREEN if q.enabled else RED,
                f"{'ON' if q.enabled else 'OFF'}: {q.label[:18]}",
                f"own:crik:qtoggle:{q.id}"),
            btn(YELLOW if q.captain_only else DEFAULT,
                "👑 Cap" if q.captain_only else "All",
                f"own:crik:qtoggle_cap:{q.id}"),
            btn(RED, "Del", f"own:crik:qdelete:{q.id}", icon=EMOJI_TRASH),
        ])

    rows.append([btn(PRIMARY, "Add Question", "own:crik:qadd",     icon=EMOJI_SPARKLE)])
    rows.append([btn(YELLOW,  "Back",         "own:crik:home",     icon=EMOJI_OCTAGON)])

    try:
        await target.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _render_settings(target: Message, bot_id: int, edit: bool = False) -> None:
    async with AsyncSessionLocal() as session:
        s = await _get_or_create_settings(bot_id, session)
        auto_app  = s.auto_approve
        allow_cap = s.allow_captain_reg
        max_p     = s.max_players
        max_c     = s.max_captains
        reg_end   = s.reg_end_date
        admin_gc  = s.admin_gc
        img_off   = s.welcome_image_disabled
        bp_opts   = _parse_base_price_options(s.base_price_options)
        await session.commit()

    async with AsyncSessionLocal() as session:
        bot_row = await session.get(BotModel, bot_id)
        custom_img = bot_row.welcome_image if bot_row else None

    def dot(v: bool) -> str:
        return BLUE if v else RED

    end_label   = _fmt_ist(reg_end) if reg_end else "Not set"
    max_p_label = str(max_p) if max_p > 0 else "Unlimited"
    max_c_label = str(max_c) if max_c > 0 else "Unlimited"
    gc_label    = f"GC: {admin_gc}" if admin_gc else "Not set"
    img_label   = ("Disabled" if img_off else ("Custom" if custom_img else "Default"))
    bp_label    = ", ".join(str(v) for v in bp_opts)

    rows = [
        [btn(dot(auto_app),  f"Auto-Approve: {'ON' if auto_app else 'OFF'}",       "own:crik:stoggle:auto_approve")],
        [btn(dot(allow_cap), f"Captain Reg: {'ON' if allow_cap else 'OFF'}",         "own:crik:stoggle:allow_captain_reg")],
        [btn(YELLOW,         f"Reg End Date: {end_label}",                           "own:crik:setdate")],
        [btn(BLUE,           f"Max Players: {max_p_label}",                          "own:crik:setmax:players")],
        [btn(BLUE,           f"Max Captains: {max_c_label}",                         "own:crik:setmax:captains")],
        [btn(BLUE,           f"Base Price Options: {bp_label} Credits",              "own:crik:setbaseprice")],
        [btn(BLUE,           f"Admin GC: {gc_label}",                                "own:crik:gcmenu")],
        [btn(BLUE,           f"Start Image: {img_label}",                            "own:crik:imgmenu")],
        [btn(YELLOW,         "Back",                                                  "own:crik:home", icon=EMOJI_OCTAGON)],
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
            .where(CricketTour.bot_id == bot_id, CricketTour.active.is_(True)).limit(1)
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
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:home", icon=EMOJI_OCTAGON)]]),
        )
    except RPCError:
        await target.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:home", icon=EMOJI_OCTAGON)]]),
        )


async def _render_logs(target: Message, bot_id: int, page: int) -> None:
    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count()).select_from(OwnerLog).where(OwnerLog.bot_id == bot_id)
        )
        r = await session.execute(
            select(OwnerLog).where(OwnerLog.bot_id == bot_id)
            .order_by(OwnerLog.time.desc())
            .offset(page * PAGE_SIZE).limit(PAGE_SIZE)
        )
        logs = list(r.scalars().all())

    lines = [f"🚨 **Activity Logs** — {total} total\n"]
    for entry in logs:
        lines.append(f"• {_fmt_ist(entry.time)}\n  {entry.action[:80]}")

    rows = list(_paginate_kb("own:crik:logs", page, total, "own:crik:home"))
    text = "\n".join(lines) if logs else "🚨 No activity logs yet."
    try:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ── Admin GC menu ─────────────────────────────────────────────────────────────

async def _render_gc_menu(target: Message, bot_id: int, user_id: int) -> None:
    async with AsyncSessionLocal() as session:
        s = await _get_or_create_settings(bot_id, session)
        gc = s.admin_gc
        await session.commit()

    gc_text = f"Currently set to: `{gc}`" if gc else "Not set — owner DM only."
    rows = []
    if gc:
        rows.append([btn(DANGER, "Clear GC", "own:crik:cleargc", icon=EMOJI_TRASH)])
    rows.append([btn(PRIMARY, "Set New GC", "own:crik:setgc", icon=EMOJI_SPARKLE)])
    rows.append([btn(YELLOW,  "Back",       "own:crik:settings", icon=EMOJI_OCTAGON)])

    text = (
        "🔔 **Admin Group Chat**\n\n"
        "Registration notifications are sent here (and to your owner DM).\n\n"
        f"{gc_text}\n\n"
        "Tap **Set New GC** and forward a message from your group, or send its @username."
    )
    try:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ── Start image menu ──────────────────────────────────────────────────────────

async def _render_img_menu(target: Message, bot_id: int) -> None:
    async with AsyncSessionLocal() as session:
        s       = await _get_or_create_settings(bot_id, session)
        img_off = s.welcome_image_disabled
        await session.commit()
    async with AsyncSessionLocal() as session:
        bot_row    = await session.get(BotModel, bot_id)
        custom_img = bot_row.welcome_image if bot_row else None

    state = "Disabled" if img_off else ("Custom image set" if custom_img else "Default image")
    toggle_label = "Enable Image" if img_off else "Disable Image"

    rows = [
        [btn(YELLOW if img_off else DANGER, toggle_label, "own:crik:imgtoggle")],
        [btn(PRIMARY, "Set Custom Image URL", "own:crik:setimg", icon=EMOJI_SPARKLE)],
    ]
    if custom_img:
        rows.append([btn(RED, "Remove Custom Image", "own:crik:clearimg", icon=EMOJI_TRASH)])
    rows.append([btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)])

    text = (
        f"📸 **Start Image Settings**\n\n"
        f"Current: **{state}**\n\n"
        "• **Disable** — send text only on /start\n"
        "• **Set Custom URL** — paste a direct image link (Telegraph/Imgur etc.)\n"
        "• **Remove Custom** — revert to default Nexora image"
    )
    try:
        await target.edit_text(text, reply_markup=InlineKeyboardMarkup(rows))
    except RPCError:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ── IST Date Picker ───────────────────────────────────────────────────────────

def _date_picker_kb(epoch: float) -> InlineKeyboardMarkup:
    dt_ist   = _epoch_to_utc(epoch) + IST
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
            btn(GREEN,  "Confirm", "crikd:ok", icon=EMOJI_CHECK),
            btn(DANGER, "Cancel",  "crikd:cl", icon=EMOJI_X),
        ],
    ])


async def _render_date_picker(client: Client, user_id: int, target: Message, bot_id: int) -> None:
    now_ist     = _now_ist()
    default_ist = now_ist.replace(hour=23, minute=59, second=0, microsecond=0) + timedelta(days=7)
    default_utc = default_ist - IST
    epoch       = default_utc.timestamp()

    clone_pending[(bot_id, user_id)] = PendingAction("crik_setdate", {"epoch": epoch})
    await target.reply_text(
        "📅 **Set Registration End Date (IST)**\n\n"
        "Use the buttons to pick the date and time.\nTap **Confirm** when done.",
        reply_markup=_date_picker_kb(epoch),
    )


# ── New tour wizard ───────────────────────────────────────────────────────────

async def _start_tour_wizard(client: Client, user_id: int, target: Message, bot_id: int) -> None:
    clone_pending[(bot_id, user_id)] = PendingAction("crik_newtour", {"step": "name"})
    await target.reply_text(
        "🏆 **New Tournament**\n\nStep 1 — Send the **tournament name:**",
        reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:tours", icon=EMOJI_X)]]),
    )


async def handle_cricket_owner_message(
    client: Client, message: Message, bot_id: int, user_id: int, pending: PendingAction
) -> None:
    text   = (message.text or "").strip()
    action = pending.action

    # ── New tour ──────────────────────────────────────────────────────────────
    if action == "crik_newtour":
        step = pending.data.get("step")
        if step == "name":
            pending.data["name"] = text[:255]
            pending.data["step"] = "details"
            clone_pending[(bot_id, user_id)] = pending
            await message.reply_text(
                f"✅ Name: **{text[:50]}**\n\nStep 2 — Send a **description** (or /skip):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:tours", icon=EMOJI_X)]]),
            )
        elif step == "details":
            details = None if text.lower() == "/skip" else text[:500]
            pending.data["details"] = details
            pending.data["step"]    = "prize"
            clone_pending[(bot_id, user_id)] = pending
            await message.reply_text(
                "Step 3 — Send the **prize pool** (or /skip):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:tours", icon=EMOJI_X)]]),
            )
        elif step == "prize":
            prize   = None if text.lower() == "/skip" else text[:128]
            name    = pending.data["name"]
            details = pending.data.get("details")
            clone_pending.pop((bot_id, user_id), None)
            async with AsyncSessionLocal() as session:
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
                f"🏆 **Tournament Started!**\n\nName: **{name}**\n"
                + (f"Details: {details}\n" if details else "")
                + (f"Prize: {prize}" if prize else ""),
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back to Tours", "own:crik:tours", icon=EMOJI_OCTAGON)]]),
            )
            await _record_action(bot_id, f"Tour started: {name}")
            await _log_cricket(client, bot_id, f"🏆 **New Tour Started:** {name}\nPrize: {prize or '—'}")

    # ── Add question ──────────────────────────────────────────────────────────
    elif action == "crik_qadd":
        step = pending.data.get("step")
        if step == "label":
            pending.data["label"] = text[:256]
            pending.data["step"]  = "type"
            clone_pending[(bot_id, user_id)] = pending
            await message.reply_text(
                f"✅ Label: **{text[:60]}**\n\nChoose input type:",
                reply_markup=InlineKeyboardMarkup([
                    [btn(PRIMARY, "Text",   "own:crik:qtype:text")],
                    [btn(PRIMARY, "Number", "own:crik:qtype:number")],
                    [btn(PRIMARY, "Choice", "own:crik:qtype:choice")],
                    [btn(DANGER,  "Cancel", "own:crik:questions", icon=EMOJI_X)],
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
                    enabled=True, required=False, order_index=max_idx + 1,
                ))
                await session.commit()
            await message.reply_text(
                f"✅ Question **{label}** added with {len(choices)} choices.",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:questions", icon=EMOJI_OCTAGON)]]),
            )

    # ── Set max players / captains ────────────────────────────────────────────
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
        cap_str = str(value) if value > 0 else "Unlimited"
        await message.reply_text(
            f"✅ Max {field} set to **{cap_str}**.",
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
        )

    # ── Set base price credit options ─────────────────────────────────────────
    elif action == "crik_set_baseprice":
        clone_pending.pop((bot_id, user_id), None)
        raw_values = [v.strip() for v in text.split(",") if v.strip()]
        if not raw_values or not all(v.isdigit() and int(v) > 0 for v in raw_values):
            await message.reply_text(
                f"{TXT_ERR} Please send positive numbers separated by commas — e.g. `10, 50, 100`.",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
            )
            return
        options = sorted({int(v) for v in raw_values})
        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            s.base_price_options = json.dumps(options)
            await session.commit()
        opt_str = ", ".join(str(v) for v in options)
        await message.reply_text(
            f"✅ Base price options updated: **{opt_str} Credits**\n\n"
            "Players will now choose from these options during registration.",
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
        )
        await _record_action(bot_id, f"Base price options set: {opt_str} Credits")

    # ── Set admin GC via @username ────────────────────────────────────────────
    elif action == "crik_set_admingc":
        clone_pending.pop((bot_id, user_id), None)

        chat = None
        if message.forward_from_chat:
            chat = message.forward_from_chat
        elif text.startswith("-100") and text.lstrip("-").isdigit():
            # Raw numeric chat ID
            gc_id = int(text)
            async with AsyncSessionLocal() as session:
                s = await _get_or_create_settings(bot_id, session)
                s.admin_gc = gc_id
                await session.commit()
            await message.reply_text(
                f"✅ Admin GC set to `{gc_id}`.",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
            )
            await _record_action(bot_id, f"Admin GC set: {gc_id}")
            return
        else:
            username = text.lstrip("@")
            try:
                chat = await client.get_chat(username)
            except RPCError:
                await message.reply_text(
                    f"{TXT_ERR} Couldn't find that chat. Make sure the bot is a member of the group/channel first.",
                    reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
                )
                return

        if chat is None:
            await message.reply_text(f"{TXT_ERR} Couldn't resolve that chat.")
            return

        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            s.admin_gc = chat.id
            await session.commit()
        title = getattr(chat, "title", None) or f"id:{chat.id}"
        await message.reply_text(
            f"✅ Admin GC set to **{title}** (`{chat.id}`).\n\n"
            "Registration notifications will now also be sent there.",
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
        )
        await _record_action(bot_id, f"Admin GC set: {title}")

    # ── Add FSub channel ─────────────────────────────────────────────────────
    elif action == "crik_addch":
        clone_pending.pop((bot_id, user_id), None)
        from pyrogram.enums import ChatType as _ChatType

        chat = None
        if message.forward_from_chat:
            chat = message.forward_from_chat
        else:
            username = text.lstrip("@")
            try:
                chat = await client.get_chat(username)
            except RPCError:
                await message.reply_text(
                    f"{TXT_ERR} Couldn't find that channel. Make sure the bot is an admin there first.",
                    reply_markup=InlineKeyboardMarkup(
                        [[btn(YELLOW, "Back", "own:crik:channels", icon=EMOJI_OCTAGON)]]
                    ),
                )
                return

        if chat is None:
            await message.reply_text(
                f"{TXT_ERR} Please forward a message from the channel or send its @username.",
                reply_markup=InlineKeyboardMarkup(
                    [[btn(YELLOW, "Back", "own:crik:channels", icon=EMOJI_OCTAGON)]]
                ),
            )
            return

        try:
            member = await client.get_chat_member(chat.id, "me")
        except RPCError:
            await message.reply_text(
                f"{TXT_ERR} This bot must be an **admin** of that channel first.",
                reply_markup=InlineKeyboardMarkup(
                    [[btn(YELLOW, "Back", "own:crik:channels", icon=EMOJI_OCTAGON)]]
                ),
            )
            return
        if member.status.name not in ("ADMINISTRATOR", "OWNER"):
            await message.reply_text(
                f"{TXT_ERR} This bot must be an **admin** of that channel first.",
                reply_markup=InlineKeyboardMarkup(
                    [[btn(YELLOW, "Back", "own:crik:channels", icon=EMOJI_OCTAGON)]]
                ),
            )
            return

        async with AsyncSessionLocal() as session:
            existing = await session.execute(
                select(BotChannel).where(BotChannel.bot_id == bot_id, BotChannel.chat_id == chat.id)
            )
            if existing.scalar_one_or_none():
                await message.reply_text(
                    f"{TXT_WARN} That channel is already in the FSub list.",
                    reply_markup=InlineKeyboardMarkup(
                        [[btn(YELLOW, "Back", "own:crik:channels", icon=EMOJI_OCTAGON)]]
                    ),
                )
                return
            session.add(BotChannel(
                bot_id=bot_id,
                chat_id=chat.id,
                username=getattr(chat, "username", None),
                title=getattr(chat, "title", None),
            ))
            await session.commit()

        label = getattr(chat, "title", None) or getattr(chat, "username", str(chat.id))
        await message.reply_text(
            f"✅ **{label}** added as a force-subscribe channel.\n\n"
            "Users must join this channel before registering.",
            reply_markup=InlineKeyboardMarkup(
                [[btn(YELLOW, "Back to Channels", "own:crik:channels", icon=EMOJI_OCTAGON)]]
            ),
        )
        await _record_action(bot_id, f"FSub channel added: {label}")

    # ── Set welcome image URL ─────────────────────────────────────────────────
    elif action == "crik_set_welcome_img":
        clone_pending.pop((bot_id, user_id), None)
        url = text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.reply_text(
                f"{TXT_ERR} Please send a valid image URL starting with `https://`.",
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
            )
            return
        async with AsyncSessionLocal() as session:
            bot_row = await session.get(BotModel, bot_id)
            if bot_row:
                bot_row.welcome_image = url
                await session.commit()
        await message.reply_text(
            "✅ **Start image updated!**\n\nThe new image will appear on the next /start.",
            reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
        )
        await _record_action(bot_id, "Welcome image URL updated")


# ── Owner panel dispatcher ────────────────────────────────────────────────────

async def dispatch_cricket_owner_action(
    client: Client, user_id: int, target: Message, action: str, bot_id: int
) -> None:

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
                p_name   = p.full_name or f"id:{p.user_id}"
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
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:questions", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Send the **question label** (e.g. \"Batting Hand\"):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:questions", icon=EMOJI_X)]]),
            )

    elif action.startswith("own:crik:qtype:"):
        q_type  = action.split(":")[3]
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
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:questions", icon=EMOJI_X)]]),
                )
            except RPCError:
                await target.reply_text(
                    f"{TXT_INFO} Send choices separated by commas:",
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:questions", icon=EMOJI_X)]]),
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
                    reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:questions", icon=EMOJI_OCTAGON)]]),
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
        field = action.split(":")[3]
        clone_pending[(bot_id, user_id)] = PendingAction("crik_setmax", {"field": field})
        label = "players" if field == "players" else "captains"
        try:
            await target.edit_text(
                f"{TXT_INFO} Send the **max number of {label}** (send `0` for unlimited):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:settings", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(f"{TXT_INFO} Send max number of {label} (0 = unlimited):")

    elif action == "own:crik:setbaseprice":
        clone_pending[(bot_id, user_id)] = PendingAction("crik_set_baseprice", {})
        current = ", ".join(str(v) for v in await _get_base_price_options(bot_id))
        try:
            await target.edit_text(
                f"{TXT_INFO} **Set Base Price Options**\n\n"
                f"Currently: `{current}` Credits\n\n"
                "Send the new options as numbers separated by commas.\n"
                "Example: `10, 50, 100`\n\n"
                "Players will pick one of these as their base price during registration "
                "(in **Credits**, not currency).",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:settings", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Send the new base price options separated by commas (e.g. `10, 50, 100`):",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:settings", icon=EMOJI_X)]]),
            )

    # ── Admin GC actions ──────────────────────────────────────────────────────
    elif action == "own:crik:gcmenu":
        await _render_gc_menu(target, bot_id, user_id)

    elif action == "own:crik:setgc":
        clone_pending[(bot_id, user_id)] = PendingAction("crik_set_admingc", {})
        try:
            await target.edit_text(
                f"{TXT_INFO} **Set Admin Group Chat**\n\n"
                "Forward any message **from the group** where you want to receive notifications,\n"
                "or send the group's **@username** or numeric **chat ID** (e.g. `-1001234567890`).\n\n"
                "Make sure this bot is a member of that group first.",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:gcmenu", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Forward a message from the group or send its @username / chat ID:",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:gcmenu", icon=EMOJI_X)]]),
            )

    elif action == "own:crik:cleargc":
        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            s.admin_gc = None
            await session.commit()
        await _record_action(bot_id, "Admin GC cleared")
        await _render_gc_menu(target, bot_id, user_id)

    # ── Start image actions ───────────────────────────────────────────────────
    elif action == "own:crik:imgmenu":
        await _render_img_menu(target, bot_id)

    elif action == "own:crik:imgtoggle":
        async with AsyncSessionLocal() as session:
            s = await _get_or_create_settings(bot_id, session)
            s.welcome_image_disabled = not s.welcome_image_disabled
            new_state = s.welcome_image_disabled
            await session.commit()
        await _record_action(bot_id, f"Welcome image {'disabled' if new_state else 'enabled'}")
        await _render_img_menu(target, bot_id)

    elif action == "own:crik:setimg":
        clone_pending[(bot_id, user_id)] = PendingAction("crik_set_welcome_img", {})
        try:
            await target.edit_text(
                f"{TXT_INFO} **Set Start Image**\n\n"
                "Send a direct image URL (must start with `https://`).\n\n"
                "Tip: upload to Telegraph (telegra.ph) and copy the image link.",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:imgmenu", icon=EMOJI_X)]]),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Send the image URL (https://...)",
                reply_markup=InlineKeyboardMarkup([[btn(DANGER, "Cancel", "own:crik:imgmenu", icon=EMOJI_X)]]),
            )

    elif action == "own:crik:clearimg":
        async with AsyncSessionLocal() as session:
            bot_row = await session.get(BotModel, bot_id)
            if bot_row:
                bot_row.welcome_image = None
                await session.commit()
        await _record_action(bot_id, "Custom welcome image removed (reverted to default)")
        await _render_img_menu(target, bot_id)

    elif action == "own:crik:stats":
        await _render_stats(target, bot_id)

    elif action == "own:crik:logs" or action.startswith("own:crik:logs:"):
        page = int(action.split(":")[-1]) if action != "own:crik:logs" else 0
        await _render_logs(target, bot_id, page)

    # ── FSub / Channels ───────────────────────────────────────────────────────
    elif action == "own:crik:channels":
        await _render_cricket_channels(target, bot_id)

    elif action == "own:crik:addch":
        clone_pending[(bot_id, user_id)] = PendingAction("crik_addch", {})
        try:
            await target.edit_text(
                f"{TXT_INFO} **Add Force-Subscribe Channel**\n\n"
                "Forward a message **from the channel** you want users to join before registering,\n"
                "or send its **@username**.\n\n"
                "Make sure this bot is an **admin** in that channel first.",
                reply_markup=InlineKeyboardMarkup(
                    [[btn(DANGER, "Cancel", "own:crik:channels", icon=EMOJI_X)]]
                ),
            )
        except RPCError:
            await target.reply_text(
                f"{TXT_INFO} Forward a message from the channel or send its @username:",
                reply_markup=InlineKeyboardMarkup(
                    [[btn(DANGER, "Cancel", "own:crik:channels", icon=EMOJI_X)]]
                ),
            )

    elif action.startswith("own:crik:rmch:"):
        ch_id = int(action.split(":")[3])
        async with AsyncSessionLocal() as session:
            ch = await session.get(BotChannel, ch_id)
            if ch and ch.bot_id == bot_id:
                label = ch.title or ch.username or str(ch.chat_id)
                await session.delete(ch)
                await session.commit()
                await _record_action(bot_id, f"FSub channel removed: {label}")
        await _render_cricket_channels(target, bot_id)

    # ── Captain-only question toggle ──────────────────────────────────────────
    elif action.startswith("own:crik:qtoggle_cap:"):
        q_id = int(action.split(":")[3])
        async with AsyncSessionLocal() as session:
            q = await session.get(CricketQuestion, q_id)
            if q and q.bot_id == bot_id:
                q.captain_only = not q.captain_only
                await session.commit()
        await _render_questions(target, bot_id)


# ── Date picker callback handler ──────────────────────────────────────────────

async def handle_date_picker_callback(
    client: Client, cq: CallbackQuery, bot_id: int, user_id: int
) -> None:
    data    = cq.data
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
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
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
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:crik:settings", icon=EMOJI_OCTAGON)]]),
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

    @app.on_callback_query(filters.regex(r"^crik:"))
    async def cricket_callback(client: Client, cq: CallbackQuery) -> None:
        data    = cq.data
        user_id = cq.from_user.id

        if data == "crik:mystatus":
            await _handle_my_status(client, cq, bot_id)

        elif data == "crik:ryes":
            await _start_wizard(client, user_id, cq.message, bot_id, is_captain=False)

        elif data == "crik:cap":
            await _start_wizard(client, user_id, cq.message, bot_id, is_captain=True)

        elif data == "crik:cancel":
            clone_pending.pop((bot_id, user_id), None)
            await cq.message.edit_text("❌ Registration cancelled.")

        elif data == "crik:back":
            await cq.message.delete()

        elif data.startswith("crik:role:"):
            role_key = data.split(":")[2]
            pending  = clone_pending.get((bot_id, user_id))
            if not pending or pending.action != "crik_wizard":
                await cq.answer("Session expired. Start again.", show_alert=True)
                return
            await _wizard_role_selected(client, cq, bot_id, user_id, role_key, pending)

        elif data.startswith("crik:bp:"):
            value   = data.split(":")[2]
            pending = clone_pending.get((bot_id, user_id))
            if not pending or pending.action != "crik_wizard":
                await cq.answer("Session expired. Start again.", show_alert=True)
                return
            pending.data["answers"]["base_price"] = f"{value} Credits"
            clone_pending[(bot_id, user_id)] = pending
            await _advance_to_next_question(client, cq.message, bot_id, user_id, pending)

        elif data.startswith("crik:ch:"):
            parts        = data.split(":")
            q_index      = int(parts[2])
            choice_index = int(parts[3])
            pending      = clone_pending.get((bot_id, user_id))
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
                        pending.data["q_index"]        = q_index + 1
                        clone_pending[(bot_id, user_id)] = pending
                        await _advance_to_next_question(client, cq.message, bot_id, user_id, pending)

        elif data.startswith("crik:skip:"):
            q_index = int(data.split(":")[2])
            pending = clone_pending.get((bot_id, user_id))
            if not pending:
                await cq.answer("Session expired.", show_alert=True)
                return
            pending.data["q_index"] = q_index + 1
            clone_pending[(bot_id, user_id)] = pending
            await _advance_to_next_question(client, cq.message, bot_id, user_id, pending)

        elif data == "crik:submit":
            pending = clone_pending.get((bot_id, user_id))
            if not pending or pending.action != "crik_confirm":
                await cq.answer("Session expired.", show_alert=True)
                return
            await _submit_registration(client, cq, bot_id, user_id, pending)

        elif data == "crik:redo":
            pending = clone_pending.pop((bot_id, user_id), None)
            is_cap  = pending.data.get("is_captain", False) if pending else False
            await _start_wizard(client, user_id, cq.message, bot_id, is_captain=is_cap)

        elif data.startswith("crik:apr:") or data.startswith("crik:rej:") or data.startswith("crik:wl:"):
            parts     = data.split(":")
            action    = parts[1]
            player_id = int(parts[2])
            await _handle_approval(client, cq, action, player_id, bot_id)

        await cq.answer()

    @app.on_callback_query(filters.regex(r"^crikd:"))
    async def cricket_datepicker_callback(client: Client, cq: CallbackQuery) -> None:
        await handle_date_picker_callback(client, cq, bot_id, cq.from_user.id)
