"""Execution-layer exceptions."""
from __future__ import annotations


class ExecutionHaltError(Exception):
    """Raised by LiveExecutionPolicy when execution fails and the bot must halt."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
