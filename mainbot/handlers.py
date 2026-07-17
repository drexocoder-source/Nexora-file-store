"""Nexora File Store — main bot handlers.

Commands: /start /help /newbot /mybots /rmbot /support /admin
"""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from bot_manager import manager
from clonebot.handlers import register_clone_handlers
from config import settings
from database.engine import AsyncSessionLocal
from database.models import Bot as BotModel
from database.models import BotSettings, CloneUser, Owner
from keyboards import (
    BLUE, DANGER, DEFAULT, GREEN, PRIMARY, RED, SUCCESS, YELLOW,
    EMOJI_BELL, EMOJI_CHART, EMOJI_CHECK, EMOJI_CROWN, EMOJI_DEVIL,
    EMOJI_FIRE, EMOJI_FLAG_IN, EMOJI_FOLDER, EMOJI_GLOBE, EMOJI_GUARD,
    EMOJI_LINK, EMOJI_MIC, EMOJI_OCTAGON, EMOJI_PHONE, EMOJI_SIREN,
    EMOJI_SPARKLE, EMOJI_STOP, EMOJI_TOOLS, EMOJI_TRASH,
    IMG_ADMIN, IMG_CLONE, IMG_WELCOME,
    SUPPORT_URL,
    TXT_ERR, TXT_INFO, TXT_OK, TXT_WARN,
    admin_menu_kb, back_kb, btn, main_menu_kb, quote,
    remoji, template_kb, yes_no_kb,
)
from utils.state import PendingAction, main_pending

log = logging.getLogger("nexora.mainbot")

# ── Text constants ────────────────────────────────────────────────────────────
WELCOME_TEXT = (
    "🇮🇳 **Nexora File Store**\n\n"
    "Create your own Telegram File Store Bot.\n"
    "No coding. No limits.\n\n"
    "📁 Unlimited Files\n"
    "😈 Force Subscribe\n"
    "🎤 Broadcast\n"
    "🚨 Live Logs\n"
    "📊 Statistics\n"
    "💂 Owner Panel\n"
    "🔗 Link Protect\n"
    "👑 Superadmin Access"
)

HELP_TEXT = (
    "🛠 **How it works**\n\n"
    "**Step 1**\n"
    "Talk to @BotFather, create a bot, copy its token.\n\n"
    "**Step 2**\n"
    "Use /newbot here and paste the token.\n\n"
    "**Step 3**\n"
    "Pick a template:\n"
    "• 📁 **File Store** — store & share files\n"
    "• 🔗 **Link Protect** — protect links behind a gate\n\n"
    "Done — your bot is live instantly."
)

SUPPORT_TEXT = f"📞 Need help? Reach out to Nexora Support:\n\n{SUPPORT_URL}"


def _is_main_owner(user_id: int) -> bool:
    return user_id == settings.main_owner_id


async def _log_main(client: Client, text: str) -> None:
    if not settings.main_log_channel_id:
        return
    try:
        await client.send_message(settings.main_log_channel_id, text)
    except RPCError:
        log.exception("Failed to write to main log channel")


async def _get_or_create_owner(session, user) -> Owner:
    result = await session.execute(select(Owner).where(Owner.telegram_id == user.id))
    owner = result.scalar_one_or_none()
    if owner is None:
        owner = Owner(telegram_id=user.id, username=user.username, first_name=user.first_name)
        session.add(owner)
        await session.flush()
    return owner


