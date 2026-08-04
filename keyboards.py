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
    DANGER  = enums.ButtonStyle.DANGER
    DEFAULT = enums.ButtonStyle.DEFAULT
else:
    PRIMARY = "💙"
    SUCCESS = "💚"
    DANGER  = "❤️"
    DEFAULT = "💛"

BLUE   = PRIMARY
GREEN  = SUCCESS
RED    = DANGER
YELLOW = DEFAULT

# ── Premium custom emoji IDs ─────────────────────────────────────────────────
EMOJI_DEVIL   = "5332696592317160796"   # 😈
EMOJI_STOP    = "5283283384418707920"   # ⛔
EMOJI_PHONE   = "5318779098686826724"   # 📞
EMOJI_SIREN   = "6267144651153609853"   # 🚨
EMOJI_TOOLS   = "5462921117423384478"   # 🛠
EMOJI_OCTAGON = "6271674836628541366"   # 🛑
EMOJI_FLAG_IN = "6138853050309150669"   # 🇮🇳
EMOJI_MIC     = "5258500422393415126"   # 🎤
EMOJI_SPARKLE = "5222108309795908493"   # ✨
EMOJI_WARN    = "6089000948292652337"   # ⚠️
EMOJI_MAIL    = "6089022934230240700"   # ✉️
EMOJI_BELL    = "6089229298818879530"   # 🔔
EMOJI_COOL    = "5431766464040283359"   # 😎
EMOJI_DEV     = "5319161050128459957"   # 👨‍💻
EMOJI_UNLOCK  = "5429405838345265327"   # 🔓
EMOJI_TROPHY  = "6266973397922616654"   # 🏆
EMOJI_STAR    = "6267219374994626613"   # 🌟
EMOJI_CHECK   = "6089010882552008654"   # ✅
EMOJI_MUTE    = "5462990730253319917"   # 🔇
EMOJI_MIC2    = "5260652149469094137"   # 🎙
EMOJI_BAN     = "5463358164705489689"   # ⛔ (alt)
EMOJI_X       = "5454350746407419714"   # ❌
EMOJI_SMILE   = "6183599299499137107"   # ☺️
EMOJI_GUARD   = "6309677544782174678"   # 💂  (owner / admin panel)
EMOJI_CROWN   = "5237699328843200968"   # 👑  main owner / superadmin
EMOJI_LINK    = "5215519737181096462"   # 🔗  link protect
EMOJI_LOCK    = "5373123633101267608"   # 🔒  protected content
EMOJI_CHART   = "5219978203285888739"   # 📊  statistics / charts
EMOJI_FIRE    = "5213452215527677338"   # 🔥  top / popular
EMOJI_GLOBE   = "5215854116564648552"   # 🌐  platform-wide
EMOJI_FOLDER  = "5218777718840180631"   # 📁  file store template
EMOJI_TRASH   = "5215209357985613414"   # 🗑  delete

# ── Cricket-specific emojis ───────────────────────────────────────────────────
EMOJI_CRICKET  = "5188450861226117570"  # 🏏  cricket bat
EMOJI_MEDAL    = "5219886195297809399"  # 🥇  medal / rank
EMOJI_CALENDAR = "5222108309795908493"  # 📅  reuses sparkle slot — update if needed
EMOJI_HOURGLASS = "6089000948292652337" # ⏳  reuses warn slot — pending
EMOJI_USERS    = "5215854116564648552"  # 👥  reuses globe slot


def btn(
    style,
    text: str,
    callback_data: str | None = None,
    url: str | None = None,
    icon: str | None = None,
) -> InlineKeyboardButton:
    """Build an InlineKeyboardButton.

    When the Pyrogram build supports styled buttons the premium emoji icon is
    shown alone (no text emoji needed).  In the fallback build the style colour
    emoji is prepended instead so buttons are never bare.
    """
    if SUPPORTS_BUTTON_STYLE:
        kwargs: dict = {"style": style}
        if icon:
            kwargs["icon_custom_emoji_id"] = icon
    else:
        # Fallback: prefix the colour dot so the button isn't totally plain
        text = f"{style} {text}"
        kwargs = {}
    if url:
        return InlineKeyboardButton(text, url=url, **kwargs)
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def quote(message_id: int, text: str | None = None) -> ReplyParameters:
    if text:
        return ReplyParameters(message_id=message_id, quote=text[:1024])
    return ReplyParameters(message_id=message_id)


# ── Status markers ────────────────────────────────────────────────────────────
TXT_INFO = "🔵"
TXT_WARN = "🟡"
TXT_ERR  = "🔴"
TXT_OK   = "🟢"

