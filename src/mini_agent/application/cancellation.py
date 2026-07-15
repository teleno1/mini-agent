"""Small host-controlled interrupt state machine for the interactive CLI."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from types import FrameType


class ForcedInterrupt(KeyboardInterrupt):
    """Raised after the user interrupts a cancellation cleanup a second time."""

    exit_code = 130


class InterruptController:
    """Turn the first Ctrl+C into cancellation and the second into exit 130."""

    def __init__(
        self,
        task: asyncio.Task[object] | None = None,
        *,
        cleanup_seconds: float = 5.0,
        on_acknowledged: Callable[[], object] | None = None,
    ) -> None:
        if cleanup_seconds <= 0:
            raise ValueError("interrupt cleanup window must be positive")
        self.task = task
        self.cleanup_seconds = cleanup_seconds
        self.on_acknowledged = on_acknowledged
        self.interrupt_count = 0
        self.acknowledged = False
        self.forced = False
        self.cancel_event = asyncio.Event()
        self._old_handler: object = signal.getsignal(signal.SIGINT)

    def install(self) -> None:
        """Install a main-thread-safe SIGINT handler for one async Turn."""

        try:
            signal.signal(signal.SIGINT, self._handle_signal)
        except (ValueError, OSError):
            # Embedded/event-loop hosts may not own signal handling.  The
            # application remains cancellable through task.cancel().
            return

    def uninstall(self) -> None:
        try:
            signal.signal(signal.SIGINT, self._old_handler)  # type: ignore[arg-type]
        except (ValueError, OSError):
            return

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self.request_interrupt()

    def request_interrupt(self) -> None:
        """Record an interrupt; useful for tests and non-signal UI hosts."""

        self.interrupt_count += 1
        if self.interrupt_count == 1:
            self.acknowledged = True
            self.cancel_event.set()
            if self.on_acknowledged is not None:
                try:
                    self.on_acknowledged()
                except Exception:
                    pass
            if self.task is not None:
                self.task.cancel()
            return
        self.forced = True
        if self.task is not None:
            self.task.cancel()


__all__ = ["ForcedInterrupt", "InterruptController"]
