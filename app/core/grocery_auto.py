# -*- coding: utf-8 -*-
"""
Grocery auto: use selected items, sell selected items, keep gold via revive pills.

@author by ak
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.core.game_attach import GameAttachSession
from app.core.package_api import (
    DEFAULT_CARRY_PACKAGE_INDEXES,
    DEFAULT_FULL_GOLD_MIN,
    DEFAULT_MONEY_PACKAGE_INDEX,
    DEFAULT_PACKAGE_INDEX,
    DEFAULT_REVIVE_PILL_PRICE,
    PackageActionResult,
    PackageItem,
    ensure_full_gold,
    find_items_by_name,
    format_gold,
    get_money,
    list_package_items,
    list_packages_items,
    read_package_slot,
    sell_item_from_package,
    use_item_in_package,
)

LogFn = Callable[[str], None]


def _pid_blocked(session: GameAttachSession) -> tuple[bool, str]:
    """True when SafeDispatch/remote gate blocks this game pid."""
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session)
    except Exception:
        return False, ""


def _hard_stop_result(action: str, reason: str, *, used: int = 0) -> PackageActionResult:
    msg = f"{action}已停止(进程不可用): {reason}"
    return PackageActionResult(
        ok=used > 0,
        action=action,
        message=msg,
        error=reason or "remote_blocked",
    )



@dataclass
class GroceryConfig:
    """
    Grocery feature parameters.

    @author by ak
    """

    package_index: int = DEFAULT_PACKAGE_INDEX
    money_package_index: int = DEFAULT_MONEY_PACKAGE_INDEX
    min_money: int = DEFAULT_FULL_GOLD_MIN
    pill_price: int = DEFAULT_REVIVE_PILL_PRICE
    # names or "tid=N" selected for auto-use / auto-sell
    use_names: list[str] = field(default_factory=list)
    sell_names: list[str] = field(default_factory=list)
    use_delay_s: float = 0.35
    sell_delay_s: float = 0.35
    # bag open-box loop: max UseItem calls per run (safety)
    open_box_max: int = 200
    # 开箱（勾选/名单）默认间隔
    open_box_delay_s: float = 0.45
    # 「开第一格」固定背包 slot=0；仅 open_first_delay_s 控制节奏，无 fail 空转
    open_first_delay_s: float = 0.05

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_tid_token(token: str) -> int | None:
    """
    Parse "tid=12345" or bare digits from a list entry.

    @author by ak
    """
    s = (token or "").strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("tid="):
        s = s[4:].strip()
    if s.isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return None


def item_matches_want(it: PackageItem, want: list[str]) -> bool:
    """
    Match bag item against name list and/or tid= tokens.

    @author by ak
    """
    if not want:
        return False
    name = (it.name or "").strip()
    tid = int(it.tid or 0)
    for w in want:
        w = (w or "").strip()
        if not w:
            continue
        wt = _parse_tid_token(w)
        if wt is not None:
            if tid and tid == wt:
                return True
            # also allow name field still showing tid=
            if name == f"tid={wt}" or name.endswith(f"tid={wt}"):
                return True
            continue
        if name and (name == w or w in name):
            return True
    return False


def grocery_package_indexes(cfg: GroceryConfig) -> tuple[int, ...]:
    """
    Packages that form the character's usable carry bag.

    Live IVTRTYPE: 2=main bag, 3/4=the two expansion areas provided by
    capacity bags (including 帝王背包). Warehouse 11 is deliberately excluded.

    A non-default package_index keeps the legacy single-package behavior.
    @author by ak
    """
    primary = int(getattr(cfg, "package_index", DEFAULT_PACKAGE_INDEX) or DEFAULT_PACKAGE_INDEX)
    if primary == int(DEFAULT_PACKAGE_INDEX):
        return tuple(int(x) for x in DEFAULT_CARRY_PACKAGE_INDEXES)
    return (primary,)


def _list_grocery_items(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    log: LogFn | None = None,
    use_cache: bool = False,
    max_age_s: float | None = None,
) -> list[PackageItem]:
    """List main + capacity-bag expansion items, preserving package identity."""
    log = log or (lambda _m: None)
    idxs = grocery_package_indexes(cfg)
    try:
        items = list_packages_items(
            session,
            idxs,
            log=log,
            use_cache=bool(use_cache),
            max_age_s=max_age_s,
        )
        if items:
            return list(items)
    except Exception as e:
        log(f"grocery: 多包读取失败，回退主包: {e}")

    # Compatibility/fail-soft fallback. It also keeps old one-package callers
    # and tests functional when package 3/4 are unavailable.
    return list_package_items(session, int(cfg.package_index), log=log)


def open_attach_session(pid: int, *, log: LogFn | None = None) -> GameAttachSession:
    """
    Attach read session for package ops.

    Soft-prefetches bag/money (non-urgent) for high-frequency grocery loops.
    @author by ak
    """
    log = log or (lambda _m: None)
    sess = GameAttachSession(log=log)
    sess.attach(int(pid))
    try:
        from app.core.state_dispatch import StateKind, warmup_session

        warmup_session(
            sess,
            (StateKind.BAG, StateKind.MONEY, StateKind.SCENE, StateKind.POS),
            log=lambda m: log(f"grocery warmup: {m}") if m else None,
        )
    except Exception as e:
        log(f"grocery warmup skip: {e}")
    return sess


def refresh_bag(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    log: LogFn | None = None,
    use_cache: bool = True,
    max_age_s: float | None = None,
) -> list[PackageItem]:
    """
    List bag items for UI.

    Prefer multi-package helper so SafeDispatch bag TTL/single-flight applies.
    Business decides max_age_s; use_cache=False for forced refresh.

    @author by ak
    """
    log = log or (lambda _m: None)
    idxs = grocery_package_indexes(cfg)
    if use_cache:
        try:
            from app.core.state_dispatch import StateKind, get_state

            warm = get_state(
                session,
                StateKind.BAG,
                fresh=False if max_age_s != 0 else True,
                max_age=max_age_s,
                log=lambda _m: None,
            )
            if isinstance(warm, list):
                allowed = set(idxs)
                return [
                    it
                    for it in warm
                    if int(getattr(it, "package", cfg.package_index) or cfg.package_index)
                    in allowed
                ]
        except Exception:
            pass
    try:
        return _list_grocery_items(
            session, cfg, log=log, use_cache=use_cache, max_age_s=max_age_s
        )
    except Exception:
        return list_package_items(session, cfg.package_index, log=log)


# 名称含这些字样时，按「箱子」循环开；否则单次使用
_BOX_NAME_TOKENS = (
    "箱",
    "礼盒",
    "宝匣",
    "礼包",
    "锦盒",
    "匣",
    "袋",
    "包",
    "福袋",
    "盲盒",
)


def looks_like_box_item(name: str = "", tid: int = 0) -> bool:
    """
    Heuristic: bag item is openable box (loop UseItem) vs one-shot use.

    @author by ak
    """
    n = (name or "").strip()
    if n.startswith("tid="):
        # 无名堆叠默认当箱子（可循环开）；单次道具一般有中文名
        return True
    if not n:
        return bool(int(tid or 0))
    return any(tok in n for tok in _BOX_NAME_TOKENS)


def auto_use_or_open_selected(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    items: list[PackageItem] | None = None,
    item_keys: list[tuple[int, int]] | None = None,
    slots: list[int] | None = None,
    names: list[str] | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    合并「使用/开箱」：只执行一轮（每个选中槽 UseItem 一次）。

    按名称区分仅用于日志标记（箱/普通），不再循环开空。

    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        log(f"grocery: use_or_open hard_stop {brsn}")
        return _hard_stop_result("use_or_open", brsn)
    bag = _list_grocery_items(session, cfg, log=log, use_cache=False)
    by_key = {(int(it.package), int(it.slot)): it for it in bag}

    targets: list[PackageItem] = []
    if items:
        for it in items:
            live = by_key.get((int(it.package), int(it.slot)))
            if live is not None:
                targets.append(live)
    elif item_keys:
        for package, slot in item_keys:
            live = by_key.get((int(package), int(slot)))
            if live is not None:
                targets.append(live)
    elif slots:
        # Legacy slot-only input means slots in the configured primary package.
        for s in slots:
            live = by_key.get((int(cfg.package_index), int(s)))
            if live is not None:
                targets.append(live)
    else:
        want = [
            n.strip()
            for n in (names if names is not None else cfg.use_names)
            if n.strip()
        ]
        if not want:
            return PackageActionResult(
                ok=False,
                action="use_or_open",
                message="未选择物品（勾选背包或加入使用名单）",
            )
        for it in bag:
            if item_matches_want(it, want):
                targets.append(it)

    if not targets:
        return PackageActionResult(
            ok=False, action="use_or_open", message="选中物品已不在背包"
        )

    used = 0
    fail = 0
    parts: list[str] = []
    for it in targets:
        if stop_event is not None and stop_event.is_set():
            break
        kind = "箱" if looks_like_box_item(it.name, it.tid) else "用"
        r = use_item_in_package(session, it.package, it.slot, log=log)
        if r.ok:
            used += 1
            parts.append(f"{kind}包{it.package}槽{it.slot}")
        else:
            fail += 1
            parts.append(f"{kind}包{it.package}槽{it.slot}失败")
        time.sleep(max(0.05, float(cfg.use_delay_s)))

    msg = (
        f"使用/开箱(一次) ok={used} fail={fail} "
        f"({'; '.join(parts[:6])}{'…' if len(parts) > 6 else ''})"
    )
    log(f"grocery: {msg}")
    return PackageActionResult(ok=used > 0, action="use_or_open", message=msg)


def _sleep_interruptible(
    seconds: float, stop_event: threading.Event | None = None
) -> bool:
    """Sleep in slices; return True if stop requested. Never sleep negative."""
    end = time.time() + max(0.0, float(seconds or 0.0))
    while True:
        if stop_event is not None and stop_event.is_set():
            return True
        left = end - time.time()
        if left <= 0:
            break
        time.sleep(min(0.2, left))
    return stop_event is not None and stop_event.is_set()


def auto_use_selected(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    names: list[str] | None = None,
    item_keys: list[tuple[int, int]] | None = None,
    slots: list[int] | None = None,
    stop_event: threading.Event | None = None,
    continuous: bool = True,
    idle_s: float = 1.0,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    自动使用名单物品；continuous=True 时循环直到停止。

    每轮：扫背包匹配名单，各 UseItem 一次；无匹配则 idle 等待。

    @author by ak
    """
    log = log or (lambda _m: None)
    want = [n.strip() for n in (names if names is not None else cfg.use_names) if n.strip()]
    key_set = {(int(p), int(s)) for p, s in (item_keys or []) if int(s) >= 0}
    slot_set = {int(s) for s in (slots or []) if int(s) >= 0}
    if not want and not key_set and not slot_set:
        return PackageActionResult(
            ok=False, action="auto_use", message="未选择自动使用物品（名单或勾选槽）"
        )
    used_total = 0
    fail_total = 0
    rounds = 0
    last_err = ""
    attach_fail = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        blocked, brsn = _pid_blocked(session)
        if blocked:
            log(f"grocery: auto_use hard_stop {brsn}")
            return _hard_stop_result("auto_use", brsn)
        try:
            items = _list_grocery_items(session, cfg, log=log, use_cache=False)
            attach_fail = 0
        except Exception as e:
            attach_fail += 1
            log(f"grocery: 自动使用 读背包失败 #{attach_fail}: {e}")
            if attach_fail >= 5 or not continuous:
                last_err = str(e)
                break
            if _sleep_interruptible(max(1.0, float(idle_s)), stop_event):
                break
            continue
        matched = []
        for it in items:
            if key_set:
                if (int(it.package), int(it.slot)) in key_set:
                    matched.append(it)
            elif slot_set:
                # Legacy slot-only selection is scoped to the primary package.
                if int(it.slot) in slot_set:
                    if int(it.package) == int(cfg.package_index):
                        matched.append(it)
            elif item_matches_want(it, want):
                matched.append(it)
        if not matched:
            if not continuous:
                break
            log(f"grocery: 自动使用 无匹配，{idle_s:.1f}s 后复检")
            if _sleep_interruptible(idle_s, stop_event):
                break
            continue
        rounds += 1
        used = 0
        fail = 0
        for it in matched:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                r = use_item_in_package(session, it.package, it.slot, log=log)
            except Exception as e:
                fail += 1
                fail_total += 1
                last_err = str(e)
                log(f"grocery: 自动使用异常 slot={it.slot}: {e}")
                break
            if r.ok:
                used += 1
                used_total += 1
            else:
                fail += 1
                fail_total += 1
                last_err = r.message or r.error or last_err
            if _sleep_interruptible(max(0.05, float(cfg.use_delay_s)), stop_event):
                break
        log(
            f"grocery: 自动使用 round={rounds} used={used} fail={fail} "
            f"total={used_total} want={want or sorted(key_set) or list(slot_set)}"
        )
        if not continuous:
            break
        if stop_event is not None and stop_event.is_set():
            break
        # 本轮打完再扫
        if _sleep_interruptible(0.15, stop_event):
            break
    stopped = stop_event is not None and stop_event.is_set()
    msg = (
        f"{'自动使用已停止' if stopped else '自动使用完成'} "
        f"rounds={rounds} used={used_total} fail={fail_total}"
    )
    if fail_total and last_err:
        msg = f"{msg} last={last_err}"
    log(f"grocery: {msg}")
    return PackageActionResult(ok=used_total > 0, action="auto_use", message=msg)


