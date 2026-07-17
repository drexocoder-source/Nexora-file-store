"""Handlers attached to every dynamically created clone bot Client.

`register_clone_handlers(app)` is called once per clone right after it is
constructed. `app.bot_db_id` is set beforehand so every handler knows which
`bots` row it belongs to.

Supports two bot types:
  • filestore    — store & share files with users
  • linkprotect  — protect URLs behind a force-subscribe gate
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.enums import ChatType
from pyrogram.errors import RPCError, UserIsBlocked, PeerIdInvalid, UsernameNotOccupied
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from database.engine import AsyncSessionLocal
from database.models import Bot as BotModel
from database.models import (
    BotChannel, BotSettings, CloneUser, Owner, OwnerLog,
    ProtectedLink, UploadedFile,
)
from keyboards import (
    BLUE, DANGER, DEFAULT, GREEN, PRIMARY, RED, SUCCESS, YELLOW,
    EMOJI_BELL, EMOJI_CHART, EMOJI_CHECK, EMOJI_CROWN, EMOJI_DEVIL,
    EMOJI_FIRE, EMOJI_FOLDER, EMOJI_GLOBE, EMOJI_GUARD, EMOJI_LINK,
    EMOJI_LOCK, EMOJI_MIC, EMOJI_OCTAGON, EMOJI_PHONE, EMOJI_SIREN,
    EMOJI_SPARKLE, EMOJI_STAR, EMOJI_STOP, EMOJI_TOOLS, EMOJI_TRASH,
    IMG_CLONE, TXT_ERR, TXT_INFO, TXT_OK, TXT_WARN,
    back_kb, btn, quote, yes_no_kb,
)
from utils.fsub import missing_channels
from utils.state import PendingAction, clone_pending

log = logging.getLogger("nexora.clonebot")

MEDIA_FILTER = filters.photo | filters.video | filters.document | filters.audio | filters.animation


# ── Media helpers ─────────────────────────────────────────────────────────────
def _media_kind(message: Message) -> tuple[str, str, str, int | None] | None:
    for kind, obj in (
        ("photo",     message.photo),
        ("video",     message.video),
        ("document",  message.document),
        ("audio",     message.audio),
        ("animation", message.animation),
    ):
        if obj:
            return kind, obj.file_id, obj.file_unique_id, getattr(obj, "file_size", None)
    return None


async def _send_file(client: Client, chat_id: int, f: UploadedFile, protect: bool) -> None:
    kwargs = dict(caption=f.caption or None, protect_content=protect)
    if f.type == "photo":
        await client.send_photo(chat_id, f.file_id, **kwargs)
    elif f.type == "video":
        await client.send_video(chat_id, f.file_id, **kwargs)
    elif f.type == "audio":
        await client.send_audio(chat_id, f.file_id, **kwargs)
    elif f.type == "animation":
        await client.send_animation(chat_id, f.file_id, **kwargs)
    else:
        await client.send_document(chat_id, f.file_id, **kwargs)


async def _log_event(client: Client, bot_row: BotModel, text: str) -> None:
    if not bot_row.log_channel:
        return
    try:
        await client.send_message(bot_row.log_channel, text)
    except RPCError:
        log.exception("Failed to write clone log for bot %s", bot_row.id)


async def _get_bot(session: AsyncSession, bot_id: int) -> BotModel | None:
    return await session.get(
        BotModel,
        bot_id,
        options=[
            selectinload(BotModel.channels),
            selectinload(BotModel.settings),
            selectinload(BotModel.files),
            selectinload(BotModel.protected_links),
        ],
    )


async def _record_owner_action(bot_id: int, action: str) -> None:
    async with AsyncSessionLocal() as session:
        session.add(OwnerLog(bot_id=bot_id, action=action))
        await session.commit()


async def _is_owner(bot_id: int, user_id: int) -> bool:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Owner.telegram_id).join(BotModel).where(BotModel.id == bot_id)
        )
        telegram_id = result.scalar_one_or_none()
    return telegram_id == user_id


# ── Owner panel keyboards ─────────────────────────────────────────────────────
def owner_panel_kb(bot_type: str = "filestore") -> InlineKeyboardMarkup:
    if bot_type == "linkprotect":
        return InlineKeyboardMarkup([
            [
                btn(BLUE,   "🔗 My Links",    "own:links",     icon=EMOJI_LINK),
                btn(BLUE,   "➕ Add Link",    "own:addlink",   icon=EMOJI_SPARKLE),
            ],
            [
                btn(YELLOW, "📣 Broadcast",   "own:broadcast", icon=EMOJI_MIC),
                btn(YELLOW, "📊 Stats",       "own:stats",     icon=EMOJI_CHART),
            ],
            [
                btn(YELLOW, "📺 Channels",    "own:channels",  icon=EMOJI_GUARD),
                btn(YELLOW, "⚙️ Settings",   "own:settings",  icon=EMOJI_TOOLS),
            ],
            [
                btn(YELLOW, "🚨 Logs",        "own:logs",      icon=EMOJI_SIREN),
                btn(BLUE,   "💾 Backup",      "own:backup"),
            ],
            [btn(RED,    "❌ Close",          "own:close",     icon=EMOJI_OCTAGON)],
        ])
    # filestore
    return InlineKeyboardMarkup([
        [
            btn(BLUE,   "📺 Channels",    "own:channels",  icon=EMOJI_GUARD),
            btn(BLUE,   "📤 Upload Files","own:upload",    icon=EMOJI_SPARKLE),
        ],
        [
            btn(YELLOW, "📣 Broadcast",   "own:broadcast", icon=EMOJI_MIC),
            btn(YELLOW, "📂 Files",       "own:files",     icon=EMOJI_FOLDER),
        ],
        [
            btn(YELLOW, "⚙️ Settings",   "own:settings",  icon=EMOJI_TOOLS),
            btn(BLUE,   "📊 Stats",       "own:stats",     icon=EMOJI_CHART),
        ],
        [
            btn(YELLOW, "🚨 Logs",        "own:logs",      icon=EMOJI_SIREN),
            btn(BLUE,   "💾 Backup",      "own:backup"),
        ],
        [btn(RED,    "❌ Close",          "own:close",     icon=EMOJI_OCTAGON)],
    ])


# ── Main registration ─────────────────────────────────────────────────────────
def register_clone_handlers(app: Client) -> None:
    bot_id: int = app.bot_db_id  # type: ignore[attr-defined]

    # ── /start ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("start") & filters.private)
    async def clone_start(client: Client, message: Message) -> None:
        payload = message.command[1] if len(message.command) > 1 else None

        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            owner_result = await session.execute(select(Owner).where(Owner.id == bot_row.owner_id))
            owner = owner_result.scalar_one()
            settings_row = bot_row.settings or BotSettings(bot_id=bot_id)

            user_result = await session.execute(
                select(CloneUser).where(
                    CloneUser.bot_id == bot_id, CloneUser.user_id == message.from_user.id
                )
            )
            clone_user = user_result.scalar_one_or_none()
            first_time = clone_user is None
            if clone_user is None:
                clone_user = CloneUser(
                    bot_id=bot_id,
                    user_id=message.from_user.id,
                    username=message.from_user.username,
                    name=message.from_user.first_name,
                )
                session.add(clone_user)
                await session.flush()
                await _log_event(
                    client, bot_row,
                    f"👤 **New User Joined**\n"
                    f"Name: {message.from_user.first_name}\n"
                    f"Username: @{message.from_user.username}\n"
                    f"ID: `{message.from_user.id}`\n"
                    f"Time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
                )
            else:
                clone_user.last_seen = datetime.now(timezone.utc)

            channels = list(bot_row.channels)
            bot_type = bot_row.bot_type or "filestore"
            await session.commit()
            is_bot_owner = message.from_user.id == owner.telegram_id

        # Force subscribe check
        need_check = settings_row.force_subscribe and channels and not is_bot_owner
        missing = await missing_channels(client, channels, message.from_user.id) if need_check else []

        if missing:
            rows = []
            for ch in missing:
                label = ch.title or ch.username or "Channel"
                if ch.username:
                    rows.append([btn(BLUE, f"📲 Join {label}", url=f"https://t.me/{ch.username}", icon=EMOJI_DEVIL)])
                else:
                    rows.append([btn(BLUE, f"📲 {label}", "noop", icon=EMOJI_DEVIL)])
            rows.append([btn(GREEN, "✅ Verify Membership", f"verify:{payload or ''}", icon=EMOJI_CHECK)])
            try:
                await message.reply_photo(
                    IMG_CLONE,
                    caption=(
                        f"👋 **Welcome to {bot_row.bot_name or bot_row.bot_username}**\n\n"
                        "🔒 Join all required channels below to unlock access,\n"
                        "then press **Verify Membership**."
                    ),
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            except RPCError:
                await message.reply_text(
                    f"👋 **Welcome to {bot_row.bot_name or bot_row.bot_username}**\n\n"
                    "🔒 Join all required channels below, then press **Verify Membership**.",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            return

        if bot_type == "linkprotect":
            # For link protect: if a payload token is given, deliver that link
            await _handle_linkprotect_start(client, message, bot_id, payload, settings_row, bot_row, first_time)
        else:
            greeting = (
                "👋 **Welcome back!**\n\nHere are your files."
                if not first_time
                else (settings_row.custom_start or bot_row.welcome_caption or f"Welcome to {bot_row.bot_name}")
            )
            try:
                await message.reply_photo(
                    IMG_CLONE,
                    caption=greeting,
                    reply_parameters=quote(message.id),
                )
            except RPCError:
                await message.reply_text(greeting, reply_parameters=quote(message.id))
            await _deliver(client, message.chat.id, bot_id, payload, settings_row.protect_content)

    async def _handle_linkprotect_start(
        client: Client, message: Message, bot_id: int,
        payload: str | None, settings_row, bot_row, first_time: bool
    ) -> None:
        if payload and payload.startswith("lp_"):
            token = payload[3:]
            async with AsyncSessionLocal() as session:
                link_row = await session.execute(
                    select(ProtectedLink).where(
                        ProtectedLink.bot_id == bot_id, ProtectedLink.token == token
                    )
                )
                link_row = link_row.scalar_one_or_none()
                if link_row is None:
                    await message.reply_text(f"{TXT_ERR} This link is invalid or has been removed.")
                    return
                link_row.click_count += 1
                url = link_row.original_url
                title = link_row.title or "Protected Link"
                await session.commit()
            await message.reply_text(
                f"🔗 **{title}**\n\n"
                f"Here is your protected link:\n{url}",
                reply_markup=InlineKeyboardMarkup([
                    [btn(PRIMARY, "🌐 Open Link", url=url, icon=EMOJI_LINK)],
                ]),
                protect_content=settings_row.protect_content,
            )
            await _log_event(
                client, bot_row,
                f"🔗 **Link accessed**\n"
                f"User: {message.from_user.id} (@{message.from_user.username})\n"
                f"Link: {title}\n"
                f"URL: {url}",
            )
        else:
            greeting = (
                "👋 **Welcome back!**" if not first_time
                else (settings_row.custom_start or bot_row.welcome_caption or f"Welcome to {bot_row.bot_name}")
            )
            try:
                await message.reply_photo(
                    IMG_CLONE,
                    caption=(
                        f"{greeting}\n\n"
                        "🔗 This is a **Link Protect** bot.\n"
                        "Share protected links with your audience — only members can access them."
                    ),
                    reply_parameters=quote(message.id),
                )
            except RPCError:
                await message.reply_text(
                    f"{greeting}\n\n🔗 This is a **Link Protect** bot.",
                    reply_parameters=quote(message.id),
                )

    async def _deliver(client: Client, chat_id: int, bot_id: int, payload: str | None, protect: bool) -> None:
        async with AsyncSessionLocal() as session:
            if payload and payload.startswith("file_"):
                try:
                    file_pk = int(payload.split("_", 1)[1])
                except ValueError:
                    file_pk = None
                files: list[UploadedFile] = []
                if file_pk is not None:
                    f = await session.get(UploadedFile, file_pk)
                    if f is not None and f.bot_id == bot_id:
                        files = [f]
            else:
                result = await session.execute(
                    select(UploadedFile)
                    .where(UploadedFile.bot_id == bot_id)
                    .order_by(UploadedFile.position)
                )
                files = list(result.scalars().all())

        if not files:
            await client.send_message(chat_id, f"{TXT_WARN} No files are available yet. Check back later.")
            return

        for f in files:
            try:
                await _send_file(client, chat_id, f, protect)
            except RPCError:
                log.exception("Failed sending file %s to %s", f.id, chat_id)

    # ── /verify callback ──────────────────────────────────────────────────────
    @app.on_callback_query(filters.regex(r"^verify:"))
    async def verify_cb(client: Client, cq: CallbackQuery) -> None:
        payload = cq.data.split(":", 1)[1] or None
        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            channels = list(bot_row.channels)
            settings_row = bot_row.settings
            bot_type = bot_row.bot_type or "filestore"

        missing = await missing_channels(client, channels, cq.from_user.id)
        if missing:
            await cq.answer("❌ You haven't joined all required channels yet.", show_alert=True)
            return

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(CloneUser).where(
                    CloneUser.bot_id == bot_id, CloneUser.user_id == cq.from_user.id
                )
            )
            clone_user = result.scalar_one_or_none()
            if clone_user:
                clone_user.verified = True
            await session.commit()

        async with AsyncSessionLocal() as session:
            bot_row2 = await _get_bot(session, bot_id)
        await _log_event(
            client, bot_row2,
            f"✅ **User Verified**\n"
            f"ID: `{cq.from_user.id}`\n"
            f"Time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        )

        protect = settings_row.protect_content if settings_row else False
        if bot_type == "linkprotect":
            await cq.message.edit_text(
                f"✅ Verified! Welcome to **{bot_row.bot_name or bot_row.bot_username}**\n\n"
                "🔗 You can now access all protected links."
            )
        else:
            await cq.message.edit_text(
                f"✅ Verified! Welcome to **{bot_row.bot_name or bot_row.bot_username}**\n\nHere are your files."
            )
            await _deliver(client, cq.message.chat.id, bot_id, payload, protect)
        await cq.answer("✅ Access granted!")

    @app.on_callback_query(filters.regex(r"^noop$"))
    async def noop_cb(client: Client, cq: CallbackQuery) -> None:
        await cq.answer()

    # ── /owner ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("owner") & filters.private)
    async def owner_cmd(client: Client, message: Message) -> None:
        if not await _is_owner(bot_id, message.from_user.id):
            return
        async with AsyncSessionLocal() as session:
            bot_row = await session.get(BotModel, bot_id)
            bot_type = bot_row.bot_type if bot_row else "filestore"
        type_label = "🔗 Link Protect" if bot_type == "linkprotect" else "📁 File Store"
        await message.reply_text(
            f"💂 **Owner Panel**\n\n{type_label}",
            reply_markup=owner_panel_kb(bot_type),
        )

    # Register shortcut commands for owner
    for cmd, action in (
        ("stats",    "own:stats"),
        ("files",    "own:files"),
        ("channels", "own:channels"),
        ("settings", "own:settings"),
        ("logs",     "own:logs"),
        ("backup",   "own:backup"),
        ("links",    "own:links"),
    ):
        def _make(action: str):
            async def _handler(client: Client, message: Message) -> None:
                if not await _is_owner(bot_id, message.from_user.id):
                    return
                await _dispatch_owner_action(client, message.from_user.id, message, action)
            return _handler

        app.add_handler(MessageHandler(_make(action), filters.command(cmd) & filters.private))

    @app.on_message(filters.command("broadcast") & filters.private)
    async def broadcast_cmd(client: Client, message: Message) -> None:
        if not await _is_owner(bot_id, message.from_user.id):
            return
        if message.reply_to_message:
            await _run_broadcast(client, message.from_user.id, message.reply_to_message, message)
        else:
            clone_pending[(bot_id, message.from_user.id)] = PendingAction("await_broadcast")
            await message.reply_text(
                f"{TXT_INFO} 📣 Send (or forward) the message you want to broadcast to all users."
            )

    # ── owner action dispatcher ───────────────────────────────────────────────
    async def _dispatch_owner_action(client: Client, user_id: int, target: Message, action: str) -> None:

        # ── get bot type ──
        async with AsyncSessionLocal() as session:
            bot_row_meta = await session.get(BotModel, bot_id)
            bot_type = bot_row_meta.bot_type if bot_row_meta else "filestore"

        # ── channels ──
        if action == "own:channels":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                channels = list(bot_row.channels)
            rows = []
            lines = ["📺 **Force Subscribe Channels**\n"]
            if not channels:
                lines.append("No channels added yet.")
            for ch in channels:
                label = ch.title or ch.username or str(ch.chat_id)
                lines.append(f"• {label}")
                rows.append([btn(RED, f"🗑 Remove {label}", f"own:rmch:{ch.id}", icon=EMOJI_TRASH)])
            rows.append([btn(BLUE, "➕ Add Channel", "own:addch", icon=EMOJI_SPARKLE)])
            rows.append([btn(YELLOW, "🔙 Back", "own:home", icon=EMOJI_OCTAGON)])
            await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

        elif action == "own:addch":
            clone_pending[(bot_id, user_id)] = PendingAction("await_channel")
            await target.reply_text(
                f"{TXT_INFO} Forward a message from the channel, or send its @username.\n"
                "Make sure this bot is an admin there first."
            )

        # ── upload (filestore only) ──
        elif action == "own:upload":
            clone_pending[(bot_id, user_id)] = PendingAction("await_upload", {"count": 0})
            await target.reply_text(
                f"{TXT_INFO} 📤 Send the files you want to store — videos, documents, photos, audio, anything.\n"
                "Press **Done** when finished.",
                reply_markup=InlineKeyboardMarkup([[btn(RED, "✅ Done", "own:upload_done", icon=EMOJI_CHECK)]]),
            )

        elif action == "own:upload_done":
            count = clone_pending.pop((bot_id, user_id), PendingAction("", {})).data.get("count", 0)
            await target.reply_text(
                f"✅ Upload finished — **{count}** file(s) stored.",
                reply_markup=owner_panel_kb(bot_type),
            )

        elif action == "own:broadcast":
            clone_pending[(bot_id, user_id)] = PendingAction("await_broadcast")
            await target.reply_text(
                f"{TXT_INFO} 📣 Send (or forward) the message you want to broadcast to all users.",
                reply_markup=InlineKeyboardMarkup([[btn(RED, "❌ Cancel", "own:home", icon=EMOJI_OCTAGON)]]),
            )

        # ── files (filestore) ──
        elif action == "own:files":
            async with AsyncSessionLocal() as session:
                counts = await session.execute(
                    select(UploadedFile.type, func.count())
                    .where(UploadedFile.bot_id == bot_id)
                    .group_by(UploadedFile.type)
                )
                counts = dict(counts.all())
                total = sum(counts.values())
            lines = [f"📂 **File Manager** — {total} files\n"]
            for kind, n in counts.items():
                lines.append(f"• {kind.title()}: **{n}**")
            await target.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [btn(RED, "🗑 Delete All Files", "own:delall", icon=EMOJI_TRASH)],
                    [btn(YELLOW, "🔙 Back", "own:home", icon=EMOJI_OCTAGON)],
                ]),
            )

        elif action == "own:delall":
            async with AsyncSessionLocal() as session:
                total = await session.scalar(
                    select(func.count()).select_from(UploadedFile).where(UploadedFile.bot_id == bot_id)
                )
            await target.reply_text(
                f"⚠️ Delete **all {total} stored files**? This cannot be undone.",
                reply_markup=yes_no_kb("own:delall_yes", "own:files"),
            )

        elif action == "own:delall_yes":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                for f in list(bot_row.files):
                    await session.delete(f)
                await session.commit()
            await _record_owner_action(bot_id, "Deleted all files")
            await target.reply_text(f"✅ All files deleted.", reply_markup=owner_panel_kb(bot_type))

        # ── links (linkprotect) ──
        elif action == "own:links":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                links = list(bot_row.protected_links)
                me = await client.get_me()
            rows = []
            lines = ["🔗 **Protected Links**\n"]
            if not links:
                lines.append("No links added yet.")
            for lk in links:
                alias = f"https://t.me/{me.username}?start=lp_{lk.token}"
                lines.append(f"• **{lk.title or 'Untitled'}**\n  Clicks: {lk.click_count}\n  Alias: `{alias}`")
                rows.append([btn(RED, f"🗑 Delete: {lk.title or lk.token[:8]}", f"own:rmlink:{lk.id}", icon=EMOJI_TRASH)])
            rows.append([btn(BLUE, "➕ Add Link", "own:addlink", icon=EMOJI_SPARKLE)])
            rows.append([btn(YELLOW, "🔙 Back", "own:home", icon=EMOJI_OCTAGON)])
            await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

        elif action == "own:addlink":
            clone_pending[(bot_id, user_id)] = PendingAction("await_link_url")
            await target.reply_text(
                f"{TXT_INFO} 🔗 Send the **URL** you want to protect.\n\nExample: `https://t.me/yourchannel`",
                reply_markup=InlineKeyboardMarkup([[btn(RED, "❌ Cancel", "own:links", icon=EMOJI_OCTAGON)]]),
            )

        elif action.startswith("own:rmlink:"):
            link_id = int(action.split(":")[2])
            async with AsyncSessionLocal() as session:
                lk = await session.get(ProtectedLink, link_id)
                if lk and lk.bot_id == bot_id:
                    await session.delete(lk)
                    await session.commit()
            await _record_owner_action(bot_id, f"Deleted protected link {link_id}")
            await _dispatch_owner_action(client, user_id, target, "own:links")

        # ── settings ──
        elif action == "own:settings":
            await _render_settings(target, bot_type=bot_type)

        elif action.startswith("own:toggle:"):
            field = action.split(":")[2]
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                s = bot_row.settings
                setattr(s, field, not getattr(s, field))
                await session.commit()
            await _record_owner_action(bot_id, f"Toggled {field}")
            await _render_settings(target, edit=True, bot_type=bot_type)

        # ── stats ──
        elif action == "own:stats":
            async with AsyncSessionLocal() as session:
                total_users = await session.scalar(
                    select(func.count()).select_from(CloneUser).where(CloneUser.bot_id == bot_id)
                )
                verified = await session.scalar(
                    select(func.count()).select_from(CloneUser)
                    .where(CloneUser.bot_id == bot_id, CloneUser.verified.is_(True))
                )
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                today_users = await session.scalar(
                    select(func.count()).select_from(CloneUser)
                    .where(CloneUser.bot_id == bot_id, CloneUser.joined_at >= today_start)
                )
                yesterday_start = today_start - timedelta(days=1)
                yesterday_users = await session.scalar(
                    select(func.count()).select_from(CloneUser)
                    .where(CloneUser.bot_id == bot_id, CloneUser.joined_at >= yesterday_start, CloneUser.joined_at < today_start)
                )
                if bot_type == "linkprotect":
                    extra_count = await session.scalar(
                        select(func.count()).select_from(ProtectedLink).where(ProtectedLink.bot_id == bot_id)
                    )
                    total_clicks = await session.scalar(
                        select(func.coalesce(func.sum(ProtectedLink.click_count), 0))
                        .where(ProtectedLink.bot_id == bot_id)
                    )
                    extra_text = f"🔗 Protected Links: **{extra_count}**\n👆 Total Clicks: **{total_clicks}**"
                else:
                    total_files = await session.scalar(
                        select(func.count()).select_from(UploadedFile).where(UploadedFile.bot_id == bot_id)
                    )
                    extra_text = f"📂 Total Files: **{total_files}**"

            text = (
                "📊 **Statistics**\n\n"
                f"👥 Total Users: **{total_users}**\n"
                f"🗓 Today: **{today_users}**\n"
                f"📅 Yesterday: **{yesterday_users}**\n"
                f"✅ Verified: **{verified}**\n"
                f"⏳ Pending: **{total_users - verified}**\n"
                f"{extra_text}"
            )
            await target.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "🔙 Back", "own:home", icon=EMOJI_OCTAGON)]]),
            )

        # ── logs ──
        elif action == "own:logs":
            clone_pending[(bot_id, user_id)] = PendingAction("await_log_channel")
            await target.reply_text(
                f"{TXT_INFO} 🚨 Forward a message from the channel you want to use for logs.\n"
                "Make the bot an admin in that channel first."
            )

        # ── backup ──
        elif action == "own:backup":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                payload = {
                    "bot_username": bot_row.bot_username,
                    "bot_type": bot_row.bot_type,
                    "welcome_caption": bot_row.welcome_caption,
                    "log_channel": bot_row.log_channel,
                    "channels": [
                        {"chat_id": c.chat_id, "username": c.username, "title": c.title, "required": c.required}
                        for c in bot_row.channels
                    ],
                    "files": [
                        {"file_id": f.file_id, "type": f.type, "caption": f.caption}
                        for f in bot_row.files
                    ],
                    "protected_links": [
                        {"token": lk.token, "url": lk.original_url, "title": lk.title, "clicks": lk.click_count}
                        for lk in bot_row.protected_links
                    ],
                    "settings": {
                        "auto_delete": bot_row.settings.auto_delete if bot_row.settings else 0,
                        "protect_content": bot_row.settings.protect_content if bot_row.settings else False,
                        "force_subscribe": bot_row.settings.force_subscribe if bot_row.settings else True,
                    },
                }
            buf = io.BytesIO(json.dumps(payload, indent=2, default=str).encode("utf-8"))
            buf.name = f"nexora_backup_bot_{bot_id}.json"
            await client.send_document(target.chat.id, buf, caption=f"{TXT_INFO} 💾 Backup exported.")

        elif action == "own:close":
            await target.reply_text("✅ Panel closed.")

        elif action == "own:home":
            await target.reply_text(
                f"💂 **Owner Panel**",
                reply_markup=owner_panel_kb(bot_type),
            )

    # ── settings renderer ─────────────────────────────────────────────────────
    async def _render_settings(target: Message, edit: bool = False, bot_type: str = "filestore") -> None:
        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            s = bot_row.settings

        def dot(v: bool) -> str:
            return BLUE if v else RED

        rows = [
            [btn(dot(s.force_subscribe), f"😈 Force Subscribe: {'✅ ON' if s.force_subscribe else '❌ OFF'}", "own:toggle:force_subscribe")],
            [btn(dot(s.protect_content), f"🔒 Protect Content: {'✅ ON' if s.protect_content else '❌ OFF'}", "own:toggle:protect_content")],
            [btn(dot(s.welcome_enabled), f"👋 Welcome Msg: {'✅ ON' if s.welcome_enabled else '❌ OFF'}", "own:toggle:welcome_enabled")],
        ]
        if bot_type == "filestore":
            rows.append([btn(dot(s.send_files_once), f"📤 Send Once: {'✅ ON' if s.send_files_once else '❌ OFF'}", "own:toggle:send_files_once")])
        rows.append([btn(YELLOW, "🔙 Back", "own:home", icon=EMOJI_OCTAGON)])

        text = "⚙️ **Settings**\n\nTap any toggle to switch it."
        markup = InlineKeyboardMarkup(rows)
        if edit:
            try:
                await target.edit_text(text, reply_markup=markup)
                return
            except RPCError:
                pass
        await target.reply_text(text, reply_markup=markup)

    # ── owner callback router ─────────────────────────────────────────────────
    @app.on_callback_query(filters.regex(r"^own:"))
    async def owner_callback(client: Client, cq: CallbackQuery) -> None:
        if not await _is_owner(bot_id, cq.from_user.id):
            await cq.answer("👑 Owner only.", show_alert=True)
            return
        await _dispatch_owner_action(client, cq.from_user.id, cq.message, cq.data)
        await cq.answer()

    # ── owner text/media intake ───────────────────────────────────────────────
    @app.on_message(
        filters.private & (filters.text | MEDIA_FILTER)
        & ~filters.command(["start", "owner", "stats", "files", "channels", "settings", "logs", "backup", "broadcast", "links"])
    )
    async def owner_intake(client: Client, message: Message) -> None:
        pending = clone_pending.get((bot_id, message.from_user.id))
        if not pending or not await _is_owner(bot_id, message.from_user.id):
            return

        if pending.action == "await_channel" and (message.forward_from_chat or message.text):
            await _handle_add_channel(client, message)
        elif pending.action == "await_upload" and _media_kind(message):
            await _handle_upload(message, pending)
        elif pending.action == "await_broadcast":
            clone_pending.pop((bot_id, message.from_user.id), None)
            await _run_broadcast(client, message.from_user.id, message, message)
        elif pending.action == "await_log_channel" and message.forward_from_chat:
            await _handle_set_log_channel(client, message)
        elif pending.action == "await_link_url" and message.text:
            await _handle_add_link_url(client, message, pending)
        elif pending.action == "await_link_title" and message.text:
            await _handle_add_link_title(client, message, pending)

    # ── channel handler ───────────────────────────────────────────────────────
    async def _handle_add_channel(client: Client, message: Message) -> None:
        chat = None
        if message.forward_from_chat:
            chat = message.forward_from_chat
        elif message.text:
            username = message.text.strip().lstrip("@")
            try:
                chat = await client.get_chat(username)
            except (UsernameNotOccupied, PeerIdInvalid, RPCError):
                await message.reply_text(f"{TXT_ERR} Couldn't find that channel/username.")
                return

        try:
            member = await client.get_chat_member(chat.id, "me")
        except RPCError:
            await message.reply_text(f"{TXT_ERR} This bot must be an admin of that channel first.")
            return
        if member.status.name not in ("ADMINISTRATOR", "OWNER"):
            await message.reply_text(f"{TXT_ERR} This bot must be an admin of that channel first.")
            return

        async with AsyncSessionLocal() as session:
            session.add(BotChannel(
                bot_id=bot_id,
                chat_id=chat.id,
                username=getattr(chat, "username", None),
                title=getattr(chat, "title", None),
                type="group" if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else "channel",
            ))
            await session.commit()
        clone_pending.pop((bot_id, message.from_user.id), None)
        await _record_owner_action(bot_id, f"Added channel {getattr(chat, 'title', chat.id)}")
        await message.reply_text(
            f"✅ Channel **{getattr(chat, 'title', chat.id)}** added.",
            reply_markup=InlineKeyboardMarkup([
                [btn(BLUE, "➕ Add Another", "own:addch"), btn(RED, "✅ Done", "own:channels")],
            ]),
        )

    # ── upload handler ────────────────────────────────────────────────────────
    async def _handle_upload(message: Message, pending: PendingAction) -> None:
        kind, file_id, file_unique_id, size = _media_kind(message)
        async with AsyncSessionLocal() as session:
            session.add(UploadedFile(
                bot_id=bot_id,
                file_unique_id=file_unique_id,
                file_id=file_id,
                type=kind,
                caption=message.caption,
                size=size,
                position=pending.data.get("count", 0),
            ))
            await session.commit()
        pending.data["count"] = pending.data.get("count", 0) + 1
        if pending.data["count"] % 20 == 0:
            await message.reply_text(f"{TXT_INFO} {pending.data['count']} files stored so far…")

    # ── log channel handler ───────────────────────────────────────────────────
    async def _handle_set_log_channel(client: Client, message: Message) -> None:
        chat = message.forward_from_chat
        try:
            member = await client.get_chat_member(chat.id, "me")
        except RPCError:
            await message.reply_text(f"{TXT_ERR} This bot must be an admin of that channel first.")
            return
        if member.status.name not in ("ADMINISTRATOR", "OWNER"):
            await message.reply_text(f"{TXT_ERR} This bot must be an admin of that channel first.")
            return
        async with AsyncSessionLocal() as session:
            bot_row = await session.get(BotModel, bot_id)
            bot_row.log_channel = chat.id
            bot_type = bot_row.bot_type or "filestore"
            await session.commit()
        clone_pending.pop((bot_id, message.from_user.id), None)
        await _record_owner_action(bot_id, f"Set log channel to {chat.title}")
        await message.reply_text(
            f"✅ Log channel set to **{chat.title}**.",
            reply_markup=owner_panel_kb(bot_type),
        )

    # ── link protect handlers ─────────────────────────────────────────────────
    async def _handle_add_link_url(client: Client, message: Message, pending: PendingAction) -> None:
        url = message.text.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await message.reply_text(
                f"{TXT_ERR} Please send a valid URL starting with `http://` or `https://`."
            )
            return
        pending.action = "await_link_title"
        pending.data["url"] = url
        await message.reply_text(
            f"{TXT_INFO} 🔗 URL saved! Now send a **title** for this link (e.g. 'My Channel Invite').\n\n"
            "Or send `/skip` to use no title."
        )

    async def _handle_add_link_title(client: Client, message: Message, pending: PendingAction) -> None:
        title = None if message.text.strip().lower() == "/skip" else message.text.strip()
        url = pending.data.get("url", "")
        async with AsyncSessionLocal() as session:
            lk = ProtectedLink(bot_id=bot_id, original_url=url, title=title)
            session.add(lk)
            await session.flush()
            token = lk.token
            bot_row = await session.get(BotModel, bot_id)
            bot_type = bot_row.bot_type if bot_row else "linkprotect"
            await session.commit()

        clone_pending.pop((bot_id, message.from_user.id), None)
        me = await client.get_me()
        alias = f"https://t.me/{me.username}?start=lp_{token}"
        await _record_owner_action(bot_id, f"Added protected link: {title or url[:40]}")
        await message.reply_text(
            f"✅ **Protected Link Created!**\n\n"
            f"📋 Title: **{title or 'Untitled'}**\n"
            f"🔗 URL: `{url}`\n\n"
            f"**Share this alias with users:**\n`{alias}`",
            reply_markup=owner_panel_kb(bot_type),
        )

    # ── broadcast ─────────────────────────────────────────────────────────────
    async def _run_broadcast(client: Client, owner_user_id: int, source: Message, reply_target: Message) -> None:
        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            result = await session.execute(select(CloneUser.user_id).where(CloneUser.bot_id == bot_id))
            user_ids = [row[0] for row in result.all()]
            from database.models import Broadcast
            record = Broadcast(bot_id=bot_id)
            session.add(record)
            await session.flush()
            record_id = record.id
            await session.commit()

        progress = await reply_target.reply_text(
            "📣 **Broadcast Started**\n\n░░░░░░░░░░ 0%",
            reply_parameters=quote(source.id, (source.text or source.caption or "")[:80] or None),
        )

        success = failed = blocked = 0
        total = len(user_ids)

        for idx, uid in enumerate(user_ids, start=1):
            try:
                await source.copy(uid)
                success += 1
            except UserIsBlocked:
                blocked += 1
            except RPCError:
                failed += 1

            if total and (idx % max(1, total // 10) == 0 or idx == total):
                filled = int((idx / total) * 10)
                bar = "█" * filled + "░" * (10 - filled)
                pct = int((idx / total) * 100)
                try:
                    await progress.edit_text(f"📣 **Broadcast in Progress**\n\n{bar} {pct}%")
                except RPCError:
                    pass

        async with AsyncSessionLocal() as session:
            from database.models import Broadcast
            record = await session.get(Broadcast, record_id)
            record.success = success
            record.failed = failed
            record.blocked = blocked
            record.completed_at = datetime.now(timezone.utc)
            await session.commit()

        await progress.edit_text(
            f"✅ **Broadcast Complete**\n\n"
            f"██████████ 100%\n\n"
            f"✔️ Delivered: **{success}**\n"
            f"❌ Failed: **{failed}**\n"
            f"🚫 Blocked: **{blocked}**\n"
            f"📊 Total: **{total}**"
        )
        await _record_owner_action(
            bot_id, f"Broadcast done (✔{success} ❌{failed} 🚫{blocked})"
        )
        async with AsyncSessionLocal() as session:
            bot_row2 = await _get_bot(session, bot_id)
        await _log_event(
            client, bot_row2,
            f"📣 **Broadcast Finished**\n"
            f"✔️ Success: {success}  ❌ Failed: {failed}  🚫 Blocked: {blocked}",
        )
