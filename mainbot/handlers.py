"""Nexora File Store — main bot: /start /help /newbot /mybots /rmbot /support."""
from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.errors import RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import select

from bot_manager import manager
from clonebot.handlers import register_clone_handlers
from config import settings
from database.engine import AsyncSessionLocal
from database.models import Bot as BotModel
from database.models import BotSettings, Owner
from keyboards import (
    BLUE,
    EMOJI_GUARD,
    EMOJI_PHONE,
    EMOJI_SPARKLE,
    EMOJI_STOP,
    GREEN,
    RED,
    SUPPORT_URL,
    TXT_ERR,
    TXT_INFO,
    TXT_OK,
    TXT_WARN,
    YELLOW,
    back_kb,
    btn,
    main_menu_kb,
    quote,
    yes_no_kb,
)
from utils.state import PendingAction, main_pending

log = logging.getLogger("nexora.mainbot")

STATS_IMAGE_URL = "https://graph.org/file/4e9cfe6722a743d0a791e-010fd8c5e3567948b8.jpg"

WELCOME_TEXT = (
    "**Nexora File Store**\n\n"
    "Create your own Telegram File Store Bot.\n\n"
    "No coding required.\n\n"
    "• Unlimited Files\n"
    "• Force Subscribe\n"
    "• Broadcast\n"
    "• Logs\n"
    "• Statistics\n"
    "• Owner Panel"
)

HELP_TEXT = (
    "**How it works**\n\n"
    "**Step 1**\n"
    "Talk to @BotFather, create a bot, and copy its token.\n\n"
    "**Step 2**\n"
    "Come back here, use /newbot, and paste the token.\n\n"
    "Done — your own file store bot is live."
)