def register_main_handlers(app: Client) -> None:

    # ── /start ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(client: Client, message: Message) -> None:
        main_pending.pop(message.from_user.id, None)
        try:
            await message.reply_photo(
                IMG_WELCOME,
                caption=WELCOME_TEXT,
                reply_markup=main_menu_kb(),
            )
        except RPCError:
            await message.reply_text(WELCOME_TEXT, reply_markup=main_menu_kb())

    # ── /help ─────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("help") & filters.private)
    async def help_cmd(client: Client, message: Message) -> None:
        await message.reply_text(HELP_TEXT, reply_markup=back_kb())

    # ── /support ──────────────────────────────────────────────────────────────
    @app.on_message(filters.command("support") & filters.private)
    async def support_cmd(client: Client, message: Message) -> None:
        await message.reply_text(SUPPORT_TEXT, reply_markup=back_kb())

    # ── /newbot ───────────────────────────────────────────────────────────────
    @app.on_message(filters.command("newbot") & filters.private)
    async def newbot_cmd(client: Client, message: Message) -> None:
        main_pending[message.from_user.id] = PendingAction("await_token")
        await message.reply_text(
            f"{TXT_INFO} ✨ Send me the **bot token** you copied from @BotFather.",
            reply_markup=back_kb(),
        )

    # ── /mybots ───────────────────────────────────────────────────────────────
    @app.on_message(filters.command("mybots") & filters.private)
    async def mybots_cmd(client: Client, message: Message) -> None:
        await _send_mybots(client, message.from_user.id, message)

    # ── /rmbot ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("rmbot") & filters.private)
    async def rmbot_cmd(client: Client, message: Message) -> None:
        await _send_rmbot_list(client, message.from_user.id, message)

    # ── /admin ────────────────────────────────────────────────────────────────
    @app.on_message(filters.command("admin") & filters.private)
    async def admin_cmd(client: Client, message: Message) -> None:
        if not _is_main_owner(message.from_user.id):
            return
        try:
            await message.reply_photo(
                IMG_ADMIN,
                caption="👑 **Nexora Superadmin Panel**\n\nFull platform access.",
                reply_markup=admin_menu_kb(),
            )
        except RPCError:
            await message.reply_text(
                "👑 **Nexora Superadmin Panel**\n\nFull platform access.",
                reply_markup=admin_menu_kb(),
            )

    # ── helpers ───────────────────────────────────────────────────────────────
    async def _send_mybots(client: Client, user_id: int, target: Message) -> None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotModel).join(Owner).where(Owner.telegram_id == user_id)
            )
            bots = result.scalars().all()

        if not bots:
            await target.reply_text(
                f"{TXT_WARN} You haven't created any bots yet. Use /newbot to get started.",
                reply_markup=back_kb(),
            )
            return

        rows = []
        for b in bots:
            label = f"@{b.bot_username}" if b.bot_username else (b.bot_name or f"Bot #{b.id}")
            icon_map = {"linkprotect": EMOJI_LINK, "filestore": EMOJI_FOLDER}
            icon = icon_map.get(b.bot_type or "filestore", EMOJI_FOLDER)
            type_tag = "🔗" if b.bot_type == "linkprotect" else "📁"
            rows.append([btn(SUCCESS, f"{type_tag} {label}", f"openpanel:{b.id}", icon=icon)])
        rows.append([btn(DANGER, "🔙 Back", "home", icon=EMOJI_OCTAGON)])
        await target.reply_text(
            f"💂 **Your Bots** ({len(bots)} total)", reply_markup=InlineKeyboardMarkup(rows)
        )

    async def _send_rmbot_list(client: Client, user_id: int, target: Message) -> None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotModel).join(Owner).where(Owner.telegram_id == user_id)
            )
            bots = result.scalars().all()

        if not bots:
            await target.reply_text(f"{TXT_WARN} You have no bots to remove.", reply_markup=back_kb())
            return

        rows = []
        for b in bots:
            label = f"@{b.bot_username}" if b.bot_username else (b.bot_name or f"Bot #{b.id}")
            rows.append([btn(DANGER, f"🗑 Delete {label}", f"rmbot:{b.id}", icon=EMOJI_TRASH)])
        rows.append([btn(PRIMARY, "🔙 Back", "home", icon=EMOJI_OCTAGON)])
        await target.reply_text(
            "⛔ **Select a bot to remove**", reply_markup=InlineKeyboardMarkup(rows)
        )

    # ── text router ───────────────────────────────────────────────────────────
    @app.on_message(
        filters.private & filters.text
        & ~filters.command(["start", "help", "newbot", "mybots", "rmbot", "support", "admin"])
    )
    async def text_router(client: Client, message: Message) -> None:
        pending = main_pending.get(message.from_user.id)
        if not pending:
            return
        if pending.action == "await_token":
            await _handle_new_token(client, message)
        elif pending.action == "await_admin_broadcast":
            await _handle_admin_broadcast(client, message)

    async def _handle_new_token(client: Client, message: Message) -> None:
        token = message.text.strip()
        if ":" not in token or len(token.split(":")[0]) < 5:
            await message.reply_text(
                f"{TXT_ERR} That doesn't look like a valid bot token. "
                "Send the token @BotFather gave you, or press Back.",
                reply_markup=back_kb(),
            )
            return

        status_msg = await message.reply_text(f"{TXT_WARN} 🛠 Checking token...")

        probe = Client(
            name=f"probe_{message.from_user.id}",
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            bot_token=token,
            in_memory=True,
        )
        try:
            await probe.start()
            me = await probe.get_me()
        except Exception:
            try:
                await probe.stop()
            except Exception:
                pass
            await status_msg.edit_text(
                f"{TXT_ERR} Telegram rejected that token. Double-check it and try again.",
                reply_markup=back_kb(),
            )
            return
        await probe.stop()

        async with AsyncSessionLocal() as session:
            existing = await session.execute(select(BotModel).where(BotModel.bot_token == token))
            if existing.scalar_one_or_none() is not None:
                await status_msg.edit_text(
                    f"{TXT_ERR} This bot is already registered with Nexora.",
                    reply_markup=back_kb(),
                )
                return

        # Store token + bot info in pending, then ask for template
        main_pending[message.from_user.id] = PendingAction("await_template", {
            "token": token,
            "username": me.username,
            "name": me.first_name,
        })

        await status_msg.edit_text(
            f"✅ **Bot verified:** @{me.username}\n\n"
            "🎨 **Choose a template** for this bot:\n\n"
            "📁 **File Store** — store & share files with users\n"
            "🔗 **Link Protect** — protect URLs behind force-subscribe gate",
            reply_markup=template_kb(),
        )

    async def _create_bot(client: Client, user_id: int, token: str, bot_username: str,
                          bot_name: str, bot_type: str, status_msg: Message) -> None:
        """Create the bot record, start the clone, and report back."""
        async with AsyncSessionLocal() as session:
            # re-check for duplicates (race)
            existing = await session.execute(select(BotModel).where(BotModel.bot_token == token))
            if existing.scalar_one_or_none() is not None:
                await status_msg.edit_text(
                    f"{TXT_ERR} This bot is already registered.", reply_markup=back_kb()
                )
                return

            from pyrogram.types import User as TgUser
            # get the real User object for owner creation
            class _FakeUser:
                id = user_id
                username = None
                first_name = None

            async with AsyncSessionLocal() as s2:
                res = await s2.execute(select(Owner).where(Owner.telegram_id == user_id))
                owner = res.scalar_one_or_none()
                if owner is None:
                    owner = Owner(telegram_id=user_id)
                    s2.add(owner)
                    await s2.flush()
                owner_db_id = owner.id
                await s2.commit()

            bot_row = BotModel(
                owner_id=owner_db_id,
                bot_token=token,
                bot_username=bot_username,
                bot_name=bot_name,
                bot_type=bot_type,
                welcome_caption=f"Welcome to {bot_name}",
            )
            session.add(bot_row)
            await session.flush()
            session.add(BotSettings(bot_id=bot_row.id))
            await session.commit()
            bot_id = bot_row.id

        main_pending.pop(user_id, None)

        try:
            await manager.start_clone(bot_id, token, register_clone_handlers)
        except Exception:
            log.exception("Failed to start clone bot %s", bot_id)
            await status_msg.edit_text(
                f"{TXT_ERR} Bot saved but failed to start. Try /mybots later.",
                reply_markup=back_kb(),
            )
            return

        await _log_main(
            client,
            f"🚨 ➕ New clone created\n"
            f"Type: {bot_type}\n"
            f"Owner: {user_id}\n"
            f"Bot: @{bot_username} (id {bot_id})",
        )

        type_label = "🔗 Link Protect" if bot_type == "linkprotect" else "📁 File Store"
        next_step = (
            "Send `/owner` in your bot to configure links."
            if bot_type == "linkprotect"
            else "Send `/owner` in your bot to upload files & set channels."
        )

        await status_msg.edit_text(
            f"✨ **Bot Created!** — {type_label}\n\n"
            f"🤖 @{bot_username} is now live.\n\n"
            f"**Next step:** {next_step}",
            reply_markup=InlineKeyboardMarkup([
                [btn(SUCCESS, "🚀 Open Bot", url=f"https://t.me/{bot_username}", icon=EMOJI_GUARD)],
                [btn(DEFAULT, "🤖 My Bots", "mybots", icon=EMOJI_TOOLS)],
            ]),
        )

    # ── callback router ───────────────────────────────────────────────────────
    @app.on_callback_query()
    async def callback_router(client: Client, cq: CallbackQuery) -> None:
        data = cq.data or ""
        user_id = cq.from_user.id

        # ── home ──
        if data == "home":
            main_pending.pop(user_id, None)
            try:
                await cq.message.edit_caption(WELCOME_TEXT, reply_markup=main_menu_kb())
            except RPCError:
                await cq.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())

        # ── help ──
        elif data == "help":
            try:
                await cq.message.edit_caption(HELP_TEXT, reply_markup=back_kb())
            except RPCError:
                await cq.message.edit_text(HELP_TEXT, reply_markup=back_kb())

        # ── support ──
        elif data == "support":
            try:
                await cq.message.edit_caption(SUPPORT_TEXT, reply_markup=back_kb())
            except RPCError:
                await cq.message.edit_text(SUPPORT_TEXT, reply_markup=back_kb())

        # ── newbot ──
        elif data == "newbot":
            main_pending[user_id] = PendingAction("await_token")
            try:
                await cq.message.edit_caption(
                    f"{TXT_INFO} ✨ Send me the **bot token** you copied from @BotFather.",
                    reply_markup=back_kb(),
                )
            except RPCError:
                await cq.message.edit_text(
                    f"{TXT_INFO} ✨ Send me the **bot token** you copied from @BotFather.",
                    reply_markup=back_kb(),
                )

        # ── template selection ──
        elif data.startswith("tpl:"):
            pending = main_pending.get(user_id)
            if not pending or pending.action != "await_template":
                await cq.answer("Session expired. Use /newbot again.", show_alert=True)
                return
            bot_type = data.split(":")[1]
            token = pending.data["token"]
            bot_username = pending.data["username"]
            bot_name = pending.data["name"]
            type_label = "🔗 Link Protect" if bot_type == "linkprotect" else "📁 File Store"
            try:
                await cq.message.edit_text(f"🛠 Creating **{type_label}** bot…")
            except RPCError:
                pass
            await _create_bot(client, user_id, token, bot_username, bot_name, bot_type, cq.message)

        # ── mybots ──
        elif data == "mybots":
            await _send_mybots(client, user_id, cq.message)

        # ── platform stats ──
        elif data == "stats":
            async with AsyncSessionLocal() as session:
                total_owners = await session.scalar(select(func.count()).select_from(Owner))
                total_bots = await session.scalar(select(func.count()).select_from(BotModel))
                total_users = await session.scalar(select(func.count()).select_from(CloneUser))
                fs_bots = await session.scalar(
                    select(func.count()).select_from(BotModel).where(BotModel.bot_type == "filestore")
                )
                lp_bots = await session.scalar(
                    select(func.count()).select_from(BotModel).where(BotModel.bot_type == "linkprotect")
                )
            text = (
                "📊 **Platform Statistics**\n\n"
                f"👑 Owners: **{total_owners}**\n"
                f"🤖 Total Bots: **{total_bots}**\n"
                f"   📁 File Store: **{fs_bots}**\n"
                f"   🔗 Link Protect: **{lp_bots}**\n"
                f"👥 Total Users: **{total_users}**"
            )
            try:
                await cq.message.edit_text(text, reply_markup=back_kb())
            except RPCError:
                await cq.message.edit_caption(text, reply_markup=back_kb())

        # ── open panel ──
        elif data.startswith("openpanel:"):
            bot_id = int(data.split(":")[1])
            async with AsyncSessionLocal() as session:
                bot_row = await session.get(BotModel, bot_id)
            if bot_row is None:
                await cq.answer("Bot not found.", show_alert=True)
                return
            type_label = "🔗 Link Protect" if bot_row.bot_type == "linkprotect" else "📁 File Store"
            text = (
                f"💂 **@{bot_row.bot_username}**\n\n"
                f"Type: {type_label}\n\n"
                "Send `/owner` inside your bot to manage it."
            )
            try:
                await cq.message.edit_text(
                    text,
                    reply_markup=InlineKeyboardMarkup([
                        [btn(SUCCESS, "🚀 Open Bot", url=f"https://t.me/{bot_row.bot_username}", icon=EMOJI_GUARD)],
                        [btn(DANGER, "🗑 Delete this bot", f"rmbot:{bot_row.id}", icon=EMOJI_TRASH)],
                        [btn(DEFAULT, "🔙 Back", "mybots", icon=EMOJI_OCTAGON)],
                    ]),
                )
            except RPCError:
                await cq.message.edit_caption(text, reply_markup=InlineKeyboardMarkup([
                    [btn(SUCCESS, "🚀 Open Bot", url=f"https://t.me/{bot_row.bot_username}", icon=EMOJI_GUARD)],
                    [btn(DANGER, "🗑 Delete", f"rmbot:{bot_row.id}", icon=EMOJI_TRASH)],
                    [btn(DEFAULT, "🔙 Back", "mybots", icon=EMOJI_OCTAGON)],
                ]))

        # ── rmbot ──
        elif data.startswith("rmbot:"):
            bot_id = int(data.split(":")[1])
            async with AsyncSessionLocal() as session:
                bot_row = await session.get(BotModel, bot_id)
            if bot_row is None:
                await cq.answer("Bot not found.", show_alert=True)
                return
            label = f"@{bot_row.bot_username}" if bot_row.bot_username else f"Bot #{bot_row.id}"
            try:
                await cq.message.edit_text(
                    f"⛔ Delete **{label}**?\n\nThis removes all its files, links, channels and users.",
                    reply_markup=yes_no_kb(f"rmbot_yes:{bot_id}", "mybots"),
                )
            except RPCError:
                pass

        elif data.startswith("rmbot_yes:"):
            bot_id = int(data.split(":")[1])
            async with AsyncSessionLocal() as session:
                bot_row = await session.get(BotModel, bot_id)
                if bot_row is None:
                    await cq.answer("Already deleted.", show_alert=True)
                    return
                username = bot_row.bot_username
                await session.delete(bot_row)
                await session.commit()
            await manager.stop_clone(bot_id)
            await _log_main(client, f"🚨 🗑 Clone deleted: @{username} (id {bot_id})")
            try:
                await cq.message.edit_text(
                    f"✅ Deleted @{username}.", reply_markup=back_kb()
                )
            except RPCError:
                pass

        elif data == "rmbot_list":
            await _send_rmbot_list(client, user_id, cq.message)

        # ── superadmin panel ──
        elif data.startswith("adm:"):
            if not _is_main_owner(user_id):
                await cq.answer("Access denied.", show_alert=True)
                return
            await _handle_admin_callback(client, cq, data, user_id)

        await cq.answer()

    # ── superadmin callbacks ──────────────────────────────────────────────────
    async def _handle_admin_callback(client: Client, cq: CallbackQuery, data: str, user_id: int) -> None:
        if data == "adm:stats":
            async with AsyncSessionLocal() as session:
                total_owners = await session.scalar(select(func.count()).select_from(Owner))
                total_bots = await session.scalar(select(func.count()).select_from(BotModel))
                active_bots = await session.scalar(
                    select(func.count()).select_from(BotModel).where(BotModel.active.is_(True))
                )
                total_users = await session.scalar(select(func.count()).select_from(CloneUser))
                fs_bots = await session.scalar(
                    select(func.count()).select_from(BotModel).where(BotModel.bot_type == "filestore")
                )
                lp_bots = await session.scalar(
                    select(func.count()).select_from(BotModel).where(BotModel.bot_type == "linkprotect")
                )
            text = (
                "📊 **Platform Statistics — Superadmin View**\n\n"
                f"👑 Owners: **{total_owners}**\n"
                f"🤖 Total Bots: **{total_bots}** ({active_bots} active)\n"
                f"   📁 File Store: **{fs_bots}**\n"
                f"   🔗 Link Protect: **{lp_bots}**\n"
                f"👥 Total Users (all bots): **{total_users}**"
            )
            try:
                await cq.message.edit_text(
                    text,
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", "adm:home", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                pass

        elif data == "adm:home":
            try:
                await cq.message.edit_text(
                    "👑 **Nexora Superadmin Panel**\n\nFull platform access.",
                    reply_markup=admin_menu_kb(),
                )
            except RPCError:
                pass

        elif data == "adm:bots":
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotModel).order_by(BotModel.created_at.desc()).limit(20)
                )
                bots = result.scalars().all()
            if not bots:
                try:
                    await cq.message.edit_text("No bots yet.", reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", "adm:home")]]))
                except RPCError:
                    pass
                return
            lines = ["🤖 **All Bots** (latest 20)\n"]
            for b in bots:
                t = "🔗" if b.bot_type == "linkprotect" else "📁"
                lines.append(f"{t} @{b.bot_username or b.id} — owner_id:{b.owner_id}")
            try:
                await cq.message.edit_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", "adm:home", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                pass

        elif data == "adm:owners":
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Owner).order_by(Owner.created_at.desc()).limit(20)
                )
                owners = result.scalars().all()
            lines = ["👥 **All Owners** (latest 20)\n"]
            for o in owners:
                handle = f"@{o.username}" if o.username else f"id:{o.telegram_id}"
                lines.append(f"• {o.first_name or 'Unknown'} {handle}")
            try:
                await cq.message.edit_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", "adm:home", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                pass

        elif data == "adm:topbots":
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(BotModel.bot_username, func.count(CloneUser.id).label("cnt"))
                    .outerjoin(CloneUser, CloneUser.bot_id == BotModel.id)
                    .group_by(BotModel.id)
                    .order_by(func.count(CloneUser.id).desc())
                    .limit(10)
                )
                rows = result.all()
            lines = ["🔥 **Top Bots by Users**\n"]
            for i, (uname, cnt) in enumerate(rows, 1):
                lines.append(f"{i}. @{uname or '?'} — {cnt} users")
            try:
                await cq.message.edit_text(
                    "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", "adm:home", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                pass

        elif data == "adm:logs":
            async with AsyncSessionLocal() as session:
                from database.models import OwnerLog
                result = await session.execute(
                    select(OwnerLog).order_by(OwnerLog.time.desc()).limit(15)
                )
                logs_list = result.scalars().all()
            lines = ["📋 **Recent Owner Actions**\n"]
            for entry in logs_list:
                lines.append(f"• Bot {entry.bot_id}: {entry.action[:60]}")
            try:
                await cq.message.edit_text(
                    "\n".join(lines) if len(lines) > 1 else "No logs yet.",
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", "adm:home", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                pass

        elif data == "adm:broadcast":
            main_pending[user_id] = PendingAction("await_admin_broadcast")
            try:
                await cq.message.edit_text(
                    f"{TXT_INFO} 📣 Send the message you want to broadcast to **all users across all bots**.",
                    reply_markup=InlineKeyboardMarkup([[btn(DANGER, "🔙 Cancel", "adm:home", icon=EMOJI_OCTAGON)]]),
                )
            except RPCError:
                pass

    async def _handle_admin_broadcast(client: Client, message: Message) -> None:
        """Broadcast a message to every CloneUser across all bots."""
        main_pending.pop(message.from_user.id, None)
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(CloneUser.user_id).distinct())
            user_ids = [row[0] for row in result.all()]

        total = len(user_ids)
        progress = await message.reply_text(f"📣 Broadcasting to {total} users…\n\n░░░░░░░░░░")
        success = failed = blocked = 0

        from pyrogram.errors import UserIsBlocked

        for idx, uid in enumerate(user_ids, start=1):
            try:
                await message.copy(uid)
                success += 1
            except UserIsBlocked:
                blocked += 1
            except RPCError:
                failed += 1
            if idx % max(1, total // 10) == 0 or idx == total:
                filled = int((idx / total) * 10) if total else 10
                bar = "█" * filled + "░" * (10 - filled)
                try:
                    await progress.edit_text(f"📣 Broadcasting…\n\n{bar}\n{idx}/{total}")
                except RPCError:
                    pass

        await progress.edit_text(
            f"✅ **Broadcast Complete**\n\n"
            f"✔️ Success: **{success}**\n"
            f"❌ Failed: **{failed}**\n"
            f"🚫 Blocked: **{blocked}**"
        )
