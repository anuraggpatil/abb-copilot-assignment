"""Alarm Management API simulator — the source system the copilot integrates with.

Stands in for a plant historian / alarm management system. It exists so the assignment is
runnable end to end with no external dependency, and so the dataset can be shaped to make
the acceptance scenario's investigation lead somewhere real.

The copilot never imports from this package: it reaches the API over HTTP, through the MCP
server, exactly as it would a third-party system.
"""

__version__ = "0.1.0"