SUPPORT_TEXT = f"Need help? Reach out to Nexora Support:\n\n{SUPPORT_URL}"


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
    @app.on_message(filters.command("start") & filters.private)
    async def start_cmd(client: Client, message: Message) -> None:
        main_pending.pop(message.from_user.id, None)
        await message.reply_text(
            WELCOME_TEXT,
            reply_markup=main_menu_kb(),
            reply_parameters=quote(message.id),
        )

    @app.on_message(filters.command("help") & filters.private)
    async def help_cmd(client: Client, message: Message) -> None:
        await message.reply_text(HELP_TEXT, reply_markup=back_kb())

    @app.on_message(filters.command("support") & filters.private)
    async def support_cmd(client: Client, message: Message) -> None:
        await message.reply_text(SUPPORT_TEXT, reply_markup=back_kb())

    @app.on_message(filters.command("newbot") & filters.private)
    async def newbot_cmd(client: Client, message: Message) -> None:
        main_pending[message.from_user.id] = PendingAction("await_token")
        await message.reply_text(
            f"{TXT_INFO} Send me the bot token you copied from @BotFather.",
            reply_markup=back_kb(),
        )

    @app.on_message(filters.command("mybots") & filters.private)
    async def mybots_cmd(client: Client, message: Message) -> None:
        await _send_mybots(client, message.from_user.id, message)

    @app.on_message(filters.command("rmbot") & filters.private)
    async def rmbot_cmd(client: Client, message: Message) -> None:
        await _send_rmbot_list(client, message.from_user.id, message)

    async def _send_mybots(client: Client, user_id: int, target: Message) -> None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(BotModel).join(Owner).where(Owner.telegram_id == user_id)
            )
            bots = result.scalars().all()

        if not bots:
            await target.reply_text(
                f"{TXT_WARN} You haven't created any bots yet. Use /newbot to create your first one.",
                reply_markup=back_kb(),
            )
            return

        rows = []
        for b in bots:
            label = f"@{b.bot_username}" if b.bot_username else (b.bot_name or f"Bot #{b.id}")
            rows.append([btn(BLUE, f"Open {label}", f"openpanel:{b.id}")])
        rows.append([btn(RED, "Back", "home")])
        await target.reply_text("**Your Bots**", reply_markup=InlineKeyboardMarkup(rows))

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
            rows.append([btn(RED, f"Delete {label}", f"rmbot:{b.id}")])
        rows.append([btn(BLUE, "Back", "home")])
        await target.reply_text("**Select a bot to remove**", reply_markup=InlineKeyboardMarkup(rows))

    @app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "newbot", "mybots", "rmbot", "support"]))
    async def text_router(client: Client, message: Message) -> None:
        pending = main_pending.get(message.from_user.id)
        if not pending:
            return

        if pending.action == "await_token":
            await _handle_new_token(client, message)

    async def _handle_new_token(client: Client, message: Message) -> None:
        token = message.text.strip()
        if ":" not in token or len(token.split(":")[0]) < 5:
            await message.reply_text(
                f"{TXT_ERR} That doesn't look like a valid bot token. Send the token @BotFather gave you, "
                "or press Back to cancel.",
                reply_markup=back_kb(),
            )
            return

        status_msg = await message.reply_text(f"{TXT_WARN} Checking token...")

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
            await probe.stop()
            await status_msg.edit_text(
                f"{TXT_ERR} Telegram rejected that token. Double check it and try again, or press Back.",
                reply_markup=back_kb(),
            )
            return
        await probe.stop()

        async with AsyncSessionLocal() as session:
            existing = await session.execute(select(BotModel).where(BotModel.bot_token == token))
            if existing.scalar_one_or_none() is not None:
                await status_msg.edit_text(
                    f"{TXT_ERR} This bot is already registered with Nexora.", reply_markup=back_kb()
                )
                return

            owner = await _get_or_create_owner(session, message.from_user)
            bot_row = BotModel(
                owner_id=owner.id,
                bot_token=token,
                bot_username=me.username,
                bot_name=me.first_name,
                welcome_caption=f"Welcome to {me.first_name}",
            )
            session.add(bot_row)
            await session.flush()
            session.add(BotSettings(bot_id=bot_row.id))
            await session.commit()
            bot_id = bot_row.id
            bot_username = me.username

        main_pending.pop(message.from_user.id, None)

        try:
            await manager.start_clone(bot_id, token, register_clone_handlers)
        except Exception:
            log.exception("Failed to start clone bot %s", bot_id)
            await status_msg.edit_text(
                f"{TXT_ERR} Bot saved but failed to start. Try /mybots later, or contact support.",
                reply_markup=back_kb(),
            )
            return

        await _log_main(
            client,
            f"➕ New clone bot created\nOwner: {message.from_user.id} (@{message.from_user.username})\n"
            f"Bot: @{bot_username} (id {bot_id})",
        )

        await status_msg.edit_text(
            "**Bot Created Successfully**\n\nNow finish setup.\n\n"
            "**⚠ Setup Required**\n"
            "1. Add Force Subscribe channels\n"
            "2. Add folder invite links (optional)\n"
            "3. Make your clone bot an admin in those channels\n"
            "4. Upload files\n"
            "5. Start receiving users\n\n"
            f"Open @{bot_username} and send `/owner` to configure it.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [btn(BLUE, "Open Bot", url=f"https://t.me/{bot_username}")],
                    [btn(YELLOW, "My Bots", "mybots")],
                ]
            ),
        )

    @app.on_callback_query()
    async def callback_router(client: Client, cq: CallbackQuery) -> None:
        data = cq.data or ""
        user_id = cq.from_user.id

        if data == "home":
            main_pending.pop(user_id, None)
            await cq.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_kb())
        elif data == "help":
            await cq.message.edit_text(HELP_TEXT, reply_markup=back_kb())
        elif data == "support":
            await cq.message.edit_text(SUPPORT_TEXT, reply_markup=back_kb())
        elif data == "newbot":
            main_pending[user_id] = PendingAction("await_token")
            await cq.message.edit_text(
                f"{TXT_INFO} Send me the bot token you copied from @BotFather.", reply_markup=back_kb()
            )
        elif data == "mybots":
            await _send_mybots(client, user_id, cq.message)
        elif data.startswith("openpanel:"):
            bot_id = int(data.split(":")[1])
            async with AsyncSessionLocal() as session:
                bot_row = await session.get(BotModel, bot_id)
            if bot_row is None:
                await cq.answer("Bot not found", show_alert=True)
                return
            await cq.message.edit_text(
                f"@{bot_row.bot_username}\n\nOpen the bot and send `/owner` to manage it.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [btn(BLUE, "Open Bot", url=f"https://t.me/{bot_row.bot_username}")],
                        [btn(RED, "Delete this bot", f"rmbot:{bot_row.id}")],
                        [btn(YELLOW, "Back", "mybots")],
                    ]
                ),
            )
        elif data.startswith("rmbot:"):
            bot_id = int(data.split(":")[1])
            async with AsyncSessionLocal() as session:
                bot_row = await session.get(BotModel, bot_id)
            if bot_row is None:
                await cq.answer("Bot not found", show_alert=True)
                return
            label = f"@{bot_row.bot_username}" if bot_row.bot_username else f"Bot #{bot_row.id}"
            await cq.message.edit_text(
                f"Delete **{label}**? This removes all of its files, channels and users.",
                reply_markup=yes_no_kb(f"rmbot_yes:{bot_id}", "mybots"),
            )
        elif data.startswith("rmbot_yes:"):
            bot_id = int(data.split(":")[1])
            async with AsyncSessionLocal() as session:
                bot_row = await session.get(BotModel, bot_id)
                if bot_row is None:
                    await cq.answer("Already deleted", show_alert=True)
                    return
                username = bot_row.bot_username
                await session.delete(bot_row)
                await session.commit()
            await manager.stop_clone(bot_id)
            await _log_main(client, f"🗑 Clone bot deleted: @{username} (id {bot_id})")
            await cq.message.edit_text(f"{TXT_ERR} Deleted.", reply_markup=back_kb())
        elif data == "rmbot_list":
            await _send_rmbot_list(client, user_id, cq.message)

        await cq.answer()
