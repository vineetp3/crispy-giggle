"""The single Console every command prints through.

One instance so width, colour and capture behave identically everywhere.
"""

from __future__ import annotations

from rich.console import Console

console = Console()