def auto_open_box(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    names: list[str] | None = None,
    item_keys: list[tuple[int, int]] | None = None,
    slots: list[int] | None = None,
    max_uses: int | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    开箱（原功能）：循环 UseItem 勾选槽 / 使用名单，直到空或 max_uses。

    与「开第一格」无关；需要先勾选或名单。

    @author by ak
    """
    log = log or (lambda _m: None)
    want = [
        n.strip()
        for n in (names if names is not None else cfg.use_names)
        if n.strip()
    ]
    key_set = {(int(p), int(s)) for p, s in (item_keys or []) if int(s) >= 0}
    slot_set = {int(s) for s in (slots or []) if int(s) >= 0}
    if not want and not key_set and not slot_set:
        return PackageActionResult(
            ok=False, action="open_box", message="未选择开箱物品（勾选或加入使用名单）"
        )
    limit = int(max_uses if max_uses is not None else cfg.open_box_max)
    limit = max(1, min(5000, limit))
    delay = max(0.05, float(cfg.open_box_delay_s))
    used = 0
    fail_streak = 0
    last_key: tuple | None = None
    same_key_hits = 0
    while used < limit:
        if stop_event is not None and stop_event.is_set():
            break
        blocked, brsn = _pid_blocked(session)
        if blocked:
            log(f"grocery: open_box hard_stop {brsn}")
            return _hard_stop_result("open_box", brsn, used=used)
        items = _list_grocery_items(
            session, cfg, log=lambda _m: None, use_cache=False
        )
        targets = []
        for it in items:
            if key_set:
                if (int(it.package), int(it.slot)) in key_set:
                    targets.append(it)
            elif slot_set:
                if int(it.slot) in slot_set:
                    if int(it.package) == int(cfg.package_index):
                        targets.append(it)
            elif item_matches_want(it, want):
                targets.append(it)
        if not targets:
            break
        it = targets[0]
        key = (
            int(it.package),
            int(it.slot),
            int(it.tid or 0),
            int(it.count or 0),
        )
        r = use_item_in_package(session, it.package, it.slot, log=log)
        if r.ok:
            used += 1
            fail_streak = 0
            if last_key == key:
                same_key_hits += 1
            else:
                same_key_hits = 0
                last_key = key
            if same_key_hits >= 3:
                log(
                    f"grocery: open_box stop no progress "
                    f"slot={it.slot} tid={it.tid} count={it.count}"
                )
                break
        else:
            fail_streak += 1
            if fail_streak >= 5:
                log(f"grocery: open_box abort fail_streak={fail_streak}")
                break
        time.sleep(delay)
    msg = (
        f"开箱完成 used={used}/{limit} "
        f"want={want or sorted(key_set) or sorted(slot_set)}"
    )
    log(f"grocery: {msg}")
    return PackageActionResult(ok=used > 0, action="open_box", message=msg)


def auto_open_first_slot(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    slot: int = 0,
    stop_event: threading.Event | None = None,
    continuous: bool = True,
    idle_s: float = 1.0,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    开第一格：固定 package slot（默认 0）。

    节奏只受 cfg.open_first_delay_s 控制（与按键宏同语义）：
      调用 UseItem(bridge) → sleep(ms) → 再调
    不做全包扫描、不做 bag-wait 确认（那些会把 100ms 拖成 200~400ms）。
    idle_s 仅兼容旧参数，不参与节流。

    @author by ak
    """
    log = log or (lambda _m: None)
    fixed = max(0, int(slot))
    pack = int(getattr(cfg, "package_index", 2) or 2)
    delay = max(0.01, float(getattr(cfg, "open_first_delay_s", 0.05) or 0.05))
    used = 0
    attempts = 0
    empty_streak = 0
    attach_fail = 0
    br = None
    hwnd = int(getattr(session, "hwnd", 0) or 0)
    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(
            int(session.pid),
            log=lambda _m: None,
            inject_if_needed=False,
            hwnd=hwnd or None,
        )
    except Exception as e:
        log(f"grocery: open_first bridge cache fail: {e}")

    log(
        f"grocery: open_first start slot={fixed} delay={int(delay * 1000)}ms "
        f"bridge={'yes' if br else 'no'}"
    )

    while True:
        if stop_event is not None and stop_event.is_set():
            break
        blocked, brsn = _pid_blocked(session)
        if blocked:
            log(f"grocery: 开第一格 hard_stop {brsn}")
            return _hard_stop_result("open_first", brsn, used=used)

        try:
            it = read_package_slot(session, pack, fixed, log=lambda _m: None)
            attach_fail = 0
        except Exception as e:
            attach_fail += 1
            if attach_fail == 1 or attach_fail % 20 == 0:
                log(f"grocery: 开第一格 读槽失败 #{attach_fail}: {e}")
            if attach_fail >= 50 or not continuous:
                break
            if _sleep_interruptible(delay, stop_event):
                break
            continue

        if it is None:
            empty_streak += 1
            if not continuous:
                break
            # empty: still only wait configured ms
            if empty_streak == 1 or empty_streak % 50 == 0:
                log(f"grocery: 开第一格 slot={fixed} 空 (x{empty_streak})")
            if _sleep_interruptible(delay, stop_event):
                break
            continue
        empty_streak = 0

        try:
            r = use_item_in_package(
                session,
                pack,
                fixed,
                log=log,
                prefer_bridge=True,
                verify_bag=False,
                quiet=True,
                bridge=br,
            )
        except Exception as e:
            log(f"grocery: 开第一格异常 slot={fixed}: {e}")
            if not continuous:
                break
            if _sleep_interruptible(delay, stop_event):
                break
            continue

        attempts += 1
        if r.ok:
            used += 1
        # occasional status (avoid log flood drowning cadence)
        if attempts == 1 or attempts % 40 == 0:
            log(
                f"grocery: open_first tick attempts={attempts} used~={used} "
                f"ret={getattr(r, 'ret', None)} tid={it.tid} count={it.count}"
            )

        if not continuous:
            break
        if _sleep_interruptible(delay, stop_event):
            break

    stopped = stop_event is not None and stop_event.is_set()
    msg = (
        f"{'开第一格已停止' if stopped else '开第一格完成'} "
        f"used~={used} attempts={attempts} slot={fixed} "
        f"delay={int(delay * 1000)}ms"
    )
    log(f"grocery: {msg}")
    return PackageActionResult(ok=used > 0, action="open_first", message=msg)



def auto_sell_selected(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    names: list[str] | None = None,
    stop_event: threading.Event | None = None,
    continuous: bool = True,
    idle_s: float = 1.5,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    自动出售名单物品；continuous=True 时循环直到停止。

    需先开杂货 NPC。无匹配时 idle 等待再扫。

    @author by ak
    """
    log = log or (lambda _m: None)
    want = [n.strip() for n in (names if names is not None else cfg.sell_names) if n.strip()]
    if not want:
        return PackageActionResult(
            ok=False, action="auto_sell", message="未选择自动出售物品"
        )
    sold = 0
    fail_streak = 0
    attach_fail = 0
    idle = max(0.3, float(idle_s))
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        blocked, brsn = _pid_blocked(session)
        if blocked:
            log(f"grocery: auto_sell hard_stop {brsn}")
            return _hard_stop_result("auto_sell", brsn)
        try:
            items = _list_grocery_items(session, cfg, log=log, use_cache=False)
            attach_fail = 0
        except Exception as e:
            attach_fail += 1
            log(f"grocery: 自动出售 读背包失败 #{attach_fail}: {e}")
            if attach_fail >= 5 or not continuous:
                break
            if _sleep_interruptible(idle, stop_event):
                break
            continue
        target = None
        for it in items:
            if item_matches_want(it, want):
                target = it
                break
        if target is None:
            if not continuous:
                break
            log(f"grocery: 自动出售 无匹配，{idle:.1f}s 后复检 want={want}")
            if _sleep_interruptible(idle, stop_event):
                break
            fail_streak = 0
            continue
        before_cnt = max(1, int(target.count or 1))
        n_sell = before_cnt
        try:
            r = sell_item_from_package(
                session, target.package, target.slot, n_sell, log=log
            )
        except Exception as e:
            fail_streak += 1
            log(f"grocery: 自动出售异常 slot={target.slot}: {e}")
            if fail_streak >= 5 or not continuous:
                break
            if _sleep_interruptible(idle, stop_event):
                break
            continue
        if _sleep_interruptible(max(0.05, float(cfg.sell_delay_s)), stop_event):
            break
        try:
            after_items = _list_grocery_items(
                session, cfg, log=lambda _m: None, use_cache=False
            )
        except Exception as e:
            fail_streak += 1
            log(f"grocery: 自动出售 复扫失败: {e}")
            if fail_streak >= 5 or not continuous:
                break
            if _sleep_interruptible(idle, stop_event):
                break
            continue
        after = next(
            (
                x
                for x in after_items
                if int(x.package) == int(target.package)
                and int(x.slot) == int(target.slot)
            ),
            None,
        )
        if after is None:
            sold += 1
            fail_streak = 0
            continue
        ac = int(after.count or 0)
        at = int(after.tid or 0)
        if ac < before_cnt or (
            int(target.tid or 0) and at and at != int(target.tid or 0)
        ):
            sold += 1
            fail_streak = 0
        else:
            fail_streak += 1
            log(
                f"grocery: sell no bag progress slot={target.slot} "
                f"ret={r.ret} count={before_cnt}->{ac}"
            )
            if fail_streak >= 5:
                log(
                    f"grocery: auto_sell fail_streak={fail_streak}，"
                    f"{idle:.1f}s 后重试（保持杂货NPC打开）"
                )
                if not continuous:
                    break
                if _sleep_interruptible(idle, stop_event):
                    break
                fail_streak = 0
    stopped = stop_event is not None and stop_event.is_set()
    if sold <= 0:
        msg = (
            f"{'自动出售已停止' if stopped else '自动出售未成交'} want={want} "
            f"（请先打开杂货NPC交易界面）"
        )
    else:
        msg = (
            f"{'自动出售已停止' if stopped else '自动出售完成'} "
            f"sold={sold} names={want}"
        )
    log(f"grocery: {msg}")
    return PackageActionResult(ok=sold > 0, action="auto_sell", message=msg)


def auto_full_gold(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    stop_event: threading.Event | None = None,
    on_money: Callable[[int], None] | None = None,
    continuous: bool = True,
    log: LogFn | None = None,
) -> PackageActionResult:
    """
    Keep money >= cfg.min_money by selling 白云熊胆丸.

    continuous=True: guard forever until stop (client spends gold; we refill).

    @author by ak
    """
    return ensure_full_gold(
        session,
        min_money=int(cfg.min_money),
        pill_price=int(cfg.pill_price),
        package_index=int(cfg.package_index),
        money_package_index=int(cfg.money_package_index),
        stop_event=stop_event,
        on_money=on_money,
        continuous=bool(continuous),
        log=log,
    )


def read_money_text(
    session: GameAttachSession,
    cfg: GroceryConfig,
    *,
    log: LogFn | None = None,
) -> str:
    """
    One-line money status: 绑定 (卖满金用) + 非绑.

    @author by ak
    """
    from app.core.package_api import format_money_pair

    r = get_money(session, cfg.money_package_index, log=log)
    return format_money_pair(r.money, r.money_trade)
