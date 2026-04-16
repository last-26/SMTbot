"""Cross-platform async shutdown plumbing.

A single `asyncio.Event` is the source of truth for "stop the loop". Signals
hand the event its `.set()`; the runner polls `shutdown.is_set()` between
cycles. Platform caveats:

  * POSIX: `loop.add_signal_handler(SIGINT/SIGTERM, ...)` works — use it.
  * Windows ProactorEventLoop: `add_signal_handler` raises NotImplementedError.
    We fall back to stdlib `signal.signal(...)` + `loop.call_soon_threadsafe`
    so the handler fires from the OS thread but the event mutation runs on
    the loop thread.
  * Windows terminal Ctrl-C: the CRT short-circuits and raises
    `KeyboardInterrupt` directly at `asyncio.run`, so `__main__.py` must
    also catch it. The signal handler here is a best-effort second layer.

Called once at the top of `BotRunner.run()`. Registering twice replaces the
previous handler, which is fine for tests that restart the loop.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Iterable


def install_shutdown_handlers(event: asyncio.Event) -> None:
    """Wire SIGINT / SIGTERM / (SIGBREAK on Windows) to `event.set()`.

    Never raises — platforms that don't support a given signal are logged
    by the caller (if at all); the bot still starts.
    """
    loop = asyncio.get_running_loop()
    signals: Iterable[int] = (signal.SIGINT, signal.SIGTERM)

    for sig in signals:
        _install_one(loop, event, sig)

    # Windows Ctrl-Break — doesn't exist on POSIX.
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        _install_one(loop, event, sigbreak)


def _install_one(loop: asyncio.AbstractEventLoop, event: asyncio.Event,
                 sig: int) -> None:
    # Preferred path — POSIX SelectorEventLoop.
    try:
        loop.add_signal_handler(sig, event.set)
        return
    except (NotImplementedError, RuntimeError):
        pass

    # Windows fallback: stdlib signal handler. Runs on the OS thread, so
    # schedule the event.set() onto the loop thread.
    try:
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(event.set))
    except (OSError, ValueError):
        # Either the signal isn't valid on this platform, or we're not on
        # the main thread. Either way, the KeyboardInterrupt path in
        # __main__ covers interactive shutdown.
        pass
