"""CLI entry point: walks COMMAND_GROUPS in order to register every command.

Registration order is `poc --help` order. Details: docs/reference/presentation.md
"""

from __future__ import annotations

from importlib import import_module

from pier39_poc.cli.context import app

COMMAND_GROUPS = (
    "ingest_cmds",
    "query_cmds",
    "inspect_cmds",
    "chat_cmds",
    "workflow_cmds",
)

for _group in COMMAND_GROUPS:
    import_module(f"pier39_poc.cli.{_group}")


if __name__ == "__main__":
    app()
