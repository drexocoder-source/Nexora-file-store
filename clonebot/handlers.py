"""Handlers attached to every dynamically created clone bot Client.

`register_clone_handlers(app)` is called once per clone right after it is
constructed (see bot_manager.BotManager.start_clone). `app.bot_db_id` is set
beforehand so every handler knows which `bots` row it belongs to.
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
from database.models import BotChannel, BotSettings, CloneUser, Owner, OwnerLog, UploadedFile
from keyboards import (
    BLUE,
    EMOJI_DEVIL,
    EMOJI_GUARD,
    EMOJI_MIC,
    EMOJI_OCTAGON,
    EMOJI_SIREN,
    EMOJI_SPARKLE,
    EMOJI_STOP,
    EMOJI_TOOLS,
    GREEN,
    RED,
    TXT_ERR,
    TXT_INFO,
    TXT_OK,
    TXT_WARN,
    YELLOW,
    back_kb,
    btn,
    quote,
    yes_no_kb,
)
from utils.fsub import missing_channels
from utils.state import PendingAction, clone_pending

log = logging.getLogger("nexora.clonebot")

MEDIA_FILTER = filters.photo | filters.video | filters.document | filters.audio | filters.animation


def _media_kind(message: Message) -> tuple[str, str, str, int | None] | None:
    for kind, obj in (
        ("photo", message.photo),
        ("video", message.video),
        ("document", message.document),
        ("audio", message.audio),
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


# Every place we later touch bot_row.channels / .settings / .files as a plain
# attribute needs those relationships eagerly loaded up front. Lazy-loading a
# relationship on an object bound to an AsyncSession triggers implicit IO
# that only works inside the greenlet context set up by session.execute();
# touching it as a bare attribute outside that raises
# sqlalchemy.exc.MissingGreenlet. selectinload() sidesteps this by loading
# everything as part of the initial query.
async def _get_bot(session: AsyncSession, bot_id: int) -> BotModel | None:
    return await session.get(
        BotModel,
        bot_id,
        options=[
            selectinload(BotModel.channels),
            selectinload(BotModel.settings),
            selectinload(BotModel.files),
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


def owner_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                btn(BLUE, "Channels", "own:channels", icon=EMOJI_GUARD),
                btn(BLUE, "Upload Files", "own:upload", icon=EMOJI_SPARKLE),
            ],
            [
                btn(YELLOW, "Broadcast", "own:broadcast", icon=EMOJI_MIC),
                btn(YELLOW, "Files", "own:files"),
            ],
            [
                btn(YELLOW, "Settings", "own:settings", icon=EMOJI_TOOLS),
                btn(BLUE, "Statistics", "own:stats", icon=EMOJI_SPARKLE),
            ],
            [
                btn(YELLOW, "Logs", "own:logs", icon=EMOJI_SIREN),
                btn(BLUE, "Backup", "own:backup"),
            ],
            [btn(RED, "Close", "own:close", icon=EMOJI_OCTAGON)],
        ]
    )


def register_clone_handlers(app: Client) -> None:
    bot_id: int = app.bot_db_id  # type: ignore[attr-defined]

    # ---------------------------------------------------------------- start
    @app.on_message(filters.command("start") & filters.private)
    async def clone_start(client: Client, message: Message) -> None:
        payload = message.command[1] if len(message.command) > 1 else None

        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            owner_result = await session.execute(select(Owner).where(Owner.id == bot_row.owner_id))
            owner = owner_result.scalar_one()
            settings_row = bot_row.settings or BotSettings(bot_id=bot_id)

            user_result = await session.execute(
                select(CloneUser).where(CloneUser.bot_id == bot_id, CloneUser.user_id == message.from_user.id)
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
                    client,
                    bot_row,
                    f"👤 New user\nName: {message.from_user.first_name}\n"
                    f"Username: @{message.from_user.username}\nID: {message.from_user.id}\n"
                    f"Time: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
                )
            else:
                clone_user.last_seen = datetime.now(timezone.utc)

            channels = list(bot_row.channels)
            await session.commit()
            is_bot_owner = message.from_user.id == owner.telegram_id

        need_check = settings_row.force_subscribe and channels and not is_bot_owner
        if need_check:
            missing = await missing_channels(client, channels, message.from_user.id)
        else:
            missing = []

        if missing:
            rows = []
            for ch in missing:
                if ch.username:
                    rows.append(
                        [btn(BLUE, f"Join {ch.title or ch.username}", url=f"https://t.me/{ch.username}", icon=EMOJI_DEVIL)]
                    )
                else:
                    rows.append([btn(BLUE, f"Join {ch.title or ch.chat_id}", "noop", icon=EMOJI_DEVIL)])
            rows.append([btn(GREEN, "Verify", f"verify:{payload or ''}", icon=EMOJI_SPARKLE)])
            await message.reply_text(
                f"**Welcome to {bot_row.bot_name or bot_row.bot_username}**\n\n"
                "Before accessing files you must join every required channel below.\n\n"
                "Press each Join button, then press Verify.",
                reply_markup=InlineKeyboardMarkup(rows),
                reply_parameters=quote(message.id),
            )
            return

        greeting = "Welcome Back 👋\n\nEnjoy your files." if not first_time else (
            settings_row.custom_start or bot_row.welcome_caption or f"Welcome to {bot_row.bot_name}"
        )
        await message.reply_text(greeting, reply_parameters=quote(message.id))
        await _deliver(client, message.chat.id, bot_id, payload, settings_row.protect_content)

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
                    select(UploadedFile).where(UploadedFile.bot_id == bot_id).order_by(UploadedFile.position)
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

    # --------------------------------------------------------------- verify
    @app.on_callback_query(filters.regex(r"^verify:"))
    async def verify_cb(client: Client, cq: CallbackQuery) -> None:
        payload = cq.data.split(":", 1)[1] or None
        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            channels = list(bot_row.channels)
            settings_row = bot_row.settings

        missing = await missing_channels(client, channels, cq.from_user.id)
        if missing:
            await cq.answer("❌ Join all channels first.", show_alert=True)
            return

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(CloneUser).where(CloneUser.bot_id == bot_id, CloneUser.user_id == cq.from_user.id)
            )
            clone_user = result.scalar_one_or_none()
            if clone_user:
                clone_user.verified = True
            await session.commit()

        await _log_event(
            client,
            bot_row,
            f"✅ User verified\nID: {cq.from_user.id}\nTime: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        )

        await cq.message.edit_text(f"Welcome to {bot_row.bot_name or bot_row.bot_username}\n\nHere are your files.")
        await _deliver(client, cq.message.chat.id, bot_id, payload, settings_row.protect_content if settings_row else False)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^noop$"))
    async def noop_cb(client: Client, cq: CallbackQuery) -> None:
        await cq.answer()

    # --------------------------------------------------------------- /owner
    @app.on_message(filters.command("owner") & filters.private)
    async def owner_cmd(client: Client, message: Message) -> None:
        if not await _is_owner(bot_id, message.from_user.id):
            return
        await message.reply_text("**Owner Panel**", reply_markup=owner_panel_kb())

    for cmd, action in (
        ("stats", "own:stats"),
        ("files", "own:files"),
        ("channels", "own:channels"),
        ("settings", "own:settings"),
        ("logs", "own:logs"),
        ("backup", "own:backup"),
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
            await message.reply_text(f"{TXT_INFO} Send (or forward) the message you want to broadcast.")

    # ------------------------------------------------------- owner: routing
    async def _dispatch_owner_action(client: Client, user_id: int, target: Message, action: str) -> None:
        if action == "own:channels":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                channels = list(bot_row.channels)
            lines = ["**Force Subscribe Channels**", ""]
            rows = []
            if not channels:
                lines.append("No channels added yet.")
            for ch in channels:
                lines.append(f"• {ch.title or ch.username or ch.chat_id}")
                rows.append([btn(RED, f"Remove {ch.title or ch.username or ch.chat_id}", f"own:rmch:{ch.id}")])
            rows.append([btn(BLUE, "Add Channel", "own:addch")])
            rows.append([btn(YELLOW, "Back", "own:home")])
            await target.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

        elif action == "own:addch":
            clone_pending[(bot_id, user_id)] = PendingAction("await_channel")
            await target.reply_text(
                f"{TXT_INFO} Forward a message from the channel, or send its @username.\n"
                "Make sure this bot is an admin there first."
            )

        elif action == "own:upload":
            clone_pending[(bot_id, user_id)] = PendingAction("await_upload", {"count": 0})
            await target.reply_text(
                f"{TXT_INFO} Send the files you want to store — videos, documents, photos, audio, anything.\n"
                "Press Done when finished.",
                reply_markup=InlineKeyboardMarkup([[btn(RED, "Done", "own:upload_done")]]),
            )

        elif action == "own:upload_done":
            clone_pending.pop((bot_id, user_id), None)
            await target.reply_text(f"{TXT_INFO} Upload finished.", reply_markup=owner_panel_kb())

        elif action == "own:broadcast":
            clone_pending[(bot_id, user_id)] = PendingAction("await_broadcast")
            await target.reply_text(f"{TXT_INFO} Send (or forward) the message you want to broadcast to all users.")

        elif action == "own:files":
            async with AsyncSessionLocal() as session:
                counts = await session.execute(
                    select(UploadedFile.type, func.count()).where(UploadedFile.bot_id == bot_id).group_by(UploadedFile.type)
                )
                counts = dict(counts.all())
                total = sum(counts.values())
            lines = [f"**File Manager** — {total} files", ""]
            for kind, n in counts.items():
                lines.append(f"• {kind.title()}: {n}")
            await target.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [btn(RED, "Delete All Files", "own:delall")],
                        [btn(YELLOW, "Back", "own:home")],
                    ]
                ),
            )

        elif action == "own:delall":
            await target.reply_text(
                f"{TXT_ERR} Delete ALL stored files? This cannot be undone.",
                reply_markup=yes_no_kb("own:delall_yes", "own:files"),
            )

        elif action == "own:delall_yes":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                for f in list(bot_row.files):
                    await session.delete(f)
                await session.commit()
            await _record_owner_action(bot_id, "Deleted all files")
            await target.reply_text(f"{TXT_ERR} All files deleted.", reply_markup=owner_panel_kb())

        elif action == "own:settings":
            await _render_settings(target)

        elif action.startswith("own:toggle:"):
            field = action.split(":")[2]
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                s = bot_row.settings
                setattr(s, field, not getattr(s, field))
                await session.commit()
            await _record_owner_action(bot_id, f"Toggled {field}")
            await _render_settings(target, edit=True)

        elif action == "own:stats":
            async with AsyncSessionLocal() as session:
                total_users = await session.scalar(select(func.count()).select_from(CloneUser).where(CloneUser.bot_id == bot_id))
                verified = await session.scalar(
                    select(func.count()).select_from(CloneUser).where(CloneUser.bot_id == bot_id, CloneUser.verified.is_(True))
                )
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                today_users = await session.scalar(
                    select(func.count()).select_from(CloneUser).where(CloneUser.bot_id == bot_id, CloneUser.joined_at >= today_start)
                )
                yesterday_start = today_start - timedelta(days=1)
                yesterday_users = await session.scalar(
                    select(func.count())
                    .select_from(CloneUser)
                    .where(CloneUser.bot_id == bot_id, CloneUser.joined_at >= yesterday_start, CloneUser.joined_at < today_start)
                )
                total_files = await session.scalar(select(func.count()).select_from(UploadedFile).where(UploadedFile.bot_id == bot_id))
            text = (
                "**Statistics**\n\n"
                f"Total Users: {total_users}\n"
                f"Today: {today_users}\n"
                f"Yesterday: {yesterday_users}\n"
                f"Verified: {verified}\n"
                f"Pending: {total_users - verified}\n"
                f"Total Files: {total_files}"
            )
            await target.reply_text(text, reply_markup=InlineKeyboardMarkup([[btn(YELLOW, "Back", "own:home")]]))

        elif action == "own:logs":
            clone_pending[(bot_id, user_id)] = PendingAction("await_log_channel")
            await target.reply_text(
                f"{TXT_INFO} Forward a message from the channel you want to use for logs (make the bot an admin there)."
            )

        elif action == "own:backup":
            async with AsyncSessionLocal() as session:
                bot_row = await _get_bot(session, bot_id)
                payload = {
                    "bot_username": bot_row.bot_username,
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
                    "settings": {
                        "auto_delete": bot_row.settings.auto_delete if bot_row.settings else 0,
                        "protect_content": bot_row.settings.protect_content if bot_row.settings else False,
                        "force_subscribe": bot_row.settings.force_subscribe if bot_row.settings else True,
                    },
                }
            buf = io.BytesIO(json.dumps(payload, indent=2, default=str).encode("utf-8"))
            buf.name = f"nexora_backup_bot_{bot_id}.json"
            await client.send_document(target.chat.id, buf, caption=f"{TXT_INFO} Backup exported.")

        elif action == "own:close":
            await target.reply_text("Closed.")

        elif action == "own:home":
            await target.reply_text("**Owner Panel**", reply_markup=owner_panel_kb())

    async def _render_settings(target: Message, edit: bool = False) -> None:
        async with AsyncSessionLocal() as session:
            bot_row = await _get_bot(session, bot_id)
            s = bot_row.settings

        def dot(v: bool) -> str:
            return BLUE if v else RED

        rows = [
            [btn(dot(s.force_subscribe), f"Force Subscribe: {'ON' if s.force_subscribe else 'OFF'}", "own:toggle:force_subscribe")],
            [btn(dot(s.protect_content), f"Protect Content: {'ON' if s.protect_content else 'OFF'}", "own:toggle:protect_content")],
            [btn(dot(s.welcome_enabled), f"Welcome Msg: {'ON' if s.welcome_enabled else 'OFF'}", "own:toggle:welcome_enabled")],
            [btn(dot(s.send_files_once), f"Send Once: {'ON' if s.send_files_once else 'OFF'}", "own:toggle:send_files_once")],
            [btn(YELLOW, "Back", "own:home")],
        ]
        text = "**Settings**\n\nTap to toggle."
        markup = InlineKeyboardMarkup(rows)
        if edit:
            try:
                await target.edit_text(text, reply_markup=markup)
                return
            except RPCError:
                pass
        await target.reply_text(text, reply_markup=markup)

    @app.on_callback_query(filters.regex(r"^own:"))
    async def owner_callback(client: Client, cq: CallbackQuery) -> None:
        if not await _is_owner(bot_id, cq.from_user.id):
            await cq.answer("Owner only.", show_alert=True)
            return
        await _dispatch_owner_action(client, cq.from_user.id, cq.message, cq.data)
        await cq.answer()

    # ------------------------------------------------------- owner: intake
    @app.on_message(filters.private & (filters.text | MEDIA_FILTER) & ~filters.command([
        "start", "owner", "stats", "files", "channels", "settings", "logs", "backup", "broadcast",
    ]))
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
            session.add(
                BotChannel(
                    bot_id=bot_id,
                    chat_id=chat.id,
                    username=getattr(chat, "username", None),
                    title=getattr(chat, "title", None),
                    type="group" if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) else "channel",
                )
            )
            await session.commit()
        clone_pending.pop((bot_id, message.from_user.id), None)
        await _record_owner_action(bot_id, f"Added channel {getattr(chat, 'title', chat.id)}")
        await message.reply_text(
            f"{TXT_INFO} Saved. Add another channel or press Done.",
            reply_markup=InlineKeyboardMarkup(
                [[btn(BLUE, "Add Another", "own:addch"), btn(RED, "Done", "own:channels")]]
            ),
        )

    async def _handle_upload(message: Message, pending: PendingAction) -> None:
        kind, file_id, file_unique_id, size = _media_kind(message)
        async with AsyncSessionLocal() as session:
            session.add(
                UploadedFile(
                    bot_id=bot_id,
                    file_unique_id=file_unique_id,
                    file_id=file_id,
                    type=kind,
                    caption=message.caption,
                    size=size,
                    position=pending.data.get("count", 0),
                )
            )
            await session.commit()
        pending.data["count"] = pending.data.get("count", 0) + 1
        if pending.data["count"] % 20 == 0:
            await message.reply_text(f"{TXT_INFO} {pending.data['count']} files stored so far...")

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
            bot_row = await _get_bot(session, bot_id)
            bot_row.log_channel = chat.id
            await session.commit()
        clone_pending.pop((bot_id, message.from_user.id), None)
        await _record_owner_action(bot_id, f"Set log channel to {chat.title}")
        await message.reply_text(f"{TXT_INFO} Log channel set to {chat.title}.", reply_markup=owner_panel_kb())

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
            "Broadcast Started...\n\n░░░░░░░░░░",
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
                try:
                    await progress.edit_text(f"Broadcast Started...\n\n{bar}")
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
            f"Broadcast Started...\n\n██████████\n\nDone.\n\n"
            f"Success: {success}\nFailed: {failed}\nBlocked: {blocked}"
        )
        await _record_owner_action(bot_id, f"Broadcast completed (success={success}, failed={failed}, blocked={blocked})")
        await _log_event(
            client,
            bot_row,
            f"📣 Broadcast completed\nSuccess: {success}\nFailed: {failed}\nBlocked: {blocked}",
        )
