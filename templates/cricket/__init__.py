"""Cricket Tournament template."""
from templates.cricket.handlers import (
    register_cricket_handlers,
    handle_cricket_start,
    dispatch_cricket_owner_action,
    handle_cricket_wizard_message,
    seed_default_questions,
)

__all__ = [
    "register_cricket_handlers",
    "handle_cricket_start",
    "dispatch_cricket_owner_action",
    "handle_cricket_wizard_message",
    "seed_default_questions",
]
