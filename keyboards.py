"""Shared keyboard + message helpers.

Telegram Bot API 9.4 added real button colours: `style` on
InlineKeyboardButton/KeyboardButton ("primary"/"success"/"danger", default
otherwise) plus `icon_custom_emoji_id` for a leading premium-emoji icon. On
the MTProto side (what Kurigram actually speaks) this is
`keyboardButtonStyle` with `bg_primary` / `bg_success` / `bg_danger` /
`icon` flags. We use the real feature — not an emoji-dot workaround — and
fall back gracefully only if the installed Kurigram build predates it.
"""
from __future__ import annotations

import inspect

from pyrogram import enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

_BTN_PARAMS = inspect.signature(InlineKeyboardButton.__init__).parameters
SUPPORTS_BUTTON_STYLE = "style" in _BTN_PARAMS and "icon_custom_emoji_id" in _BTN_PARAMS

if SUPPORTS_BUTTON_STYLE:
    PRIMARY = enums.ButtonStyle.PRIMARY
    SUCCESS = enums.ButtonStyle.SUCCESS
    DANGER = enums.ButtonStyle.DANGER
    DEFAULT = enums.ButtonStyle.DEFAULT
else:
    # Fallback for a Kurigram build that predates Bot API 9.4 button
    # styling — emulate colour with a leading dot emoji instead of a real
    # button background.
    PRIMARY = "🔵"
    SUCCESS = "🟢"
    DANGER = "🔴"
    DEFAULT = "🟡"

# Backwards-compatible names used throughout the handlers.
BLUE = PRIMARY
GREEN = SUCCESS
RED = DANGER
YELLOW = DEFAULT

# Premium custom emoji ids (owner-supplied). `icon_custom_emoji_id` requires
# either the bot to have a Fragment username or the recipient to have
# Telegram Premium — a Telegram-side restriction, not a Kurigram one; on
# accounts without Premium these just render as the plain button text.
EMOJI_DEVIL = "5332696592317160796"      # 😈 playful gatekeeper (force-subscribe)
EMOJI_STOP = "5283283384418707920"       # ⛔️ deny / remove / reject
EMOJI_PHONE = "5318779098686826724"      # 📞 support / contact
EMOJI_GUARD = "6309677544782174678"      # 💂 owner / admin / channels guard
EMOJI_SIREN = "6267144651153609853"      # 🚨 logs / alerts
EMOJI_TOOLS = "5462921117423384478"      # 🛠 settings
EMOJI_OCTAGON = "6271674836628541366"    # 🛑 close / stop
EMOJI_FLAG_IN = "6138853050309150669"    # 🇮🇳 branding flourish
EMOJI_MIC = "5258500422393415126"        # 🎤 broadcast / announce
EMOJI_SPARKLE = "5222108309795908493"    # ✨ create / highlight / stats

def remoji(fallback: str, custom_id: str | None = None) -> str:
    """
    Returns a normal emoji.

    Telegram only supports custom emoji inside message entities,
    not plain strings, so this simply returns the fallback emoji.
    """
    return fallback

def btn(
    style,
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    icon: str | None = None,
) -> InlineKeyboardButton:
    if SUPPORTS_BUTTON_STYLE:
        kwargs = {"style": style}
        if icon:
            kwargs["icon_custom_emoji_id"] = icon
    else:
        # `style` holds a plain emoji-dot string in the fallback path.
        text = f"{style} {text}"
        kwargs = {}
    if url:
        return InlineKeyboardButton(text, url=url, **kwargs)
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def quote(message_id: int, text: str | None = None) -> ReplyParameters:
    """Bot API 7.0+ quoted reply — pins the reply to (and optionally
    highlights a snippet of) a specific earlier message."""
    if text:
        return ReplyParameters(message_id=message_id, quote=text[:1024])
    return ReplyParameters(message_id=message_id)


# Plain-text status markers for message bodies (not buttons) — kept as
# literal emoji regardless of SUPPORTS_BUTTON_STYLE, since message text has
# no `style` field to attach to.
TXT_INFO = "🔵"
TXT_WARN = "🟡"
TXT_ERR = "🔴"
TXT_OK = "🟢"

SUPPORT_USERNAME = "NexoraaBotss"
SUPPORT_URL = f"https://t.me/{SUPPORT_USERNAME}"


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn(PRIMARY, "Create Bot", "newbot", icon=EMOJI_SPARKLE)],
            [btn(DEFAULT, "My Bots", "mybots", icon=EMOJI_GUARD), btn(DEFAULT, "Help", "help")],
            [btn(DEFAULT, "Platform Stats", "stats", icon=EMOJI_SPARKLE)],
            [btn(PRIMARY, "Support", url=SUPPORT_URL, icon=EMOJI_PHONE)],
        ]
    )


def back_kb(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(DANGER, "Back", callback_data, icon=EMOJI_OCTAGON)]])


def yes_no_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[btn(SUCCESS, "YES", yes_cb), btn(DANGER, "NO", no_cb, icon=EMOJI_STOP)]]
    )
