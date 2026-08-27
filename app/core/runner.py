# -*- coding: utf-8 -*-
"""Shared lifecycle primitives for stoppable background automation runners."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


class RunnerEventProtocol(Protocol):
    phase: str
    message: str
    ok: bool
    detail: dict


@dataclass(frozen=True)
class RunnerEvent:
    phase: str
    message: str
    ok: bool = True
    detail: dict = field(default_factory=dict)


class StopToken:
    """Small wrapper that keeps the underlying Event available for legacy APIs."""

    def __init__(self, event: threading.Event | None = None):
        self.event = event or threading.Event()

    def stop(self) -> None:
        self.event.set()

    def reset(self) -> None:
        self.event.clear()

    @property
    def stopped(self) -> bool:
        return self.event.is_set()


def interruptible_sleep(
    seconds: float,
    stop_event: threading.Event | StopToken | None,
    *,
    interval: float = 0.1,
) -> bool:
    """Wait up to seconds and return False as soon as stop is requested."""
    event = stop_event.event if isinstance(stop_event, StopToken) else stop_event
    duration = max(0.0, float(seconds))
    if event is None:
        time.sleep(duration)
        return True
    return not event.wait(duration) if duration else not event.is_set()


class RunnerLifecycle:
    """Own one daemon worker and an idempotent stop token."""

    def __init__(self, name: str):
        self.name = str(name)
        self.stop_token = StopToken()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    @property
    def stop_event(self) -> threading.Event:
        return self.stop_token.event

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self, target: Callable[[], None]) -> threading.Thread | None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return None
            self.stop_token.reset()

            def run() -> None:
                try:
                    target()
                finally:
                    with self._lock:
                        if threading.current_thread() is self._thread:
                            self._thread = None

            self._thread = threading.Thread(
                target=run, name=self.name, daemon=True
            )
            self._thread.start()
            return self._thread

    def stop(self, *, wait: bool = False, timeout: float = 2.0) -> bool:
        self.stop_token.stop()
        if wait:
            return self.wait(timeout=timeout)
        return not self.is_running()

    def wait(self, timeout: float = 2.0) -> bool:
        """Wait briefly for the owned worker; never join the current thread."""
        with self._lock:
            thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout=max(0.0, float(timeout)))
        return not thread.is_alive()

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())


def emit_safely(callback: Callable[[RunnerEventProtocol], None], event) -> None:
    """Runner callbacks must never terminate an automation worker."""
    try:
        callback(event)
    except Exception:
        pass
