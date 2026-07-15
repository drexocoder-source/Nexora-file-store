"""Force-subscribe membership checks."""
from __future__ import annotations

from pyrogram import Client
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import RPCError, UserNotParticipant

from database.models import BotChannel

_NOT_MEMBER = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}


async def is_member(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status not in _NOT_MEMBER
    except UserNotParticipant:
        return False
    except RPCError:
        # Bot lost admin / channel deleted / etc — fail closed but don't crash.
        return False


async def missing_channels(client: Client, channels: list[BotChannel], user_id: int) -> list[BotChannel]:
    missing: list[BotChannel] = []
    for ch in channels:
        if not ch.required:
            continue
        if not await is_member(client, ch.chat_id, user_id):
            missing.append(ch)
    return missing
