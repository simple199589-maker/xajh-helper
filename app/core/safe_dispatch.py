# -*- coding: utf-8 -*-
"""
Platform-wide safe remote dispatch: priority queue, cache, single-flight, gate.

Orchestration only: business code decides *what* and *how often* to call.
This module serializes/gates/merges/caches those calls. CRT still serializes
per game pid; caches are process-local.

@author by ak
"""
from __future__ import annotations

import heapq
import threading
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Hashable, TypeVar

T = TypeVar("T")
LogFn = Callable[[str], None]


class Priority(IntEnum):
    """Lower value = higher priority. @author by ak"""

    P0 = 0  # open-first use, roll send when pending
    P1 = 1  # grocery write, bag verify
    P2 = 2  # cached bag/shop/scene, roll empty poll
    P3 = 3  # repair/vitality, UI refresh
    P4 = 4  # export probe / cold paths


class OpKind(str, Enum):
    RPM = "rpm"
    CRT_READ = "crt_read"
    CRT_WRITE = "crt_write"
    BRIDGE = "bridge"
    LOCAL = "local"


class PidHealth(str, Enum):
    OK = "ok"
    HUNG = "hung"
    HARD_DEAD = "hard_dead"


# Default cache TTLs for shared reads (seconds). Business may pass its own max_age/ttl.
TTL_BAG_S = 0.12
TTL_SHOP_S = 0.30
TTL_SCENE_S = 1.0
TTL_NPC_S = 3.0
TTL_EXPORT_S = 3600.0

# Cache key prefixes (names only; call cadence is owned by each business)
CACHE_BAG = "bag"
CACHE_BAG_SLOT = "bag_slot"
CACHE_SHOP = "shop"
CACHE_ROLL = "loot_roll"
CACHE_NPC = "npc"
CACHE_EXPORT = "export"
CACHE_SCENE = "scene"


class RemoteBlockedError(TimeoutError):
    """Raised when pid is hung/hard_dead and remote work must stop. @author by ak"""

    def __init__(self, pid: int, health: PidHealth | str, message: str = ""):
        self.pid = int(pid)
        self.health = PidHealth(str(health)) if str(health) in PidHealth._value2member_map_ else str(health)
        msg = message or f"pid={pid} remote blocked health={health}"
        super().__init__(msg)


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int
    enqueued_at: float = field(compare=False)
    op: str = field(compare=False, default="")
    kind: str = field(compare=False, default=OpKind.LOCAL.value)
    fn: Callable[[], Any] = field(compare=False, default=lambda: None)
    done: threading.Event = field(compare=False, default_factory=threading.Event)
    result: Any = field(compare=False, default=None)
    error: BaseException | None = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)


@dataclass
class _CacheEntry:
    value: Any
    expire_at: float
    created_at: float


@dataclass
class _Flight:
    event: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None


@dataclass
class _PeriodicJob:
    job_id: str
    pid: int
    interval_s: float
    priority: Priority
    fn: Callable[[], Any]
    kind: OpKind = OpKind.LOCAL
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    last_run_at: float = 0.0
    last_error: str = ""


class _PidWorker:
    """One worker thread + priority queue per game pid. @author by ak"""

    def __init__(self, pid: int):
        self.pid = int(pid)
        self._cv = threading.Condition()
        self._heap: list[_QueueItem] = []
        self._seq = 0
        self._alive = True
        self._thread = threading.Thread(
            target=self._loop,
            name=f"SafeDispatch-{self.pid}",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        priority: Priority = Priority.P2,
        op: str = "",
        kind: OpKind = OpKind.LOCAL,
        wait: bool = True,
        timeout_s: float | None = None,
    ) -> Any:
        if not self._alive:
            raise RuntimeError(f"SafeDispatch worker dead pid={self.pid}")
        item = _QueueItem(
            priority=int(priority),
            seq=0,
            enqueued_at=time.monotonic(),
            op=str(op or ""),
            kind=str(kind.value if isinstance(kind, OpKind) else kind),
            fn=fn,
        )
        with self._cv:
            self._seq += 1
            item.seq = self._seq
            heapq.heappush(self._heap, item)
            self._cv.notify()
        if not wait:
            return None
        ok = item.done.wait(None if timeout_s is None else max(0.0, float(timeout_s)))
        if not ok:
            item.cancelled = True
            raise TimeoutError(
                f"SafeDispatch submit timeout pid={self.pid} op={item.op}"
            )
        if item.error is not None:
            raise item.error
        return item.result

    def stop(self) -> None:
        with self._cv:
            self._alive = False
            self._cv.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while self._alive and not self._heap:
                    self._cv.wait(0.5)
                if not self._alive and not self._heap:
                    return
                if not self._heap:
                    continue
                item = heapq.heappop(self._heap)
            if item.cancelled:
                item.done.set()
                continue
            try:
                item.result = item.fn()
            except BaseException as e:  # noqa: BLE001 — surface to waiter
                item.error = e
            finally:
                item.done.set()


