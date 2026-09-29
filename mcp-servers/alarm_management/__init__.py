"""MCP server exposing the plant alarm management system as tools.

This is the only path by which the copilot is allowed to reach the alarm API — the
assignment's hard constraint, and the reason the tools live here rather than inside the
backend. `build_server` assembles it; `python -m alarm_management` runs it standalone.
"""

from alarm_management.server import SERVER_NAME, SERVER_VERSION, build_server

__all__ = ["SERVER_NAME", "SERVER_VERSION", "build_server"]