SUPPORT_USERNAME = "NexoraaBotss"
SUPPORT_URL      = f"https://t.me/{SUPPORT_USERNAME}"

# ── Image URLs ────────────────────────────────────────────────────────────────
IMG_WELCOME = "https://graph.org/file/ea216e97056aa00b9dc0b-a6c5c48f38d06dfe36.jpg"
IMG_ADMIN   = "https://graph.org/file/e8087a3300ad254ff93d7-c9325cbef36a60e2b4.jpg"
IMG_CLONE   = "https://graph.org/file/874c7523cf9fb087baae4-787a191131ca5d0bb7.jpg"


# ── Keyboards ─────────────────────────────────────────────────────────────────
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(PRIMARY, "Create Bot",      "newbot",  icon=EMOJI_SPARKLE)],
        [
            btn(DEFAULT, "My Bots",      "mybots",  icon=EMOJI_STAR),
            btn(DEFAULT, "Help",         "help",    icon=EMOJI_TOOLS),
        ],
        [btn(DEFAULT, "Platform Stats",  "stats",   icon=EMOJI_CHART)],
        [btn(PRIMARY, "Support",         url=SUPPORT_URL, icon=EMOJI_PHONE)],
    ])


def admin_menu_kb() -> InlineKeyboardMarkup:
    """Superadmin panel keyboard — shown only to the main owner."""
    return InlineKeyboardMarkup([
        [
            btn(PRIMARY, "Platform Stats", "adm:stats",     icon=EMOJI_CHART),
            btn(PRIMARY, "All Bots",       "adm:bots",      icon=EMOJI_GLOBE),
        ],
        [
            btn(SUCCESS, "Broadcast All",  "adm:broadcast", icon=EMOJI_MIC),
            btn(DEFAULT, "All Owners",     "adm:owners",    icon=EMOJI_GUARD),
        ],
        [
            btn(YELLOW,  "Recent Logs",    "adm:logs",      icon=EMOJI_SIREN),
            btn(YELLOW,  "Top Bots",       "adm:topbots",   icon=EMOJI_FIRE),
        ],
        [
            btn(SUCCESS, "Main FSub",      "adm:fsub",      icon=EMOJI_DEVIL),
        ],
        [btn(DANGER,  "Back",             "home",           icon=EMOJI_OCTAGON)],
    ])


def back_kb(callback_data: str = "home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn(DANGER, "Back", callback_data, icon=EMOJI_OCTAGON)]])


def yes_no_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        btn(SUCCESS, "YES", yes_cb, icon=EMOJI_CHECK),
        btn(DANGER,  "NO",  no_cb,  icon=EMOJI_X),
    ]])


def template_kb() -> InlineKeyboardMarkup:
    """Shown after token validation — user picks bot type."""
    return InlineKeyboardMarkup([
        [btn(PRIMARY, "📁 File Store",         "tpl:filestore",   icon=EMOJI_FOLDER)],
        [btn(SUCCESS, "🔗 Link Protect",       "tpl:linkprotect", icon=EMOJI_LINK)],
        [btn(YELLOW,  "🏏 Cricket Tournament", "tpl:cricket",     icon=EMOJI_TROPHY)],
        [btn(DANGER,  "Cancel",                "home",            icon=EMOJI_OCTAGON)],
    ])


def cricket_owner_panel_kb() -> InlineKeyboardMarkup:
    """Owner panel keyboard for cricket bots."""
    return InlineKeyboardMarkup([
        [
            btn(PRIMARY, "Tours",     "own:crik:tours",    icon=EMOJI_TROPHY),
            btn(PRIMARY, "Players",   "own:crik:players",  icon=EMOJI_GUARD),
        ],
        [
            btn(YELLOW,  "Pending",   "own:crik:pending",  icon=EMOJI_BELL),
            btn(YELLOW,  "Captains",  "own:crik:captains", icon=EMOJI_CROWN),
        ],
        [
            btn(BLUE,    "Questions", "own:crik:questions",icon=EMOJI_SPARKLE),
            btn(BLUE,    "Settings",  "own:crik:settings", icon=EMOJI_TOOLS),
        ],
        [
            btn(GREEN,   "Stats",     "own:crik:stats",    icon=EMOJI_CHART),
            btn(GREEN,   "Logs",      "own:crik:logs",     icon=EMOJI_SIREN),
        ],
        [
            btn(BLUE,    "Channels / FSub", "own:crik:channels", icon=EMOJI_DEVIL),
        ],
        [btn(RED,    "Close",         "own:close",         icon=EMOJI_OCTAGON)],
    ])
