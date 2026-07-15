"""Formatting for retry hints."""


def format_retry_delay(seconds: int) -> str:
    """Format a non-negative retry delay."""

    return f"{seconds} seconds"
