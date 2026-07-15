"""Lightweight in-memory conversation state.

Each clone bot process is single-instance and long-running, so a plain dict
is sufficient to track "what is this owner in the middle of doing" without
adding an FSM framework or extra DB tables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingAction:
    action: str
    data: dict[str, Any] = field(default_factory=dict)


# Main bot: keyed by the Telegram user id talking to Nexora itself.
main_pending: dict[int, PendingAction] = {}

# Clone bots: keyed by (bot_id, telegram user id) since many clones share
# this one process.
clone_pending: dict[tuple[int, int], PendingAction] = {}
