"""Shared keyboard + message helpers for Nexora File Store."""
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
    PRIMARY = "💙"
    SUCCESS = "💚"
    DANGER = "❤️"
    DEFAULT = "💛"

BLUE   = PRIMARY
GREEN  = SUCCESS
RED    = DANGER
YELLOW = DEFAULT

# ── Premium custom emoji IDs ────────────────────────────────────────────────
EMOJI_DEVIL    = "5332696592317160796"   # 😈  force-subscribe gatekeeper
EMOJI_STOP     = "5283283384418707920"   # ⛔  deny / remove / reject
EMOJI_PHONE    = "5318779098686826724"   # 📞  support / contact
EMOJI_GUARD    = "6309677544782174678"   # 💂  owner / admin panel
EMOJI_SIREN    = "6267144651153609853"   # 🚨  logs / alerts
EMOJI_TOOLS    = "5462921117423384478"   # 🛠  settings
EMOJI_OCTAGON  = "6271674836628541366"   # 🛑  close / back
EMOJI_FLAG_IN  = "6138853050309150669"   # 🇮🇳  branding
EMOJI_MIC      = "5258500422393415126"   # 🎤  broadcast / announce
EMOJI_SPARKLE  = "5222108309795908493"   # ✨  create / highlight / stats
EMOJI_CROWN    = "5237699328843200968"   # 👑  main owner / superadmin
EMOJI_LINK     = "5215519737181096462"   # 🔗  link protect
EMOJI_LOCK     = "5373123633101267608"   # 🔒  protected content
EMOJI_CHART    = "5219978203285888739"   # 📊  statistics / charts
EMOJI_FIRE     = "5213452215527677338"   # 🔥  top / popular
EMOJI_GLOBE    = "5215854116564648552"   # 🌐  platform-wide
EMOJI_FOLDER   = "5218777718840180631"   # 📁  file store template
EMOJI_BELL     = "5215209307736895527"   # 🔔  notifications
EMOJI_TRASH    = "5215209357985613414"   # 🗑  delete
EMOJI_CHECK    = "5215209447052903498"   # ✅  verified / done
EMOJI_STAR     = "5222108309795908493"   # ⭐  featured


def remoji(fallback: str, custom_id: str | None = None) -> str:
    """Returns the fallback emoji (custom emoji requires message entities)."""
    return fallback


def btn(
    style,
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    icon: str | None = None,
) -> InlineKeyboardButton:
    if SUPPORTS_BUTTON_STYLE:
        kwargs: dict = {"style": style}
        if icon:
            kwargs["icon_custom_emoji_id"] = icon
    else:
        text = f"{style} {text}"
        kwargs = {}
    if url:
        return InlineKeyboardButton(text, url=url, **kwargs)
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def quote(message_id: int, text: str | None = None) -> ReplyParameters:
    if text:
        return ReplyParameters(message_id=message_id, quote=text[:1024])
    return ReplyParameters(message_id=message_id)


# ── Status markers ───────────────────────────────────────────────────────────
TXT_INFO = "🔵"
TXT_WARN = "🟡"
TXT_ERR  = "🔴"
TXT_OK   = "🟢"

SUPPORT_USERNAME = "NexoraaBotss"
SUPPORT_URL      = f"https://t.me/{SUPPORT_USERNAME}"

# ── Image URLs ───────────────────────────────────────────────────────────────
IMG_WELCOME = "https://graph.org/file/4e9cfe6722a743d0a791e-010fd8c5e3567948b8.jpg"
IMG_ADMIN   = "https://graph.org/file/e8087a3300ad254ff93d7-c9325cbef36a60e2b4.jpg"
IMG_CLONE   = "https://graph.org/file/874c7523cf9fb087baae4-787a191131ca5d0bb7.jpg"


# ── Keyboards ────────────────────────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(PRIMARY, "✨ Create Bot",      "newbot",  icon=EMOJI_SPARKLE)],
        [
            btn(DEFAULT, "🤖 My Bots",      "mybots",  icon=EMOJI_GUARD),
            btn(DEFAULT, "❓ Help",          "help"),
        ],
        [btn(DEFAULT, "📊 Platform Stats",  "stats",   icon=EMOJI_CHART)],
        [btn(PRIMARY, "📞 Support",         url=SUPPORT_URL, icon=EMOJI_PHONE)],
    ])


def admin_menu_kb() -> InlineKeyboardMarkup:
    """Superadmin panel keyboard — shown only to the main owner."""
    return InlineKeyboardMarkup([
        [
            btn(PRIMARY, "📊 Platform Stats", "adm:stats",     icon=EMOJI_CHART),
            btn(PRIMARY, "🤖 All Bots",        "adm:bots",      icon=EMOJI_GLOBE),
        ],
        [
            btn(SUCCESS, "📣 Broadcast All",   "adm:broadcast", icon=EMOJI_MIC),
            btn(DEFAULT, "👥 All Owners",       "adm:owners",    icon=EMOJI_GUARD),
        ],
        [
            btn(YELLOW,  "📋 Recent Logs",      "adm:logs",      icon=EMOJI_SIREN),
            btn(YELLOW,  "🔥 Top Bots",         "adm:topbots",   icon=EMOJI_FIRE),
        ],
        [btn(DANGER, "🔙 Back",               "home",          icon=EMOJI_OCTAGON)],
    ])


def back_kb(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(DANGER, "🔙 Back", callback_data, icon=EMOJI_OCTAGON)]])


def yes_no_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        btn(SUCCESS, "✅ YES", yes_cb, icon=EMOJI_CHECK),
        btn(DANGER,  "❌ NO",  no_cb,  icon=EMOJI_STOP),
    ]])


def template_kb() -> InlineKeyboardMarkup:
    """Shown after token validation — user picks bot type."""
    return InlineKeyboardMarkup([
        [btn(PRIMARY, "📁 File Store",    "tpl:filestore",     icon=EMOJI_FOLDER)],
        [btn(SUCCESS, "🔗 Link Protect",  "tpl:linkprotect",   icon=EMOJI_LINK)],
        [btn(DANGER,  "🔙 Cancel",        "home",              icon=EMOJI_OCTAGON)],
    ])