class SafeDispatch:
    """
    Process-local multi-pid safe dispatch hub.

    @author by ak
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._workers: dict[int, _PidWorker] = {}
        self._cache: dict[tuple[int, str], _CacheEntry] = {}
        self._flights: dict[tuple[int, str], _Flight] = {}
        self._periodics: dict[tuple[int, str], _PeriodicJob] = {}
        self._crt_budget: dict[int, list[float]] = {}  # pid -> timestamps
        self._crt_budget_window_s = 1.0
        self._crt_budget_max = 40  # soft budget; P0/P1 not delayed, only counted

    # --- workers ---------------------------------------------------------
    def _worker(self, pid: int) -> _PidWorker:
        pid = int(pid)
        with self._lock:
            w = self._workers.get(pid)
            if w is None:
                w = _PidWorker(pid)
                self._workers[pid] = w
            return w

    def submit(
        self,
        pid: int,
        fn: Callable[[], T],
        *,
        priority: Priority = Priority.P2,
        op: str = "",
        kind: OpKind = OpKind.LOCAL,
        wait: bool = True,
        timeout_s: float | None = 30.0,
        bypass_queue: bool = False,
    ) -> T:
        """
        Run fn under pid serialization.

        RPM/LOCAL default inline after gate.
        CRT/BRIDGE go through per-pid priority queue unless bypass_queue.
        bypass_queue=True: gate + inline (P0 hot path, no extra delay).
        """
        pid = int(pid)
        self.ensure_callable(pid, kind=kind)

        use_queue = (
            not bypass_queue
            and kind in (OpKind.CRT_READ, OpKind.CRT_WRITE, OpKind.BRIDGE)
        )

        def _run() -> T:
            self.ensure_callable(pid, kind=kind)
            if kind in (OpKind.CRT_READ, OpKind.CRT_WRITE, OpKind.BRIDGE):
                self._note_crt(pid)
            try:
                return fn()
            except Exception as e:
                self.note_exception(pid, e)
                raise

        if not use_queue:
            return _run()
        return self._worker(pid).submit(
            _run,
            priority=priority,
            op=op,
            kind=kind,
            wait=wait,
            timeout_s=timeout_s,
        )

    def run_p0(
        self,
        pid: int,
        fn: Callable[[], T],
        *,
        op: str = "",
        kind: OpKind = OpKind.BRIDGE,
    ) -> T:
        """P0 hot path: gate + inline, no extra delay. @author by ak"""
        return self.submit(
            pid,
            fn,
            priority=Priority.P0,
            op=op,
            kind=kind,
            bypass_queue=True,
            wait=True,
        )

    # --- health / gate ---------------------------------------------------
    def pid_health(self, pid: int) -> PidHealth:
        from app.core import remote_runtime as rr

        pid = int(pid)
        if hasattr(rr, "is_pid_hard_dead") and rr.is_pid_hard_dead(pid):
            return PidHealth.HARD_DEAD
        if hasattr(rr, "is_pid_remote_hung") and rr.is_pid_remote_hung(pid):
            return PidHealth.HUNG
        # hung cooldown dict fallback
        try:
            rr.ensure_pid_remote_callable(pid)
        except TimeoutError:
            return PidHealth.HUNG
        except Exception:
            return PidHealth.HARD_DEAD
        return PidHealth.OK

    def ensure_callable(self, pid: int, *, kind: OpKind = OpKind.CRT_READ) -> None:
        """Raise RemoteBlockedError if pid cannot accept remote work."""
        if kind == OpKind.LOCAL:
            return
        from app.core import remote_runtime as rr

        pid = int(pid)
        health = self.pid_health(pid)
        if health is PidHealth.HARD_DEAD:
            raise RemoteBlockedError(pid, health, f"pid={pid} hard_dead")
        # hung: block CRT/BRIDGE only; pure RPM may still probe liveness
        if health is PidHealth.HUNG and kind in (
            OpKind.CRT_READ,
            OpKind.CRT_WRITE,
            OpKind.BRIDGE,
        ):
            raise RemoteBlockedError(pid, health, f"pid={pid} remote hung")
        if kind in (OpKind.CRT_READ, OpKind.CRT_WRITE, OpKind.BRIDGE):
            try:
                rr.ensure_pid_remote_callable(pid)
            except TimeoutError as e:
                raise RemoteBlockedError(pid, PidHealth.HUNG, str(e)) from e

    def is_blocked(self, pid: int) -> bool:
        h = self.pid_health(pid)
        return h in (PidHealth.HUNG, PidHealth.HARD_DEAD)

    def note_exception(self, pid: int, exc: BaseException) -> None:
        """Map OS/CRT failures into gate state. @author by ak"""
        from app.core import remote_runtime as rr

        pid = int(pid)
        msg = str(exc).lower()
        hard = (
            "err=5" in msg
            or "access is denied" in msg
            or "virtualallocex failed" in msg
            or "openprocess failed" in msg
            or ("createremotethread failed" in msg and "err=5" in msg)
        )
        if hard and hasattr(rr, "mark_pid_hard_dead"):
            rr.mark_pid_hard_dead(pid)
            try:
                from app.core import diag_log

                diag_log.error(
                    f"pid={pid} marked hard_dead via SafeDispatch: {exc}",
                    tag="REMOTE",
                )
            except Exception:
                pass
            self.invalidate(pid)  # drop all cache for pid
            return
        if "hung" in msg or "cooldown" in msg:
            if hasattr(rr, "mark_pid_remote_hung"):
                rr.mark_pid_remote_hung(pid)

    def _note_crt(self, pid: int) -> None:
        now = time.monotonic()
        window = float(self._crt_budget_window_s)
        with self._lock:
            arr = self._crt_budget.setdefault(int(pid), [])
            arr.append(now)
            cutoff = now - window
            self._crt_budget[int(pid)] = [t for t in arr if t >= cutoff]

    def crt_rate(self, pid: int) -> int:
        now = time.monotonic()
        with self._lock:
            arr = self._crt_budget.get(int(pid), [])
            return sum(1 for t in arr if t >= now - self._crt_budget_window_s)

    # --- cache / single-flight ------------------------------------------
    def _ck(self, pid: int, key: str) -> tuple[int, str]:
        return (int(pid), str(key))

    def get_cached(self, pid: int, key: str, *, max_age: float | None = None) -> Any | None:
        ck = self._ck(pid, key)
        now = time.monotonic()
        with self._lock:
            ent = self._cache.get(ck)
            if ent is None:
                return None
            if max_age is not None and (now - ent.created_at) > float(max_age):
                return None
            if ent.expire_at and now > ent.expire_at:
                return None
            return ent.value

    def put_cached(
        self,
        pid: int,
        key: str,
        value: Any,
        *,
        ttl_s: float,
    ) -> Any:
        now = time.monotonic()
        ttl = max(0.0, float(ttl_s))
        with self._lock:
            self._cache[self._ck(pid, key)] = _CacheEntry(
                value=value,
                created_at=now,
                expire_at=(now + ttl) if ttl > 0 else 0.0,
            )
        return value

    def invalidate(self, pid: int, *keys: str) -> None:
        """Invalidate specific keys or all keys for pid when keys empty."""
        pid = int(pid)
        with self._lock:
            if not keys:
                drop = [ck for ck in self._cache if ck[0] == pid]
                for ck in drop:
                    self._cache.pop(ck, None)
                return
            for k in keys:
                self._cache.pop(self._ck(pid, k), None)
                # prefix invalidate: bag -> bag, bag:2, bag_slot:2:0
                prefix = str(k)
                drop = [
                    ck
                    for ck in self._cache
                    if ck[0] == pid
                    and (ck[1] == prefix or ck[1].startswith(prefix + ":") or ck[1].startswith(prefix + "_"))
                ]
                for ck in drop:
                    self._cache.pop(ck, None)

    def read_cached(
        self,
        pid: int,
        key: str,
        producer: Callable[[], T],
        *,
        max_age: float | None = None,
        ttl_s: float | None = None,
        priority: Priority = Priority.P2,
        kind: OpKind = OpKind.RPM,
        op: str = "",
    ) -> T:
        """
        Return fresh cache or single-flight produce + store.

        max_age=0 forces producer (still single-flight for concurrent callers).
        """
        pid = int(pid)
        key = str(key)
        ttl = float(TTL_BAG_S if ttl_s is None else ttl_s)
        if max_age is None:
            max_age = ttl
        if max_age is not None and float(max_age) > 0:
            hit = self.get_cached(pid, key, max_age=max_age)
            if hit is not None:
                return hit

        flight_key = self._ck(pid, key)
        leader = False
        with self._lock:
            fl = self._flights.get(flight_key)
            if fl is None:
                fl = _Flight()
                self._flights[flight_key] = fl
                leader = True
        if not leader:
            fl.event.wait(30.0)
            if fl.error is not None:
                raise fl.error
            return fl.value  # type: ignore[return-value]

        try:
            def _prod() -> T:
                return producer()

            # produce under submit for CRT; RPM/local inline
            if kind in (OpKind.CRT_READ, OpKind.CRT_WRITE, OpKind.BRIDGE):
                value = self.submit(
                    pid,
                    _prod,
                    priority=priority,
                    op=op or f"cache:{key}",
                    kind=kind,
                    wait=True,
                )
            else:
                self.ensure_callable(pid, kind=kind)
                value = _prod()
            # A forced producer bypasses a possibly stale entry, but its result
            # is still the newest authoritative value and must replace it.
            if ttl > 0:
                self.put_cached(pid, key, value, ttl_s=ttl)
            fl.value = value
            return value
        except BaseException as e:  # noqa: BLE001
            fl.error = e
            self.note_exception(pid, e)
            raise
        finally:
            fl.event.set()
            with self._lock:
                if self._flights.get(flight_key) is fl:
                    self._flights.pop(flight_key, None)

    # --- periodic jobs ---------------------------------------------------
    def schedule_periodic(
        self,
        pid: int,
        job_id: str,
        interval_s: float,
        fn: Callable[[], Any],
        *,
        priority: Priority = Priority.P2,
        kind: OpKind = OpKind.LOCAL,
        replace: bool = True,
    ) -> str:
        """
        Run fn repeatedly. interval_s is owned by the caller (business policy).

        SafeDispatch only provides the timer + gate/queue orchestration; it does
        not define feature-level call rates.
        """
        pid = int(pid)
        job_id = str(job_id)
        key = (pid, job_id)
        with self._lock:
            old = self._periodics.get(key)
            if old is not None:
                if not replace:
                    return job_id
                old.stop.set()
            job = _PeriodicJob(
                job_id=job_id,
                pid=pid,
                interval_s=max(0.05, float(interval_s)),
                priority=priority,
                fn=fn,
                kind=kind,
            )
            self._periodics[key] = job

            def _runner() -> None:
                # slight stagger
                while not job.stop.is_set():
                    t0 = time.monotonic()
                    health = self.pid_health(pid)
                    # A hung remote thread must stop new CRT/bridge work, but
                    # pure ReadProcessMemory is precisely the lightweight
                    # liveness/safety path that must keep running.  Previously
                    # the blanket is_blocked() check also paused RPM gates,
                    # allowing the short knock-up window to pass unobserved.
                    blocked = bool(
                        health is PidHealth.HARD_DEAD
                        or (
                            health is PidHealth.HUNG
                            and kind is not OpKind.RPM
                        )
                    )
                    if blocked:
                        # stop CRT storms; keep sleeping until cancelled or recovered
                        job.last_error = f"blocked:{health.value}"
                        job.stop.wait(min(2.0, job.interval_s))
                        # if hard_dead, exit periodic
                        if health is PidHealth.HARD_DEAD:
                            break
                        continue
                    try:
                        if kind in (OpKind.CRT_READ, OpKind.CRT_WRITE, OpKind.BRIDGE):
                            self.submit(
                                pid,
                                fn,
                                priority=priority,
                                op=f"periodic:{job_id}",
                                kind=kind,
                                wait=True,
                                timeout_s=max(5.0, job.interval_s * 3),
                            )
                        else:
                            fn()
                        job.last_run_at = time.monotonic()
                        job.last_error = ""
                    except RemoteBlockedError as e:
                        job.last_error = str(e)
                        if self.pid_health(pid) is PidHealth.HARD_DEAD:
                            break
                    except Exception as e:
                        job.last_error = str(e)
                        self.note_exception(pid, e)
                        try:
                            from app.core import diag_log

                            diag_log.error(
                                f"periodic {job_id} pid={pid} err={e}",
                                tag="DISPATCH",
                            )
                        except Exception:
                            pass
                    elapsed = time.monotonic() - t0
                    wait = max(0.05, job.interval_s - elapsed)
                    job.stop.wait(wait)

            th = threading.Thread(
                target=_runner,
                name=f"SafePeriodic-{pid}-{job_id}",
                daemon=True,
            )
            job.thread = th
            th.start()
        return job_id

    def cancel_periodic(self, pid: int, job_id: str | None = None) -> None:
        pid = int(pid)
        with self._lock:
            if job_id is None:
                keys = [k for k in self._periodics if k[0] == pid]
            else:
                keys = [(pid, str(job_id))]
            for k in keys:
                job = self._periodics.pop(k, None)
                if job is not None:
                    job.stop.set()

    def list_periodics(self, pid: int | None = None) -> list[dict]:
        with self._lock:
            items = list(self._periodics.values())
        out = []
        for j in items:
            if pid is not None and j.pid != int(pid):
                continue
            out.append(
                {
                    "pid": j.pid,
                    "job_id": j.job_id,
                    "interval_s": j.interval_s,
                    "priority": int(j.priority),
                    "last_run_at": j.last_run_at,
                    "last_error": j.last_error,
                    "alive": bool(j.thread and j.thread.is_alive()),
                }
            )
        return out

    def drop_pid(self, pid: int) -> None:
        """Cancel periodics + cache for pid (session close). @author by ak"""
        self.cancel_periodic(int(pid))
        self.invalidate(int(pid))


# Process-wide singleton
_DISPATCH: SafeDispatch | None = None
_DISPATCH_LOCK = threading.Lock()



def session_blocked(session_or_pid) -> tuple[bool, str]:
    """
    Business-facing gate probe.

    Returns (blocked, reason). Does not change call cadence — callers decide
    when to check (loop head / before CRT-heavy step).

    @author by ak
    """
    try:
        if isinstance(session_or_pid, int):
            pid = int(session_or_pid)
        else:
            pid = int(getattr(session_or_pid, "pid", 0) or 0)
    except Exception:
        pid = 0
    if pid <= 0:
        return False, ""
    try:
        d = get_dispatch()
        if d.is_blocked(pid):
            return True, f"remote_blocked:{d.pid_health(pid).value}"
    except Exception:
        pass
    try:
        from app.core.remote_runtime import is_pid_remote_blocked, is_pid_hard_dead

        if is_pid_hard_dead(pid):
            return True, "remote_blocked:hard_dead"
        if is_pid_remote_blocked(pid):
            return True, "remote_blocked"
    except Exception:
        pass
    return False, ""


def get_dispatch() -> SafeDispatch:
    global _DISPATCH
    with _DISPATCH_LOCK:
        if _DISPATCH is None:
            _DISPATCH = SafeDispatch()
        return _DISPATCH


def reset_dispatch_for_tests() -> None:
    """Test helper: drop singleton. @author by ak"""
    global _DISPATCH
    with _DISPATCH_LOCK:
        if _DISPATCH is not None:
            # stop periodics
            for pid, jid in list(_DISPATCH._periodics.keys()):
                _DISPATCH.cancel_periodic(pid, jid)
            for w in list(_DISPATCH._workers.values()):
                w.stop()
        _DISPATCH = None


# --- convenience keys ----------------------------------------------------

def bag_cache_key(package_indexes: list[int] | tuple[int, ...] | None = None) -> str:
    if not package_indexes:
        return f"{CACHE_BAG}:default"
    idxs = ",".join(str(int(x)) for x in package_indexes)
    return f"{CACHE_BAG}:{idxs}"


def bag_slot_key(package_index: int, slot: int) -> str:
    return f"{CACHE_BAG_SLOT}:{int(package_index)}:{int(slot)}"


def roll_cache_key() -> str:
    return CACHE_ROLL


def shop_cache_key() -> str:
    return CACHE_SHOP


__all__ = [
    "Priority",
    "OpKind",
    "PidHealth",
    "RemoteBlockedError",
    "SafeDispatch",
    "get_dispatch",
    "session_blocked",
    "reset_dispatch_for_tests",
    "TTL_BAG_S",
    "TTL_SHOP_S",
    "CACHE_BAG",
    "CACHE_BAG_SLOT",
    "CACHE_SHOP",
    "CACHE_ROLL",
    "bag_cache_key",
    "bag_slot_key",
    "roll_cache_key",
    "shop_cache_key",
]
