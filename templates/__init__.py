"""Template package for Nexora clone bot types.

Each sub-package (filestore, linkprotect, cricket) holds all handlers,
keyboards, and helpers for that bot type.  The shared /start flow,
force-subscribe logic, and owner-panel skeleton live in
clonebot/handlers.py and call into these modules as needed.
"""
