# -*- coding: utf-8 -*-
"""
换光切糕/快乐丹 -> 绝学兑换残页 -> 霸刀牌 流水线。

经济:
  1 切糕(50丹切糕) = 50 快乐丹
  2 快乐丹 = 1 绝学兑换残页
  6000 残页 = 1 霸刀牌
栈: 切糕/丹/残页 9999/格；牌默认 999/格。

扫描范围: 主包(2) + 扩展PACK1/2(3/4)（可顺带读仓库仅作展示，兑换不取仓）。
兑换只用非绑(item+0x6C==0)。霸刀 tid=101058。
一组9999兑换丹(tid=101020): 与切糕同类原料，1 个 → 9999 快乐丹；
  栈上限 9999；批量按空位先验算：只备「本波残页」所需丹（丹/2=页，买页时丹腾格），
  霸刀仅预留 2~3 空格；禁止空位拉满式乱卖。
切糕: 1 → 50 丹（满组 9999 切糕 → 50×9999 丹）。
快乐兑换丹*100(tid=101062): 仍可 UseItem 开包（+100 丹）。

仓库: 用户手动取到随身包；脚本每轮重扫随身动态继续。不自动取仓、不仓直卖。

v1 定版：双 NPC 分业 + 空位压力智能分流。
  阶段A 东方：卖组丹/切糕 + 买残页；有丹可换页时优先买书页。
  空位压到安全格：先判断买书页还是买霸刀（丹未必会空，切糕会持续补丹）。
    - 能换页 → 买书页（压缩占格，同店）
    - 换不动页且页>=6000 → 买霸刀腾格（即使还有切糕/组丹）
    - 空位紧时禁止再卖切糕把包撑死
  阶段B 不败：页够且（A推不动 或 空位压力出牌）才 OP_TOKEN。
安全预留3；残页单笔硬顶9999；队列单批不混阶段。

@author by ak
"""
from __future__ import annotations

from collections import deque
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from app.core.game_attach import GameAttachSession
from app.core.grocery_auto import item_matches_want
from app.core.package_api import (
    DEFAULT_CARRY_PACKAGE_INDEXES,
    DEFAULT_EXCHANGE_PACKAGE_INDEXES,
    DEFAULT_PACKAGE_INDEX,
    PACKAGE_INDEX_WAREHOUSE,
    PackageActionResult,
    PackageItem,
    buy_item_by_shop_slot,
    buy_item_from_npc,
    resolve_shop_goods_meta,
    buy_goods_tid_via_shop,
    find_shop_page_slot_for_tid,
    remember_shop_slot,
    clear_shop_slot_cache,
    DEFAULT_SHOP_SLOT_BY_TID,
    DEFAULT_BOOTH_BY_TID,
    find_shop_goods,
    list_shop_goods,
    sell_item_from_package,
    get_package_layout,
    list_packages_items,
    move_item_between_packages,
    npc_say_hello,
    package_free_slots,
    use_item_in_package,
)

LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]

STACK_MAX = 9999
TOKEN_STACK_MAX = 999
DAN_PER_PAGE = 2
PAGES_PER_TOKEN = 6000
QIEGAO_TO_DAN = 50
PACK9999_TO_DAN = 9999  # 1 一组9999兑换丹 -> 9999 快乐丹
PAGE_BATCH_MAX = 9999  # live: 超过 9999 会「无法使用该NPC功能」

OP_TOKEN = "EXCHANGE_TOKEN"
OP_PAGE = "EXCHANGE_PAGE"
OP_QIEGAO = "EXCHANGE_QIEGAO"
OP_PACK9999 = "EXCHANGE_PACK9999"
OP_OPEN = "OPEN_PACK"
OP_PAUSE = "PAUSE"
OP_DONE = "DONE"
OP_MOVE = "MOVE_TO_CARRY"

PACK_9999_TID = 101020
PACK_100_TID = 101062
PACK_9999_DAN = 9999
PACK_100_DAN = 100


@dataclass
class BadaoExchangeConfig:
    """UI / runtime config. @author by ak"""

    package_index: int = DEFAULT_PACKAGE_INDEX
    package_indexes: list[int] = field(
        default_factory=lambda: list(DEFAULT_EXCHANGE_PACKAGE_INDEXES)
    )
    carry_indexes: list[int] = field(
        default_factory=lambda: list(DEFAULT_CARRY_PACKAGE_INDEXES)
    )
    pages_per_token: int = PAGES_PER_TOKEN
    page_batch_max: int = PAGE_BATCH_MAX  # 残页单笔上限 9999（服端限制）
    page_buy_max: int = PAGE_BATCH_MAX
    stack_max: int = STACK_MAX
    token_stack_max: int = TOKEN_STACK_MAX
    dan_per_page: int = DAN_PER_PAGE
    qiegao_to_dan: int = QIEGAO_TO_DAN
    npc_name: str = "东方姑娘"  # 兼容：页/切糕/组丹默认 NPC
    npc_name_page: str = "东方姑娘"  # 买残页 / 切糕·组丹兑换
    npc_name_token: str = "不败姑娘"  # 买霸刀牌
    npc_radius: float = 35.0
    pause_poll_s: float = 1.5
    trade_delay_s: float = 0.35  # 非残页基础间隔
    trade_delay_after_sell_s: float = 1.05  # 卖切糕/组丹后包变动大，略长于买页
    trade_delay_after_token_s: float = 0.70  # 出牌后稍歇
    # 残页自适应：连成功 → page_fast；刚恢复/首笔 → page_warm；失败 → fail_backoff（禁止狂点）
    trade_delay_after_page_s: float = 0.16  # 稳态目标间隔（手速 100~200ms 量级）
    page_fast_s: float = 0.12  # 连成功 ≥page_fast_after 笔
    page_warm_s: float = 0.22  # 刚开店/卖料后/失败恢复期
    page_fast_after: int = 2  # 连续成功这么多笔才切全速
    page_burst_pause_every: int = 16  # 连续成功换页 N 笔后极短歇
    page_burst_pause_s: float = 0.18
    page_recheck_bag_s: float = 0.10  # ret!=0 但首扫无进度时再扫一次，防高速误判
    fail_backoff_base_s: float = 1.8  # 无背包变化后的退避基数（先停手再恢复）
    fail_backoff_step_s: float = 0.9  # 每多失败一次再加
    fail_streak_max: int = 6
    # 连续无进度：1 次软刷新会话，达到该次数才关店重开
    npc_soft_refresh_after_fails: int = 1
    npc_reopen_after_fails: int = 3
    # 关店重开计数：成功会 -1；撞顶后长歇继续，不再永久停
    npc_reopen_max: int = 20
    npc_reopen_cooldown_s: float = 12.0  # 撞顶后冷却秒数
    # 单笔协议上限（游戏栈/payload）；真正批量由空位先验算决定
    max_buy_count: int = STACK_MAX  # 其它用途；残页用 page_buy_max
    max_pack9999_per_trade: int = STACK_MAX  # 安全顶，规划侧按需裁
    max_qiegao_per_trade: int = STACK_MAX
    # 安全预留空格（防卖切糕/组丹撑爆；含落牌缓冲）
    token_reserve_slots: int = 3
    safety_free_slots: int = 3  # 霸刀/安全预留，默认 3 格
    # 空位 <= 该值：触发压力分流（先书页后霸刀；丹/切糕未空也可出牌腾格）
    free_pressure_slots: int = 3
    # 双 NPC 自动切换：东方↔不败 需要 Hello+点兑换（用户无法两边同时开店）
    require_npc_hello: bool = False
    require_shop_dlg: bool = True
    buy_prefer_cur_serv: bool = True
    auto_select_service: bool = True
    auto_npc_hello: bool = True
    reuse_open_shop: bool = True
    # 目标牌数：0=尽量全换；>0 本轮只换这么多（例如 1）
    service_settle_s: float = 0.48  # Hello/关店重开后等菜单稳定
    hwnd: int = 0
    dan_names: list[str] = field(
        default_factory=lambda: ["快乐兑换丹", "tid=100023"]
    )
    page_names: list[str] = field(
        default_factory=lambda: ["绝学兑换残页", "tid=100007"]
    )
    token_names: list[str] = field(
        default_factory=lambda: [
            "霸刀召唤牌",
            "霸刀牌子",
            "福州郊外上官霸刀",
            "上官霸刀",
            "tid=101058",
        ]
    )
    qiegao_names: list[str] = field(
        default_factory=lambda: ["50丹切糕", "切糕", "tid=100064"]
    )
    pack9999_names: list[str] = field(
        default_factory=lambda: ["一组9999兑换丹", "tid=101020"]
    )
    pack100_names: list[str] = field(
        default_factory=lambda: ["快乐兑换丹*100", "tid=101062"]
    )
    dan_tid: int = 100023
    page_tid: int = 100007
    qiegao_tid: int = 100064
    token_tid: int = 101058  # live sample: 福州郊外上官霸刀
    pack9999_tid: int = PACK_9999_TID
    pack100_tid: int = PACK_100_TID
    require_unbound: bool = True
    auto_open_packs: bool = True  # only 快乐兑换丹*100 open path
    pack9999_to_dan: int = PACK9999_TO_DAN
    # 兑换店 goods_tid 默认=产物/原料背包 tid（东方姑娘同类兑换可用）
    goods_tid_page: int = 100007  # 绝学兑换残页
    goods_tid_token: int = 101058  # 与背包 tid 对齐；商店 goods 若不同可改 UI
    goods_tid_qiegao: int = 100064  # 50丹切糕
    goods_tid_pack9999: int = 101020  # 一组9999兑换丹
    goods_index_page: int = 7  # live 货架 booth：绝学兑换残页 goods+0x1C
    goods_index_token: int = 0
    goods_index_qiegao: int = 0
    goods_index_pack9999: int = 0
    # 目标出牌数：0=尽量换光随身可转化原料；>0 达到后停止
    target_tokens: int = 0
    # 兼容旧配置；仓库自动取/提示已关闭，始终不拉仓
    warehouse_pause_hint: bool = False

    def to_dict(self) -> dict:
        return asdict(self)
@dataclass
class ResourceSnap:
    count: int = 0
    slots: int = 0
    stack_room: int = 0
    bound_count: int = 0
    bound_slots: int = 0
    items: list[PackageItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "slots": self.slots,
            "stack_room": self.stack_room,
            "bound_count": self.bound_count,
            "bound_slots": self.bound_slots,
        }


@dataclass
class BagSnap:
    cap: int = 0
    used: int = 0
    free: int = 0
    carry_free: int = 0
    warehouse_free: int = 0
    free_by_pkg: dict = field(default_factory=dict)
    dan: ResourceSnap = field(default_factory=ResourceSnap)
    page: ResourceSnap = field(default_factory=ResourceSnap)
    token: ResourceSnap = field(default_factory=ResourceSnap)
    qiegao: ResourceSnap = field(default_factory=ResourceSnap)
    pack9999: ResourceSnap = field(default_factory=ResourceSnap)
    pack100: ResourceSnap = field(default_factory=ResourceSnap)
    items: list[PackageItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cap": self.cap,
            "used": self.used,
            "free": self.free,
            "carry_free": self.carry_free,
            "warehouse_free": self.warehouse_free,
            "free_by_pkg": dict(self.free_by_pkg or {}),
            "dan": self.dan.to_dict(),
            "page": self.page.to_dict(),
            "token": self.token.to_dict(),
            "qiegao": self.qiegao.to_dict(),
            "pack9999": self.pack9999.to_dict(),
            "pack100": self.pack100.to_dict(),
        }


@dataclass
class TradeOp:
    kind: str
    count: int = 0
    reason: str = ""
    goods_tid: int = 0
    goods_index: int = 0
    package: int = -1
    slot: int = -1
    pack_kind: str = ""
    expect_delta: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def _match_resource(
    it: PackageItem,
    names: list[str],
    *,
    kind: str,
    tid_hint: int = 0,
) -> bool:
    tid = int(getattr(it, "tid", 0) or 0)
    name = (it.name or "").strip()

    if kind == "dan":
        if "切糕" in name or "一组" in name or "*" in name:
            return False
        if tid in (PACK_9999_TID, PACK_100_TID, 101020, 101062):
            return False
        if tid == 100023 or (tid_hint and tid == int(tid_hint)):
            return True
        if name == "快乐兑换丹":
            return True
        return bool(item_matches_want(it, names)) and (
            "快乐" in name and "丹" in name
        )

    if kind == "qiegao":
        if tid in (PACK_9999_TID, PACK_100_TID, 101020, 101062):
            return False
        if tid == 100064 or (tid_hint and tid == int(tid_hint)):
            return True
        if "切糕" in name:
            return True
        return bool(item_matches_want(it, names))

    if kind == "pack9999":
        if tid in (PACK_9999_TID, 101020) or (tid_hint and tid == int(tid_hint)):
            return True
        if "一组9999兑换丹" in name:
            return True
        return bool(item_matches_want(it, names))

    if kind == "pack100":
        if tid in (PACK_100_TID, 101062) or (tid_hint and tid == int(tid_hint)):
            return True
        if "快乐兑换丹*100" in name or ("快乐" in name and "*100" in name):
            return True
        return bool(item_matches_want(it, names))

    if kind == "page":
        if tid == 100007 or (tid_hint and tid == int(tid_hint)):
            return True
        if "绝学" in name and "残页" in name:
            return True
        return bool(item_matches_want(it, names))

    if kind == "token":
        if tid == 101058 or (tid_hint and tid == int(tid_hint)):
            return True
        if "霸刀" in name and ("牌" in name or "召唤" in name or "上官" in name):
            return True
        if "福州郊外上官霸刀" in name:
            return True
        return bool(item_matches_want(it, names))

    if tid_hint and tid == int(tid_hint):
        return True
    return bool(item_matches_want(it, names))


def _is_usable_bind(it: PackageItem, *, require_unbound: bool, kind: str) -> bool:
    if not require_unbound:
        return True
    bind = int(getattr(it, "bind", -1))
    if bind == 1:
        return False
    if bind == 0:
        return True
    if kind == "dan":
        return False
    return True


def _resource_from_items(
    items: list[PackageItem],
    names: list[str],
    *,
    kind: str,
    stack_max: int,
    require_unbound: bool = True,
    tid_hint: int = 0,
) -> ResourceSnap:
    matched: list[PackageItem] = []
    total = 0
    room = 0
    bound_count = 0
    bound_slots = 0
    for it in items:
        if not _match_resource(it, names, kind=kind, tid_hint=tid_hint):
            continue
        c = max(0, int(it.count or 0))
        if not _is_usable_bind(it, require_unbound=require_unbound, kind=kind):
            bound_count += c
            bound_slots += 1
            continue
        matched.append(it)
        total += c
        room += max(0, int(stack_max) - c)
    return ResourceSnap(
        count=total,
        slots=len(matched),
        stack_room=room,
        bound_count=bound_count,
        bound_slots=bound_slots,
        items=matched,
    )
def snapshot_bag(
    session: GameAttachSession,
    cfg: BadaoExchangeConfig,
    *,
    log: LogFn | None = None,
    items: list[PackageItem] | None = None,
) -> BagSnap:
    log = log or (lambda _m: None)
    idxs = list(getattr(cfg, "package_indexes", None) or [cfg.package_index])
    if items is not None:
        bag_items = list(items)
        cap = 0
        for idx in idxs:
            lay = get_package_layout(session, int(idx), log=log, with_items=False)
            cap += int(lay.cap or 0)
        used = len(bag_items)
        free_total = max(0, cap - used)
        layout_cap = cap
    else:
        bag_items = list_packages_items(session, idxs, log=log)
        cap = used = free_total = 0
        for idx in idxs:
            lay = get_package_layout(session, int(idx), log=log, with_items=True)
            cap += int(lay.cap or 0)
            used += int(lay.used_slots or 0)
            free_total += int(lay.free_slots or 0)
        layout_cap = cap

    carry_idxs = [
        int(x)
        for x in (
            getattr(cfg, "carry_indexes", None) or DEFAULT_CARRY_PACKAGE_INDEXES
        )
    ]
    # 规划空位：主包+扩展PACK1/2（不含仓库）；单独再扫随身三包，避免漏扩包
    free_map: dict = {}
    try:
        scan_idxs = list(dict.fromkeys(list(idxs) + list(carry_idxs)))
        free_map = package_free_slots(session, scan_idxs, log=log)
    except Exception:
        free_map = {}
    # 随身空位：逐包 layout 实扫（主包 200 格等），不用模糊估值
    carry_free = 0
    wh_free = 0
    for pi in carry_idxs:
        try:
            lay = get_package_layout(session, int(pi), log=log, with_items=True)
            fr = int(lay.free_slots or 0)
            carry_free += fr
            free_map[int(pi)] = {
                "cap": int(lay.cap or 0),
                "used": int(lay.used_slots or 0),
                "free": fr,
                "size": int(lay.size or 0),
            }
        except Exception:
            row = (free_map or {}).get(int(pi)) or (free_map or {}).get(pi) or {}
            carry_free += int((row or {}).get("free") or 0)
    for pi, row in (free_map or {}).items():
        if int(pi) == int(PACKAGE_INDEX_WAREHOUSE):
            wh_free += int((row or {}).get("free") or 0)

    sm = int(cfg.stack_max)
    tm = int(cfg.token_stack_max)
    req = bool(getattr(cfg, "require_unbound", True))
    return BagSnap(
        cap=int(layout_cap or 0),
        used=used,
        free=int(free_total),
        carry_free=int(carry_free),
        warehouse_free=int(wh_free),
        free_by_pkg=dict(free_map or {}),
        dan=_resource_from_items(
            bag_items,
            cfg.dan_names,
            kind="dan",
            stack_max=sm,
            require_unbound=req,
            tid_hint=int(getattr(cfg, "dan_tid", 0) or 0),
        ),
        page=_resource_from_items(
            bag_items,
            cfg.page_names,
            kind="page",
            stack_max=sm,
            require_unbound=req,
            tid_hint=int(getattr(cfg, "page_tid", 0) or 0),
        ),
        token=_resource_from_items(
            bag_items,
            cfg.token_names,
            kind="token",
            stack_max=tm,
            require_unbound=req,
            tid_hint=int(getattr(cfg, "token_tid", 0) or 0),
        ),
        qiegao=_resource_from_items(
            bag_items,
            cfg.qiegao_names,
            kind="qiegao",
            stack_max=sm,
            require_unbound=req,
            tid_hint=int(getattr(cfg, "qiegao_tid", 0) or 0),
        ),
        pack9999=_resource_from_items(
            bag_items,
            list(getattr(cfg, "pack9999_names", None) or []),
            kind="pack9999",
            stack_max=sm,
            require_unbound=req,
            tid_hint=int(getattr(cfg, "pack9999_tid", 0) or PACK_9999_TID),
        ),
        pack100=_resource_from_items(
            bag_items,
            list(getattr(cfg, "pack100_names", None) or []),
            kind="pack100",
            stack_max=sm,
            require_unbound=req,
            tid_hint=int(getattr(cfg, "pack100_tid", 0) or PACK_100_TID),
        ),
        items=bag_items,
    )


def slots_needed_for_add(
    add_count: int, stack_room: int, free_slots: int, stack_max: int
) -> int:
    remain = max(0, int(add_count) - max(0, int(stack_room)))
    if remain <= 0:
        return 0
    sm = max(1, int(stack_max))
    return int(math.ceil(remain / float(sm)))


def can_receive(
    add_count: int, stack_room: int, free_slots: int, stack_max: int
) -> bool:
    need = slots_needed_for_add(add_count, stack_room, free_slots, stack_max)
    return need <= max(0, int(free_slots))


def max_receivable(stack_room: int, free_slots: int, stack_max: int) -> int:
    sm = max(1, int(stack_max))
    return max(0, int(stack_room)) + max(0, int(free_slots)) * sm


def slots_for_count(count: int, stack_max: int) -> int:
    """理想叠放下 count 个物品占用格数（count<=0 → 0）。"""
    c = max(0, int(count))
    if c <= 0:
        return 0
    sm = max(1, int(stack_max))
    return int(math.ceil(c / float(sm)))


def net_slots_delta(
    *,
    before_counts: dict[str, int],
    after_counts: dict[str, int],
    stack_max: int,
    token_stack_max: int | None = None,
) -> int:
    """
    理想叠放下，若干资源 count 变化导致的占格净增（可负=腾格）。
    token 可用独立栈上限。
    """
    sm = max(1, int(stack_max))
    tm = max(1, int(token_stack_max or sm))
    keys = set(before_counts) | set(after_counts)
    delta = 0
    for k in keys:
        lim = tm if k == "token" else sm
        b = slots_for_count(int(before_counts.get(k, 0) or 0), lim)
        a = slots_for_count(int(after_counts.get(k, 0) or 0), lim)
        delta += a - b
    return int(delta)


def pipeline_reserve_free(snap: BagSnap, cfg: BadaoExchangeConfig) -> int:
    """
    安全预留空格（默认 10），防止卖切糕/组丹时快乐丹溢出、包满丢材料。

    - 优先用 cfg.safety_free_slots，否则 token_reserve_slots
    - 残页不另扣（丹→页会腾格）
    - 这 10 格不参与原料兑换占位计算

    @author by ak
    """
    want = int(getattr(cfg, "safety_free_slots", 0) or 0)
    if want <= 0:
        want = int(getattr(cfg, "token_reserve_slots", 3) or 3)
    return max(0, int(want))



def plan_token_buy_count(
    *,
    pages: int,
    pages_need: int,
    remaining: int,
    token_room: int,
    token_stack_max: int,
    free: int,
    token_slots: int = 0,
) -> int:
    """
    一次可买霸刀牌数：
      min(页能换几张, 目标剩余, 牌栈可收, 单笔上限999)
    @author by ak
    """
    pn = max(1, int(pages_need))
    tm = max(1, int(token_stack_max))
    by_pages = max(0, int(pages) // pn)
    rem = int(remaining)
    if rem >= 10**9:
        by_target = by_pages
    else:
        by_target = max(0, rem)
    room = max(0, int(token_room))
    if room <= 0:
        # 无牌栈：有空位可新开一栈，最多 tm
        room = tm if int(free) >= 1 or int(token_slots) > 0 else 0
        if int(token_slots) > 0 and int(token_room) <= 0 and int(free) >= 1:
            room = tm
    n = min(by_pages, by_target, room, tm)
    return max(0, int(n))


def max_pages_by_slot_transform(
    dan: int,
    pages: int,
    free: int,
    *,
    stack_max: int,
    dan_per_page: int,
    pages_cap: int,
) -> int:
    """
    先验算：最多能买多少残页。

    约束：
      - pages_out <= dan // 2
      - pages_out <= pages_cap（目标剩余 / 单笔上限）
      - 买页后 (丹+页) 占格净增 <= free（丹减少会腾格）

    free=0 时，若 2 栈丹→1 栈页 这类压缩仍可能成功。
    """
    dpp = max(1, int(dan_per_page))
    sm = max(1, int(stack_max))
    hi = min(max(0, int(dan) // dpp), max(0, int(pages_cap)))
    if hi <= 0:
        return 0
    free_i = max(0, int(free))
    # 注意：可行性对 k 非单调——少量买页时丹栈可能还不缩，净占格先升后降。
    # 故从大到小扫，取能放下的最大 pages_out（pages_cap 已含单笔<=9999 硬顶）。
    for k in range(int(hi), 0, -1):
        delta = net_slots_delta(
            before_counts={"dan": int(dan), "page": int(pages)},
            after_counts={
                "dan": int(dan) - k * dpp,
                "page": int(pages) + k,
            },
            stack_max=sm,
        )
        if delta <= free_i:
            return int(k)
    return 0


def max_feed_units_by_slot_transform(
    feed_count: int,
    dan: int,
    free: int,
    *,
    stack_max: int,
    unit_to_dan: int,
    units_cap: int,
    feed_key: str = "feed",
) -> int:
    """
    先验算：最多卖/开多少原料单位 → 快乐丹。
    单位可以是「一组9999兑换丹」个数，或切糕个数。
    同步考虑：原料占格下降 + 丹占格上升，净增 <= free。
    """
    p2d = max(1, int(unit_to_dan))
    sm = max(1, int(stack_max))
    hi = min(max(0, int(feed_count)), max(0, int(units_cap)))
    if hi <= 0:
        return 0
    free_i = max(0, int(free))
    lo, best = 0, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        delta = net_slots_delta(
            before_counts={feed_key: int(feed_count), "dan": int(dan)},
            after_counts={
                feed_key: int(feed_count) - mid,
                "dan": int(dan) + mid * p2d,
            },
            stack_max=sm,
        )
        if delta <= free_i:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return int(best)




def max_dan_receivable_now(
    snap: BagSnap,
    free_work: int,
    *,
    stack_max: int,
) -> int:
    """当前快乐丹还能再收多少个：已有栈 room + free_work 新开栈。"""
    sm = max(1, int(stack_max))
    room = int(getattr(snap.dan, "stack_room", 0) or 0)
    if int(getattr(snap.dan, "slots", 0) or 0) <= 0:
        room = 0
    return int(max_receivable(room, max(0, int(free_work)), sm))


def clamp_feed_units_to_dan_room(
    units: int,
    *,
    unit_to_dan: int,
    max_dan_add: int,
) -> int:
    """硬顶：units * unit_to_dan 不得超过可收丹量，杜绝切糕 400 溢出。"""
    u = max(0, int(units))
    p2d = max(1, int(unit_to_dan))
    cap = max(0, int(max_dan_add))
    if u <= 0 or cap <= 0:
        return 0
    return min(u, cap // p2d)


def max_feed_units_safe(
    snap: BagSnap,
    feed_count: int,
    dan: int,
    free_work: int,
    *,
    stack_max: int,
    unit_to_dan: int,
    units_cap: int,
    feed_key: str = "feed",
) -> int:
    """
    卖原料上限 = min(空位变换, 真·可收丹量/单位产出, units_cap)。
    必须用 stack_room+free 硬顶，避免理想叠放高估导致「卖 400 切糕只进 10001 丹」。
    """
    sm = max(1, int(stack_max))
    p2d = max(1, int(unit_to_dan))
    by_xform = max_feed_units_by_slot_transform(
        int(feed_count),
        int(dan),
        int(free_work),
        stack_max=sm,
        unit_to_dan=p2d,
        units_cap=int(units_cap),
        feed_key=feed_key,
    )
    max_dan_add = max_dan_receivable_now(snap, free_work, stack_max=sm)
    by_room = clamp_feed_units_to_dan_room(
        by_xform, unit_to_dan=p2d, max_dan_add=max_dan_add
    )
    # 再保险：can_receive 逐级回退
    room = int(getattr(snap.dan, "stack_room", 0) or 0)
    if int(getattr(snap.dan, "slots", 0) or 0) <= 0:
        room = 0
    use = int(by_room)
    while use > 0 and not can_receive(use * p2d, room, max(0, int(free_work)), sm):
        use -= 1
    return int(use)


def _ideal_resource(count: int, stack_max: int) -> ResourceSnap:
    """按理想叠放从 count 还原 slots/stack_room（队列模拟用）。"""
    c = max(0, int(count))
    sm = max(1, int(stack_max))
    if c <= 0:
        return ResourceSnap(count=0, slots=0, stack_room=0)
    slots = slots_for_count(c, sm)
    room = max(0, slots * sm - c)
    return ResourceSnap(count=c, slots=slots, stack_room=room)


def bag_snap_from_counts(
    counts: dict,
    free: int,
    cfg: BadaoExchangeConfig | None = None,
) -> BagSnap:
    """用资源计数 + 空位构造轻量 BagSnap，供队列模拟规划。@author by ak"""
    cfg = cfg or BadaoExchangeConfig()
    sm = max(1, int(cfg.stack_max))
    tm = max(1, int(cfg.token_stack_max))
    fre = max(0, int(free))

    def _c(key: str) -> int:
        return max(0, int((counts or {}).get(key, 0) or 0))

    return BagSnap(
        cap=0,
        used=0,
        free=fre,
        carry_free=fre,
        dan=_ideal_resource(_c("dan"), sm),
        page=_ideal_resource(_c("page"), sm),
        token=_ideal_resource(_c("token"), tm),
        qiegao=_ideal_resource(_c("qiegao"), sm),
        pack9999=_ideal_resource(_c("pack9999"), sm),
        pack100=_ideal_resource(_c("pack100"), sm),
    )


def apply_op_expect_delta(
    counts: dict,
    free: int,
    op: "TradeOp",
    cfg: BadaoExchangeConfig | None = None,
) -> tuple[dict, int]:
    """
    将一笔 op 的 expect_delta 应用到计数，并按理想叠放更新 free。
    买残页扣丹腾格 → free 上升 → 下一条才能再卖组丹（队列衔接）。
    """
    cfg = cfg or BadaoExchangeConfig()
    sm = max(1, int(cfg.stack_max))
    tm = max(1, int(cfg.token_stack_max))
    keys = ("dan", "page", "token", "qiegao", "pack9999", "pack100")
    before = {k: max(0, int((counts or {}).get(k, 0) or 0)) for k in keys}
    after = dict(before)
    for k, v in dict(getattr(op, "expect_delta", None) or {}).items():
        if k in after:
            after[k] = max(0, int(after[k]) + int(v))
    slot_delta = net_slots_delta(
        before_counts=before,
        after_counts=after,
        stack_max=sm,
        token_stack_max=tm,
    )
    new_free = max(0, int(free) - int(slot_delta))
    return after, new_free


def plan_pipeline_queue(
    snap: BagSnap,
    cfg: BadaoExchangeConfig,
    *,
    tokens_gained: int = 0,
    max_ops: int = 12,
) -> deque:
    """
    进队：在模拟背包上连续 plan_next_op。

    - 规划侧只负责进队；执行侧只出队交易
    - **单批队列不混阶段**：阶段A（东方卖丹/换页）与阶段B（不败出牌）分批，
      避免中途反复切 NPC
    - 阶段A内：买页腾格后可继续卖组丹（同店衔接）
    - 真机每笔成功后应清空队列并按实包重规划（防模拟漂移）

    @author by ak
    """
    cfg = cfg or BadaoExchangeConfig()
    q: deque = deque()
    cc = carry_resource_counts(snap, cfg)
    counts = {
        "dan": int(cc.get("dan", 0) or 0),
        "page": int(cc.get("page", 0) or 0),
        "token": int(cc.get("token", 0) or 0),
        "qiegao": int(cc.get("qiegao", 0) or 0),
        "pack9999": int(cc.get("pack9999", 0) or 0),
        "pack100": int(cc.get("pack100", 0) or 0),
    }
    free = int(getattr(snap, "carry_free", 0) or 0)
    if free <= 0:
        free = int(getattr(snap, "free", 0) or 0)
    gained = max(0, int(tokens_gained or 0))
    max_n = max(1, int(max_ops or 12))
    batch_phase: str | None = None  # "A"=东方业务, "B"=不败出牌

    for _ in range(max_n):
        sim_snap = bag_snap_from_counts(counts, free, cfg)
        op = plan_next_op(sim_snap, cfg, tokens_gained=gained)
        kind = str(getattr(op, "kind", "") or "")
        if kind in (OP_DONE, OP_PAUSE):
            # 队列空时把终态也放进去，方便 UI；已有活则停
            if not q:
                q.append(op)
            break
        cur_phase = "B" if kind == OP_TOKEN else "A"
        if batch_phase is None:
            batch_phase = cur_phase
        elif cur_phase != batch_phase:
            # 阶段切换留给下一轮重规划（先把当前业务跑完再换店）
            break
        q.append(op)
        counts, free = apply_op_expect_delta(counts, free, op, cfg)
        if kind == OP_TOKEN:
            gained += max(1, int(getattr(op, "count", 1) or 1))
        # 无 delta 的异常 op 避免死循环
        if not dict(getattr(op, "expect_delta", None) or {}):
            break
    return q


def plan_next_op(
    snap: BagSnap,
    cfg: BadaoExchangeConfig,
    *,
    tokens_gained: int = 0,
) -> TradeOp:
    pages_need = max(1, int(cfg.pages_per_token))
    dpp = max(1, int(cfg.dan_per_page))
    q2d = max(1, int(cfg.qiegao_to_dan))
    sm = max(1, int(cfg.stack_max))
    tm = max(1, int(cfg.token_stack_max))
    batch = max(1, int(cfg.page_batch_max))

    # 规划仅用随身（主+扩）；仓库原料需用户手动取到随身后再扫
    cc = carry_resource_counts(snap, cfg)
    dan = int(cc.get("dan", 0) or 0)
    pages = int(cc.get("page", 0) or 0)
    tokens = int(cc.get("token", 0) or 0)
    pack100 = int(cc.get("pack100", 0) or 0)
    qiegao = int(cc.get("qiegao", 0) or 0)
    pack9999 = int(cc.get("pack9999", 0) or 0)
    free = int(getattr(snap, "carry_free", 0) or 0)
    if free <= 0:
        free = int(snap.free or 0)
    reserve = pipeline_reserve_free(snap, cfg)
    # 流水线可用空位 = 总空 - 霸刀预留（残页不另扣）
    free_work = max(0, free - reserve)

    token_room_count = int(snap.token.stack_room)
    token_can = token_room_count > 0 or free >= 1
    if snap.token.slots <= 0:
        token_can = free >= 1
        token_room_count = tm if token_can else 0
    elif token_room_count <= 0 and free >= 1:
        token_room_count = tm
        token_can = True

    # 目标牌数：0=全换；>0 本轮出牌上限
    target = max(0, int(getattr(cfg, "target_tokens", 0) or 0))
    gained = max(0, int(tokens_gained or 0))
    if target > 0 and gained >= target:
        return TradeOp(
            kind=OP_DONE,
            count=0,
            reason=f"达到目标牌数 {gained}/{target}",
        )
    remaining = (target - gained) if target > 0 else 10**9

    max_buy = max(1, int(getattr(cfg, "max_buy_count", 0) or sm))
    # 残页单笔上限 9999（live 超限会无法使用NPC）；多次买直到丹耗尽
    page_buy_max = max(
        1,
        int(getattr(cfg, "page_buy_max", 0) or getattr(cfg, "page_batch_max", 0) or sm),
    )
    # 硬顶栈上限 9999，禁止再发 65535
    page_trade_cap = min(int(batch), int(page_buy_max), int(sm), 9999)
    p2d = max(1, int(getattr(cfg, "pack9999_to_dan", 0) or PACK9999_TO_DAN))

    # 目标还差多少页（仅阶段2出牌时用；阶段1不因此截断换页）
    if target > 0:
        pages_still = max(0, int(remaining) * int(pages_need) - int(pages))
    else:
        pages_still = 10**9

    # ---------- 决策总序（东方/不败分业 + 空位压力分流）----------
    # 1) 有丹能换页 → 永远先买书页（同店压缩，优先于出牌/卖切糕）
    # 2) 压到安全格且换不动页、页够 → 买霸刀腾格（切糕会持续补丹，不能等丹空）
    # 3) 空位尚宽 → 卖组丹/切糕备丹，再回到 1)
    # 4) A 推不动且页够 → 阶段B出牌
    has_feed = (
        pack9999 > 0
        or qiegao > 0
        or (pack100 > 0 and bool(getattr(cfg, "auto_open_packs", True)))
    )
    phase1_materials = (dan >= dpp) or has_feed
    pressure = max(
        0,
        int(
            getattr(cfg, "free_pressure_slots", 0)
            or getattr(cfg, "safety_free_slots", 3)
            or 3
        ),
    )
    # free<=安全线 或 工作空位用尽 → 压力模式（丹未必空）
    space_tight = int(free) <= int(pressure) or int(free_work) <= 0
    tok_tid = int(getattr(cfg, "goods_tid_token", 0) or 0) or int(
        getattr(cfg, "token_tid", 0) or 0
    )
    can_mint_token = (
        pages >= pages_need
        and tok_tid > 0
        and token_can
        and token_room_count > 0
        and remaining > 0
    )

    # ---------- ① 买书页（最高优先，含压力模式）----------
    # 有丹或原料时，换页上限=单笔满组，不被「目标页数」提前掐断
    if phase1_materials:
        pages_cap = int(page_trade_cap)
    else:
        pages_cap = min(int(pages_still), int(page_trade_cap)) if pages_still > 0 else 0

    # free_work=0 仍允许买页：2 丹栈→1 页栈可压缩（先验算）
    pages_out = max_pages_by_slot_transform(
        dan,
        pages,
        free_work,
        stack_max=sm,
        dan_per_page=dpp,
        pages_cap=max(0, int(pages_cap)),
    )
    # 双保险：规划结果再 clamp，杜绝配置误写/旧路径打出 >9999
    pages_out = min(int(pages_out), int(page_trade_cap), 9999)
    if pages_out > 0:
        dan_cost = pages_out * dpp
        why = "压力换页" if space_tight else "阶段A换页"
        return TradeOp(
            kind=OP_PAGE,
            count=pages_out,
            reason=(
                f"[{why}] dan={dan} -> pages+{pages_out}"
                f" free={free}(实扫) reserve={reserve} work={free_work}"
                f" pressure={pressure}"
                + (f" pages_still_target={pages_still}" if target > 0 else "")
            ),
            goods_tid=int(cfg.goods_tid_page or 0),
            goods_index=int(getattr(cfg, "goods_index_page", 7) or 7),
            expect_delta={"dan": -dan_cost, "page": pages_out},
        )

    # ---------- ② 空位压力且换不动页：出牌腾格（允许还有切糕/组丹）----------
    # 切糕会一直补丹 → 不能死等「丹空」才出牌；但有丹能换页时已在①处理
    if space_tight and can_mint_token:
        n_tok = plan_token_buy_count(
            pages=pages,
            pages_need=pages_need,
            remaining=remaining,
            token_room=token_room_count,
            token_stack_max=tm,
            free=free,
            token_slots=int(snap.token.slots or 0),
        )
        if n_tok > 0:
            return TradeOp(
                kind=OP_TOKEN,
                count=n_tok,
                reason=(
                    f"[压力出牌] x{n_tok} pages={pages}>={pages_need}"
                    f" free={free}<=pressure={pressure} work={free_work}"
                    f" 换页不可行，腾格后续再卖切糕/组丹"
                    f" dan={dan} qiegao={qiegao} pack={pack9999}"
                    f" remaining_tokens={remaining if target else 'all'}"
                ),
                goods_tid=tok_tid,
                goods_index=int(getattr(cfg, "goods_index_token", 0) or 0),
                expect_delta={"page": -pages_need * n_tok, "token": n_tok},
            )

    # ---------- ③ 备丹：空位尚宽才卖切糕/组丹（压力模式禁止再灌丹）----------
    # 阶段A原料→丹：按「空位能装多少丹」备料，不再死卡一波 9999页≈400切糕
    # 硬顶仍走 max_feed_units_safe（stack_room + free_work），防溢出
    max_dan_add_now = max_dan_receivable_now(snap, free_work, stack_max=sm)
    if phase1_materials:
        one_wave_dan = int(page_trade_cap) * int(dpp)  # 19998，下限一波
        # 空位多就多备：可收丹量；至少一波需求（已有丹要扣掉）
        want_dan = max(int(one_wave_dan), int(max_dan_add_now) + int(dan))
        # 但最终「还要买的丹」不能超过当前包还能塞进的量
        dan_still = max(0, min(int(want_dan) - int(dan), int(max_dan_add_now)))
        # 若可收为 0 但理论还需要，dan_still=0（后面腾格）
        pages_goal = max(1, (int(dan) + int(dan_still)) // int(dpp)) if dpp else 0
    else:
        pages_goal = min(int(pages_still), int(page_trade_cap)) if pages_still > 0 else 0
        dan_still = max(0, min(int(pages_goal) * int(dpp) - int(dan), int(max_dan_add_now)))

    max_page_in = pages_cap
    free_feed = free_work
    # 压力模式下：若已能出牌会在上面返回；此处仅在「页不够且还有一点 work」时才允许少量备丹
    allow_feed = (not space_tight) or (
        int(free_work) > 0 and (not can_mint_token) and int(dan_still) > 0
    )

    if allow_feed and pack9999 > 0 and dan_still > 0:
        max_p9 = max(1, int(getattr(cfg, "max_pack9999_per_trade", 0) or sm))
        need_packs = (int(dan_still) + p2d - 1) // p2d
        units_cap = min(int(need_packs), max_p9, int(sm))
        use = max_feed_units_safe(
            snap,
            int(pack9999),
            int(dan),
            free_work,
            stack_max=sm,
            unit_to_dan=p2d,
            units_cap=units_cap,
            feed_key="pack9999",
        )
        if use > 0:
            dan_out = use * p2d
            it = _pick_sell_item(snap, "pack9999", cfg)
            if it is not None:
                # 只卖「算得动」的数量，绝不能改成整栈 400 导致溢出
                use = min(use, max(1, int(getattr(it, "count", 1) or 1)))
                # 再按可收丹硬顶一次（叠放 room 可能小于 count 推算）
                use = clamp_feed_units_to_dan_room(
                    use,
                    unit_to_dan=p2d,
                    max_dan_add=max_dan_receivable_now(snap, free_work, stack_max=sm),
                )
                dan_out = use * p2d
            return TradeOp(
                kind=OP_PACK9999,
                count=use,
                reason=(
                    f"[阶段1备丹] 卖组丹9999 x{use} -> dan+{dan_out}"
                    f" need_dan={dan_still} max_dan_add={max_dan_add_now}"
                    f" free={free} reserve={reserve} work={free_work}"
                    f" 包{getattr(it, 'package', -1)}槽{getattr(it, 'slot', -1)}"
                ),
                goods_tid=int(getattr(cfg, "goods_tid_pack9999", 0) or 0),
                goods_index=int(getattr(cfg, "goods_index_pack9999", 0) or 0),
                package=int(getattr(it, "package", -1)) if it else -1,
                slot=int(getattr(it, "slot", -1)) if it else -1,
                pack_kind="pack9999",
                expect_delta={"pack9999": -use, "dan": dan_out},
            )
        # 装不下就不要 PAUSE 卡死：交给阶段B出牌 / PAUSE

    # 4) 快乐兑换丹*100 UseItem 开包（目标够了就别再开）
    if allow_feed and bool(getattr(cfg, "auto_open_packs", True)) and pack100 > 0 and dan_still > 0:
        # *100 开包：占格变化近似 pack100-1 + dan+100
        open_ok = (
            max_feed_units_by_slot_transform(
                1,
                int(dan),
                free_work,
                stack_max=sm,
                unit_to_dan=int(PACK_100_DAN),
                units_cap=1,
                feed_key="pack100",
            )
            >= 1
        )
        # pack100 可能不在 feed 计数模型里（与 pack9999 分栈）；用 can_receive 兜底
        dan_room = int(snap.dan.stack_room) if int(snap.dan.slots or 0) > 0 else 0
        if open_ok or can_receive(PACK_100_DAN, dan_room, free_work, sm):
            pack100_res = getattr(snap, "pack100", ResourceSnap())
            carry_pkgs = _carry_pkg_set(cfg)
            items = [
                it
                for it in list(getattr(pack100_res, "items", None) or [])
                if int(getattr(it, "package", -1) or -1) in carry_pkgs
            ]
            it = items[0] if items else None
            if it is not None or not list(getattr(pack100_res, "items", None) or []):
                return TradeOp(
                    kind=OP_OPEN,
                    count=1,
                    reason=f"open pack100 -> dan+{PACK_100_DAN}",
                    package=int(getattr(it, "package", -1)) if it else -1,
                    slot=int(getattr(it, "slot", -1)) if it else -1,
                    pack_kind="pack100",
                    expect_delta={"pack100": -1, "dan": PACK_100_DAN},
                )
        elif dan < dpp:
            return TradeOp(
                kind=OP_PAUSE,
                reason="包满：快乐兑换丹*100 无法开包（空闲格不足）",
            )

    # 5) 切糕 -> 丹（同样按需 + 空位变换）
    if allow_feed and qiegao > 0 and dan_still > 0:
        max_qg_cfg = max(1, int(getattr(cfg, "max_qiegao_per_trade", 0) or sm))
        need_qg = (int(dan_still) + q2d - 1) // q2d
        units_cap = min(int(need_qg), max_qg_cfg, int(sm), int(qiegao))
        use = max_feed_units_safe(
            snap,
            int(qiegao),
            int(dan),
            free_work,
            stack_max=sm,
            unit_to_dan=q2d,
            units_cap=units_cap,
            feed_key="qiegao",
        )
        if use > 0:
            dan_out = use * q2d
            it = _pick_sell_item(snap, "qiegao", cfg)
            if it is not None:
                use = min(use, max(1, int(getattr(it, "count", 1) or 1)))
                use = clamp_feed_units_to_dan_room(
                    use,
                    unit_to_dan=q2d,
                    max_dan_add=max_dan_receivable_now(snap, free_work, stack_max=sm),
                )
                dan_out = use * q2d
            if use <= 0:
                pass  # fall through pause
            else:
                return TradeOp(
                    kind=OP_QIEGAO,
                    count=use,
                    reason=(
                        f"[阶段1备丹] 卖切糕 x{use} -> dan+{dan_out}"
                        f" need_dan={dan_still} max_dan_add="
                        f"{max_dan_receivable_now(snap, free_work, stack_max=sm)}"
                        f" free={free} reserve={reserve} work={free_work}"
                        f" 包{getattr(it, 'package', -1)}槽{getattr(it, 'slot', -1)}"
                    ),
                    goods_tid=int(cfg.goods_tid_qiegao or 0),
                    goods_index=int(getattr(cfg, "goods_index_qiegao", 0) or 0),
                    package=int(getattr(it, "package", -1)) if it else -1,
                    slot=int(getattr(it, "slot", -1)) if it else -1,
                    pack_kind="qiegao",
                    expect_delta={"qiegao": -use, "dan": dan_out},
                )
        # 切糕装不下：不在这里 PAUSE，交给阶段B出牌 / PAUSE

    # ---------- ④ 阶段B：空位尚宽、A 推不动时出牌（不败姑娘）----------
    # 注意：有丹能换页绝不会落到这里（已在①返回）；压力出牌在②
    phase1_can_progress = False
    if dan >= dpp:
        if max_pages_by_slot_transform(
            dan, pages, free_work, stack_max=sm, dan_per_page=dpp, pages_cap=1
        ) > 0:
            phase1_can_progress = True
    if not phase1_can_progress and has_feed and dan_still > 0 and allow_feed:
        if pack9999 > 0:
            if max_feed_units_by_slot_transform(
                pack9999, dan, free_work, stack_max=sm, unit_to_dan=p2d, units_cap=1, feed_key="pack9999"
            ) > 0:
                phase1_can_progress = True
        if qiegao > 0 and not phase1_can_progress:
            if max_feed_units_by_slot_transform(
                qiegao, dan, free_work, stack_max=sm, unit_to_dan=q2d, units_cap=1, feed_key="qiegao"
            ) > 0:
                phase1_can_progress = True
    if can_mint_token and not phase1_can_progress:
        n_tok = plan_token_buy_count(
            pages=pages,
            pages_need=pages_need,
            remaining=remaining,
            token_room=token_room_count,
            token_stack_max=tm,
            free=free,
            token_slots=int(snap.token.slots or 0),
        )
        if n_tok > 0:
            return TradeOp(
                kind=OP_TOKEN,
                count=n_tok,
                reason=(
                    f"[阶段B出牌] x{n_tok} pages={pages}>={pages_need}"
                    f" free={free} work={free_work} pressure={pressure}"
                    f" remaining_tokens={remaining if target else 'all'}"
                    f" dan={dan} pack={pack9999}+{pack100} qiegao={qiegao}"
                ),
                goods_tid=tok_tid,
                goods_index=int(getattr(cfg, "goods_index_token", 0) or 0),
                expect_delta={"page": -pages_need * n_tok, "token": n_tok},
            )

    convertible = (
        dan >= dpp
        or qiegao > 0
        or can_mint_token
        or pack9999 > 0
        or pack100 > 0
    )
    if not convertible:
        return TradeOp(
            kind=OP_DONE,
            count=0,
            reason=(
                f"完成：无可转化资源 dan={dan} page={pages} "
                f"qiegao={qiegao} pack={pack9999}+{pack100} token={tokens}"
            ),
        )
    hint = ""
    if pages >= pages_need and remaining > 0:
        hint = "；残页已够，请开【不败姑娘】换霸刀腾格"
    return TradeOp(
        kind=OP_PAUSE,
        count=0,
        reason=(
            f"暂停：无法推进 dan={dan} page={pages} free={free}(实扫) "
            f"qiegao={qiegao} pack={pack9999}+{pack100} "
            f"phase1={phase1_can_progress}{hint}"
        ),
    )


def materials_locations(snap: BagSnap) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {
        "dan": [],
        "page": [],
        "token": [],
        "qiegao": [],
        "pack9999": [],
        "pack100": [],
    }
    for key in out:
        res = getattr(snap, key, None)
        pkgs = sorted(
            {
                int(getattr(it, "package", 0) or 0)
                for it in (getattr(res, "items", None) or [])
            }
        )
        out[key] = pkgs
    return out


def plan_pull_to_carry(snap: BagSnap, cfg: BadaoExchangeConfig) -> TradeOp | None:
    """仓库由用户手动取到随身；脚本不自动拉仓、不提示门徒技能。始终返回 None。"""
    return None


def _pick_sell_item(
    snap: BagSnap,
    kind: str,
    cfg: BadaoExchangeConfig,
) -> "PackageItem | None":
    """只选随身(主+扩)栈；仓库栈忽略（用户手动取出后再卖）。"""
    res = getattr(snap, kind, None)
    items = list(getattr(res, "items", None) or [])
    if not items:
        return None
    carry = _carry_pkg_set(cfg)
    for it in items:
        if int(getattr(it, "package", -1) or -1) in carry:
            return it
    return None


def _carry_pkg_set(cfg: BadaoExchangeConfig) -> set[int]:
    return {
        int(x)
        for x in (
            getattr(cfg, "carry_indexes", None) or DEFAULT_CARRY_PACKAGE_INDEXES
        )
    }


def _filter_resource_pkgs(res: ResourceSnap, pkgs: set[int]) -> ResourceSnap:
    """Subset of a ResourceSnap whose items sit in pkgs. @author by ak"""
    items = [
        it
        for it in list(getattr(res, "items", None) or [])
        if int(getattr(it, "package", -1) or -1) in pkgs
    ]
    total = sum(max(0, int(getattr(it, "count", 0) or 0)) for it in items)
    # stack_room not recomputed precisely without stack_max; use count-only planning
    return ResourceSnap(
        count=int(total),
        slots=len(items),
        stack_room=int(getattr(res, "stack_room", 0) or 0),
        bound_count=0,
        bound_slots=0,
        items=items,
    )


def carry_resource_counts(
    snap: BagSnap, cfg: BadaoExchangeConfig
) -> dict[str, int]:
    """随身(主+扩)可兑换原料计数（不含仓库）。@author by ak"""
    carry = _carry_pkg_set(cfg)
    out: dict[str, int] = {}
    for key in ("dan", "page", "token", "qiegao", "pack9999", "pack100"):
        res = getattr(snap, key, ResourceSnap())
        items = list(getattr(res, "items", None) or [])
        if not items and int(getattr(res, "count", 0) or 0) > 0:
            # mock snaps often omit items — treat count as随身
            out[key] = int(res.count or 0)
            continue
        out[key] = sum(
            max(0, int(getattr(it, "count", 0) or 0))
            for it in items
            if int(getattr(it, "package", -1) or -1) in carry
        )
    return out


def warehouse_resource_counts(
    snap: BagSnap, cfg: BadaoExchangeConfig | None = None
) -> dict[str, int]:
    """仓库原料计数。@author by ak"""
    wh = int(PACKAGE_INDEX_WAREHOUSE)
    out: dict[str, int] = {}
    for key in ("dan", "page", "token", "qiegao", "pack9999", "pack100"):
        res = getattr(snap, key, ResourceSnap())
        items = list(getattr(res, "items", None) or [])
        out[key] = sum(
            max(0, int(getattr(it, "count", 0) or 0))
            for it in items
            if int(getattr(it, "package", -1) or -1) == wh
        )
    return out


def estimate_token_capacity(
    snap: BagSnap,
    cfg: BadaoExchangeConfig | None = None,
    *,
    scope: str = "all",
) -> dict:
    """
    预计可换霸刀牌数（理论，不扣空位损耗）。

    scope: "all" | "carry" | "warehouse"
    公式：总丹当量 = 丹 + 切糕*50 + 组丹9999*9999 + *100*100
          总页 = 页 + 总丹//2
          牌 = 总页 // pages_per_token

    @author by ak
    """
    cfg = cfg or BadaoExchangeConfig()
    pages_need = max(1, int(cfg.pages_per_token or PAGES_PER_TOKEN))
    dpp = max(1, int(cfg.dan_per_page or DAN_PER_PAGE))
    q2d = max(1, int(cfg.qiegao_to_dan or QIEGAO_TO_DAN))
    p9999 = max(1, int(getattr(cfg, "pack9999_to_dan", 0) or PACK9999_TO_DAN))
    p100 = int(PACK_100_DAN)

    carry = carry_resource_counts(snap, cfg)
    wh = warehouse_resource_counts(snap, cfg)
    # 无 items 的 mock：all 用 snap 总量
    has_items = any(
        list(getattr(getattr(snap, k, None), "items", None) or [])
        for k in ("dan", "page", "qiegao", "pack9999", "pack100", "token")
    )
    if not has_items:
        total = {
            "dan": int(snap.dan.count),
            "page": int(snap.page.count),
            "token": int(snap.token.count),
            "qiegao": int(snap.qiegao.count),
            "pack9999": int(getattr(snap, "pack9999", ResourceSnap()).count or 0),
            "pack100": int(getattr(snap, "pack100", ResourceSnap()).count or 0),
        }
        carry = dict(total)
        wh = {k: 0 for k in total}

    def _pick(src: dict) -> dict:
        return {k: int(src.get(k, 0) or 0) for k in ("dan", "page", "qiegao", "pack9999", "pack100", "token")}

    if scope == "carry":
        mat = _pick(carry)
    elif scope == "warehouse":
        mat = _pick(wh)
    else:
        if has_items:
            mat = {
                k: int(carry.get(k, 0) or 0) + int(wh.get(k, 0) or 0)
                for k in ("dan", "page", "qiegao", "pack9999", "pack100", "token")
            }
        else:
            mat = _pick(carry)

    dan_eq = (
        int(mat["dan"])
        + int(mat["qiegao"]) * q2d
        + int(mat["pack9999"]) * p9999
        + int(mat["pack100"]) * p100
    )
    pages_from_dan = dan_eq // dpp
    pages_total = int(mat["page"]) + pages_from_dan
    tokens = pages_total // pages_need
    rem_pages = pages_total % pages_need
    rem_dan = dan_eq % dpp
    return {
        "tokens": int(tokens),
        "pages_total": int(pages_total),
        "pages_need": int(pages_need),
        "dan_eq": int(dan_eq),
        "remain_pages": int(rem_pages),
        "remain_dan": int(rem_dan),
        "materials": mat,
        "carry": _pick(carry) if has_items or scope != "all" else _pick(carry),
        "warehouse": _pick(wh),
        "scope": scope,
    }


def format_progress_line(
    snap: BagSnap,
    cfg: BadaoExchangeConfig,
    *,
    tokens_gained: int = 0,
    op_kind: str = "-",
) -> str:
    """精简进度：已换/应换，预计还可换。@author by ak"""
    gained = max(0, int(tokens_gained or 0))
    tgt = max(0, int(getattr(cfg, "target_tokens", 0) or 0))
    # 预计只算随身可转化原料（仓库需用户手动取出后才计入）
    est = int(estimate_token_capacity(snap, cfg, scope="carry").get("tokens") or 0)
    # 应换：填了目标用目标；0=全换时用「已换+剩余」作应换
    should = tgt if tgt > 0 else (gained + est)
    # 预计还可换：还剩原料能换几张；有目标时不超过目标剩余
    remain = est
    if tgt > 0:
        remain = min(est, max(0, tgt - gained))
    return f"已换 {gained}/{should}，预计还可换 {remain}"


def describe_op_action(op: TradeOp | None, cfg: BadaoExchangeConfig | None = None) -> str:
    """把 op 翻成一眼能懂的中文动作（不含进度）。@author by ak"""
    cfg = cfg or BadaoExchangeConfig()
    if op is None:
        return "待命"
    kind = str(getattr(op, "kind", "") or "")
    cnt = max(0, int(getattr(op, "count", 0) or 0))
    npc = npc_name_for_op(cfg, kind) if kind not in (OP_DONE, OP_PAUSE, OP_OPEN, OP_MOVE, "") else ""
    if kind == OP_PAGE:
        return f"{npc or '东方姑娘'}·买残页×{cnt}"
    if kind == OP_TOKEN:
        reason = str(getattr(op, "reason", "") or "")
        tag = "补位" if "压力" in reason else "换牌"
        return f"{npc or '不败姑娘'}·{tag}×{cnt}"
    if kind == OP_QIEGAO:
        return f"{npc or '东方姑娘'}·卖切糕×{cnt}→快乐丹"
    if kind == OP_PACK9999:
        return f"{npc or '东方姑娘'}·卖组丹×{cnt}→快乐丹"
    if kind == OP_OPEN:
        return "开快乐丹礼包"
    if kind == OP_PAUSE:
        return f"暂停·{str(getattr(op, 'reason', '') or '等待')[:36]}"
    if kind == OP_DONE:
        return f"完成·{str(getattr(op, 'reason', '') or '结束')[:36]}"
    if kind == OP_MOVE:
        return "请手动：仓库→随身"
    return f"{kind}×{cnt}"


def _fmt_count(n: int) -> str:
    """Compact count for status bar (e.g. 12.3万). @author by ak"""
    try:
        v = int(n or 0)
    except Exception:
        return "0"
    if abs(v) >= 100000:
        return f"{v / 10000:.1f}万".replace(".0万", "万")
    if abs(v) >= 10000:
        return f"{v / 10000:.1f}万".replace(".0万", "万")
    return str(v)


def format_bag_brief(snap: BagSnap | None) -> str:
    """背包关键数一眼看完。@author by ak"""
    if snap is None:
        return ""
    try:
        free = int(getattr(snap, "carry_free", 0) or 0) or int(getattr(snap, "free", 0) or 0)
        dan = int(getattr(getattr(snap, "dan", None), "count", 0) or 0)
        page = int(getattr(getattr(snap, "page", None), "count", 0) or 0)
        tok = int(getattr(getattr(snap, "token", None), "count", 0) or 0)
        qg = int(getattr(getattr(snap, "qiegao", None), "count", 0) or 0)
        p9 = int(getattr(getattr(snap, "pack9999", None), "count", 0) or 0)
        return (
            f"空{free} 丹{_fmt_count(dan)} 页{_fmt_count(page)} "
            f"牌{_fmt_count(tok)} 糕{_fmt_count(qg)} 组{_fmt_count(p9)}"
        )
    except Exception:
        return ""


def format_action_status(
    snap: BagSnap | None,
    cfg: BadaoExchangeConfig,
    op: TradeOp | None,
    *,
    tokens_gained: int = 0,
) -> str:
    """
    面板主状态：动作 + 进度 + 简要背包。
    例：东方姑娘·买残页×9999 · 已换 3/100 · 空13 丹1.2万 页9.8万
    """
    action = describe_op_action(op, cfg)
    try:
        prog = format_progress_line(
            snap or BagSnap(),
            cfg,
            tokens_gained=tokens_gained,
            op_kind=str(getattr(op, "kind", "-") if op else "-"),
        )
    except Exception:
        prog = f"已换 {int(tokens_gained or 0)}/?"
    brief = format_bag_brief(snap)
    parts = [action, prog]
    if brief:
        parts.append(brief)
    return " · ".join(parts)


def _npc_ptr(hit) -> int:
    for attr in ("address", "ptr", "obj", "base"):
        v = getattr(hit, attr, None)
        if v:
            try:
                return int(v)
            except Exception:
                pass
    return 0


def find_npc_by_name(
    session: GameAttachSession,
    name_key: str,
    *,
    radius: float = 35.0,
    log: LogFn | None = None,
) -> tuple[int, str]:
    """Find nearby NPC by name substring. @author by ak"""
    log = log or (lambda _m: None)
    name_key = (name_key or "").strip()
    if not name_key:
        return 0, "npc name empty"
    try:
        from app.core.entity_scan import KIND_NPC, scan_nearby_entities

        hits = scan_nearby_entities(
            session,
            kinds={KIND_NPC},
            radius=float(radius),
            limit=80,
            require_pos=False,
            log=log,
        )
    except Exception as e:
        log(f"badao: scan npc err: {e}")
        return 0, f"scan_err:{e}"

    best = None
    for h in hits or []:
        n = (getattr(h, "name", None) or "").strip()
        if name_key in n:
            best = h
            break
    if best is None:
        return 0, f"nearby no {name_key}"
    ptr = _npc_ptr(best)
    try:
        from app.core.plg_interact import get_object_id64

        nid = int(get_object_id64(session, ptr) or 0) if ptr else 0
    except Exception:
        nid = int(getattr(best, "id64", 0) or getattr(best, "id", 0) or 0)
    if nid <= 0:
        return 0, f"npc id missing name={getattr(best, 'name', '')}"
    return nid, f"npc={getattr(best, 'name', name_key)} id={nid}"


def find_dongfang_npc(
    session: GameAttachSession,
    cfg: BadaoExchangeConfig,
    *,
    log: LogFn | None = None,
) -> tuple[int, str]:
    """兼容旧名：找残页店 NPC（东方姑娘）。"""
    name = (
        getattr(cfg, "npc_name_page", None)
        or getattr(cfg, "npc_name", None)
        or "东方姑娘"
    )
    return find_npc_by_name(
        session, str(name), radius=float(cfg.npc_radius), log=log
    )


def npc_name_for_op(cfg: BadaoExchangeConfig, op_kind: str) -> str:
    """TOKEN → 不败姑娘；其余兑换 → 东方姑娘。@author by ak"""
    if op_kind == OP_TOKEN:
        return (
            getattr(cfg, "npc_name_token", None) or "不败姑娘"
        ).strip() or "不败姑娘"
    return (
        getattr(cfg, "npc_name_page", None)
        or getattr(cfg, "npc_name", None)
        or "东方姑娘"
    ).strip() or "东方姑娘"


# 商店/兑换 UI（卖/买均依赖服务会话；仅 NPCSayHello 不够）
SHOP_DLG_NAMES: tuple[str, ...] = ("Win_Shop", "Win_Trade", "Win_ShopBack")
NPC_SERVICE_DLG_NAMES: tuple[str, ...] = (
    "Win_NPCContent",
    "Win_NPC",
    "Win_NPCTalk",
    "Win_NPCTemplate",
)
# 服务菜单关键字（优先完整「兑换商店」；几何默认点第一格）
SERVICE_OPTION_KEYWORDS: tuple[str, ...] = (
    "兑换商店",  # 不败第一格：有霸刀牌
    "兑换",
    "商店",
    "交易",
    "杂货",
    "购买",
    "道具",
    "物品",
    "买卖",
)


def _resolve_hwnd(session: GameAttachSession, hwnd: int = 0) -> int:
    """Prefer explicit hwnd, then session/mounted, then pid main window."""
    h = int(hwnd or 0)
    if h:
        return h
    for attr in ("hwnd", "main_hwnd", "window_hwnd"):
        try:
            h = int(getattr(session, attr, 0) or 0)
        except Exception:
            h = 0
        if h:
            return h
    try:
        from app.core.inject_gate import find_main_hwnd_for_pid

        pid = int(getattr(session, "pid", 0) or 0)
        if pid:
            found = find_main_hwnd_for_pid(pid)
            if isinstance(found, tuple):
                return int(found[0] or 0)
            return int(found or 0)
    except Exception:
        pass
    return 0


def is_shop_dialog_open(
    session: GameAttachSession, *, log: LogFn | None = None
) -> tuple[bool, str]:
    """True if Win_Shop / Win_Trade 类兑换商店界面已显示。"""
    log = log or (lambda _m: None)
    try:
        from app.core.plg_ui import find_shown_dialog

        hit = find_shown_dialog(session, SHOP_DLG_NAMES, log=log)
        if hit is not None and getattr(hit, "shown", False):
            return True, str(getattr(hit, "name", "") or "shop")
    except Exception as e:
        log(f"badao: shop dlg probe err: {e}")
    return False, ""


def cur_serv_npc_id(
    session: GameAttachSession, *, log: LogFn | None = None
) -> int:
    """plg::GetHostPlayerCurServNPC() best-effort."""
    try:
        from app.core.portal_service import cur_serv_npc_id as _cur

        return int(_cur(session, log=log) or 0)
    except Exception:
        return 0


def _shop_service_ready(
    session: GameAttachSession,
    npc_id: int,
    cfg: BadaoExchangeConfig,
    *,
    log: LogFn | None = None,
) -> tuple[bool, str]:
    """
    判定是否可安全 BuyItem：
    - require_shop_dlg: 必须看到商店窗；
    - 否则至少 CurServNPC 非 0。

    注意：CurServNPC 与场景实体 id 可能不在同一 id 空间（live: entity=0x1..119
    而 cur 低 32 曾读成 409），不能用 cur==entity 判失败。
    """
    log = log or (lambda _m: None)
    shop_ok, shop_name = is_shop_dialog_open(session, log=log)
    cur = cur_serv_npc_id(session, log=log)
    if bool(getattr(cfg, "require_shop_dlg", True)):
        if shop_ok:
            return True, f"shop={shop_name} cur={cur} entity={npc_id}"
        return False, f"商店未开(Win_Shop/Trade) cur={cur} entity={npc_id}"
    if shop_ok or (cur and int(cur) > 0):
        return True, f"shop={shop_name or '-'} cur={cur} entity={npc_id}"
    return False, f"无服务会话 shop=- cur={cur} entity={npc_id}"


def _ids_match_npc(a: int, b: int) -> bool:
    a, b = int(a or 0), int(b or 0)
    if a <= 0 or b <= 0:
        return False
    if a == b:
        return True
    return (a & 0xFFFFFFFF) == (b & 0xFFFFFFFF)


def _likely_same_npc_session(cur: int, entity: int) -> bool:
    """
    cur/entity 是否像「同一 NPC 的不同 id 空间」而非两个不同 NPC。

    live：
      - 同 NPC：entity=7205759…8345 而 cur=409（小 id）→ 可复用 cur
      - 不同 NPC：entity=…8388(不败) cur=…8345(东方) 都是大 id 且不相等 → 必须切换
    """
    cur, entity = int(cur or 0), int(entity or 0)
    if cur <= 0 or entity <= 0:
        return False
    if _ids_match_npc(cur, entity):
        return True
    lo, hi = (cur, entity) if cur < entity else (entity, cur)
    # 一侧是短 id（会话句柄），一侧是场景实体 id
    if lo < 0x100000 and hi >= 0x100000:
        return True
    return False


def resolve_buy_npc_id(
    session: GameAttachSession,
    entity_npc_id: int,
    *,
    log: LogFn | None = None,
    prefer_cur_serv: bool = True,
    require_entity_match: bool = False,
) -> tuple[int, str]:
    """
    BuyItem 的 npc 参数：优先当前服务会话 CurServNPC，否则用附近实体 id。

    live 证据：Win_Shop 已开时 entity=72057594037928345 而 cur=409，
    用 entity 下单仍「无法使用该NPC服务」→ 买应用服务会话 id。

    require_entity_match=True（出霸刀）：若 cur 与 entity 明显是两个 NPC，
    拒绝用错 cur，避免在东方店 BuyItem 霸刀牌 ret=1 无进度。
    """
    log = log or (lambda _m: None)
    ent = int(entity_npc_id or 0)
    cur = 0
    try:
        cur = int(cur_serv_npc_id(session, log=log) or 0)
    except Exception as e:
        log(f"badao: CurServNPC read err: {e}")
        cur = 0
    if prefer_cur_serv and cur > 0:
        if require_entity_match and ent > 0 and not _likely_same_npc_session(cur, ent):
            # 错店：cur=东方 entity=不败 → 不能用 cur 买霸刀
            note = f"buy_npc=reject_cur_mismatch cur:{cur} entity:{ent}"
            log(f"badao: {note}")
            return 0, note
        note = f"buy_npc=cur:{cur} entity:{ent}"
        log(f"badao: {note}")
        return cur, note
    if ent > 0:
        note = f"buy_npc=entity:{ent} cur:{cur}"
        log(f"badao: {note}")
        return ent, note
    return 0, f"buy_npc=0 cur:{cur} entity:{ent}"


def _is_wrong_token_shop_shelf(shelf: list[dict] | None) -> bool:
    """
    时装/银票店指纹：大量 cost_tid=101065，没有残页 100007。
    live dump: 1000001:101065x3000 … 不是霸刀兑换店。
    """
    items = list(shelf or [])
    if not items:
        return False
    page_hits = 0
    ticket_hits = 0
    for it in items[:40]:
        ct = int(it.get("cost_tid", 0) or 0)
        cn = int(it.get("cost_n", 0) or 0)
        if ct == 100007 or (ct > 0 and cn >= 6000 and ct != 101065):
            page_hits += 1
        if ct == 101065:
            ticket_hits += 1
    # 银票时装店：一堆 101065，没有残页花费
    return ticket_hits >= 3 and page_hits <= 0


def _npc_menu_profile(npc_name: str | None) -> str:
    """服务菜单点击配置档：东方 / 不败坐标完全分开。"""
    n = str(npc_name or "")
    if "不败" in n:
        return "bubai"
    if "东方" in n:
        return "dongfang"
    return "default"


def _click_npc_service_options(
    session: GameAttachSession,
    *,
    hwnd: int = 0,
    log: LogFn | None = None,
    max_clicks: int = 6,
    prefer_first: bool = True,
    verify_token_shelf: bool = False,
    cfg: BadaoExchangeConfig | None = None,
    first_only: bool = False,
    y_try: int = 0,
    npc_name: str | None = None,
    profile: str | None = None,
) -> str:
    """
    在 Win_NPC* 服务面板上点选项，打开兑换/商店。

    **两套坐标互不共用**（live 标定）：
      - bubai 不败：第一格【兑换商店】 yf=0.14~0.17
      - dongfang 东方：Win_NPCContent 头图占位大，第一项约 yf=0.22~0.34
        （0.14~0.18 点空；0.42=第三项错店）。自上而下点，货架须有残页 100007。
    """
    log = log or (lambda _m: None)
    notes: list[str] = []
    prof = (profile or _npc_menu_profile(npc_name)).strip() or "default"
    try:
        from app.core.plg_ui import get_game_ui_dlg, is_dlg_show, read_dlg_rect
        from app.core.aui_click import click_client_bg
    except Exception as e:
        return f"import_fail:{e}"

    hwnd = _resolve_hwnd(session, hwnd)
    rects = []
    for nm in NPC_SERVICE_DLG_NAMES:
        try:
            dlg = int(get_game_ui_dlg(session, nm, log=lambda _m: None) or 0)
        except Exception:
            dlg = 0
        if not dlg:
            continue
        try:
            if not is_dlg_show(session, dlg, log=lambda _m: None):
                continue
        except Exception:
            pass
        try:
            r = read_dlg_rect(session, dlg, name=nm, log=lambda _m: None)
        except Exception:
            r = None
        if r is None:
            continue
        try:
            w = int(getattr(r, "w", 0) or 0)
            h = int(getattr(r, "h", 0) or 0)
            if w < 40 or h < 40:
                continue
            rects.append((nm, r))
        except Exception:
            continue
    if not rects:
        return "no_npc_service_dlg"

    # ---------- 分 NPC 坐标（禁止混用）----------
    if prof == "bubai":
        # 不败：只点第一格安全带
        bubai_ys = [0.15, 0.155, 0.145, 0.16, 0.14, 0.165, 0.152, 0.148, 0.17]
        yi = max(0, int(y_try or 0)) % len(bubai_ys)
        ordered = bubai_ys[yi:] + bubai_ys[:yi]
        y_fracs = [yf for yf in ordered if 0.135 <= float(yf) <= 0.172][:5]
        if not y_fracs:
            y_fracs = [0.15, 0.155, 0.145]
        x_fracs = [0.36, 0.34, 0.40]
        hold_ms = 70
        gap_s = 0.35
        tag = "bubai1"
        notes.append(f"profile=bubai y_try={y_try}")
    elif prof == "dongfang":
        # 东方 live：
        #   yf≈0.14~0.18 点到头图/空区 → 开不出店
        #   yf≈0.42 开出 Win_Shop 但是第三项错店（残页 BuyShopSlot ret=0）
        #   用户手动第一项可成功 → 第一项在 0.22~0.34 带
        # 策略：自上而下扫，开店后校验残页货架，命中最上正确项即停
        base = [
            0.24, 0.26, 0.22, 0.28, 0.25, 0.27, 0.23, 0.30, 0.21, 0.29,
            0.32, 0.20, 0.31, 0.33, 0.34, 0.19, 0.35, 0.36, 0.18,
        ]
        yi = max(0, int(y_try or 0)) % max(1, len(base))
        ordered = base[yi:] + base[:yi]
        y_fracs = ordered[:14]
        x_fracs = [0.36, 0.40, 0.32, 0.44, 0.38]
        hold_ms = 85
        gap_s = 0.28
        tag = "df1"
        notes.append(f"profile=dongfang first-item-band y_try={y_try}")
    else:
        y_fracs = [0.42, 0.52, 0.62, 0.72, 0.35, 0.80]
        x_fracs = [0.38, 0.45, 0.32]
        hold_ms = 55
        gap_s = 0.20
        tag = "def"
        notes.append(f"profile={prof}")

    clicks = 0
    for nm, r in rects:
        x = int(getattr(r, "x", 0) or 0)
        y = int(getattr(r, "y", 0) or 0)
        w = int(getattr(r, "w", 0) or 0)
        h = int(getattr(r, "h", 0) or 0)
        notes.append(f"rect {nm}={x},{y} {w}x{h}")
        for i, yf in enumerate(y_fracs):
            if clicks >= int(max_clicks):
                break
            xf = x_fracs[i % len(x_fracs)]
            cx = x + max(1, int(w * float(xf)))
            cy = y + max(1, int(h * float(yf)))
            ok = False
            try:
                ok = bool(
                    click_client_bg(
                        session,
                        int(hwnd or 0),
                        int(cx),
                        int(cy),
                        hold_ms=int(hold_ms),
                        humanize=False,
                        slide=False,
                        log=lambda _m: None,
                    )
                )
            except Exception as e:
                notes.append(f"{nm}@({cx},{cy}) err={e}")
                continue
            clicks += 1
            notes.append(f"{nm}#{tag}@({cx},{cy}) yf={yf:.3f} ok={ok}")
            time.sleep(float(gap_s))
            shop_ok, shop_nm = is_shop_dialog_open(session, log=log)
            if shop_ok:
                # 东方：必须能看到残页 tid=100007，否则是点到第 2/3 项错店
                if prof == "dongfang":
                    good = False
                    try:
                        from app.core.package_api import (
                            find_shop_page_slot_for_tid,
                            resolve_shop_goods_meta,
                        )
                        page_tid = 100007
                        if cfg is not None:
                            page_tid = int(
                                getattr(cfg, "goods_tid_page", 0)
                                or getattr(cfg, "page_tid", 0)
                                or 100007
                            )
                        # 菜单刚开货架可能晚半拍，扫两次
                        loc = {}
                        meta = {}
                        for _wait in (0.20, 0.35):
                            time.sleep(float(_wait))
                            loc = find_shop_page_slot_for_tid(
                                session, int(page_tid), log=lambda _m: None
                            ) or {}
                            meta = resolve_shop_goods_meta(
                                session, int(page_tid), log=lambda _m: None
                            ) or {}
                            if loc and int(loc.get("page", -1)) >= 0:
                                good = True
                                notes.append(
                                    f"dongfang shelf ok tid={page_tid} "
                                    f"p{loc.get('page')}s{loc.get('slot')} yf={yf:.3f}"
                                )
                                break
                            if meta and not meta.get("fallback") and int(meta.get("booth", -1)) >= 0:
                                good = True
                                notes.append(
                                    f"dongfang meta ok tid={page_tid} "
                                    f"booth={meta.get('booth')} yf={yf:.3f}"
                                )
                                break
                        if not good:
                            notes.append(
                                f"wrong_dongfang_shop yf={yf:.3f} no tid={page_tid}; close+retry"
                            )
                            try:
                                close_shop_dialogs(session, log=log)
                            except Exception:
                                pass
                            time.sleep(0.18)
                    except Exception as e:
                        notes.append(f"dongfang shelf check err={e}")
                        good = True  # 校验异常不强关
                    if not good:
                        continue
                notes.append(f"opened {shop_nm} yf={yf:.3f} prof={prof}")
                return ";".join(notes)
            still_menu = False
            try:
                from app.core.plg_ui import get_game_ui_dlg, is_dlg_show

                for mnm in NPC_SERVICE_DLG_NAMES:
                    d = int(get_game_ui_dlg(session, mnm, log=lambda _m: None) or 0)
                    if d and is_dlg_show(session, d, log=lambda _m: None):
                        still_menu = True
                        break
            except Exception:
                still_menu = True
            if not still_menu:
                notes.append("menu_gone")
                break
        if clicks >= int(max_clicks):
            break
    return ";".join(notes) if notes else "no_click"


def close_shop_dialogs(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> str:
    """
    关闭当前兑换/商店窗（Win_Shop / Win_Trade…），便于会话失效后重开。

    优先 AUI Show(0,0,1)；失败再尝试 Esc。
    """
    log = log or (lambda _m: None)
    notes: list[str] = []
    try:
        from app.core.plg_ui import find_shown_dialog, get_game_ui_dlg
        from app.core.aui_click import hide_aui_dialog
    except Exception as e:
        return f"import_fail:{e}"

    closed_any = False
    for nm in SHOP_DLG_NAMES:
        dlg = 0
        try:
            hit = find_shown_dialog(session, (nm,), log=lambda _m: None)
            if hit is not None and bool(getattr(hit, "shown", False)):
                dlg = int(getattr(hit, "dlg_ptr", 0) or 0) & 0xFFFFFFFF
        except Exception:
            dlg = 0
        if not dlg:
            try:
                dlg = int(get_game_ui_dlg(session, nm, log=lambda _m: None) or 0) & 0xFFFFFFFF
            except Exception:
                dlg = 0
        if not dlg:
            continue
        try:
            hr = hide_aui_dialog(
                session,
                dlg,
                force=True,
                show_args=(0, 0, 1),
                try_variants=True,
                log=lambda _m: None,
            )
            ok = bool((hr or {}).get("ok")) or not bool((hr or {}).get("shown_after", True))
            notes.append(f"{nm}:Show0 ok={ok}")
            closed_any = closed_any or ok
        except Exception as e:
            notes.append(f"{nm}:err={e}")

    # 兜底 Esc（部分商店不吃 Show0）
    still_open, still_nm = is_shop_dialog_open(session, log=lambda _m: None)
    if still_open:
        try:
            from app.core.sys_input import VK_ESCAPE
            from app.core import bg_input

            hwnd = 0
            try:
                hwnd = int(getattr(session, "hwnd", 0) or 0)
            except Exception:
                hwnd = 0
            for _ in range(2):
                try:
                    bg_input.key_tap(
                        VK_ESCAPE,
                        hwnd=hwnd or None,
                        log=lambda _m: None,
                    )
                except Exception:
                    try:
                        bg_input.key_tap(0x1B, hwnd=hwnd or None, log=lambda _m: None)
                    except Exception as e:
                        notes.append(f"esc_err={e}")
                        break
                time.sleep(0.12)
            still_open2, still_nm2 = is_shop_dialog_open(session, log=lambda _m: None)
            notes.append(
                f"esc still={'open:'+str(still_nm2) if still_open2 else 'closed'}"
            )
            if not still_open2:
                closed_any = True
            else:
                notes.append(f"still_open={still_nm or still_nm2}")
        except Exception as e:
            notes.append(f"esc_import_err={e}")
    elif closed_any:
        notes.append("closed")
    else:
        notes.append("no_shop")

    msg = ";".join(notes) if notes else "noop"
    log(f"badao: close_shop {msg}")
    return msg


def soft_refresh_npc_service(
    session: GameAttachSession,
    cfg: BadaoExchangeConfig,
    *,
    npc_name: str | None = None,
    entity_id: int = 0,
    log: LogFn | None = None,
) -> tuple[bool, int, str]:
    """
    不关店：对当前 NPC 再 Hello + 点兑换，刷新服务会话。
    live：BuyShopSlot ret=1 但背包不变时，往往会话半失效；全关重开太重。
    """
    log = log or (lambda _m: None)
    name = (npc_name or cfg.npc_name_page or cfg.npc_name or "东方姑娘").strip()
    eid = int(entity_id or 0)
    if eid <= 0:
        eid, note = find_npc_by_name(
            session, name, radius=float(cfg.npc_radius), log=log
        )
        if eid <= 0:
            return False, 0, f"soft_refresh no entity {note}"
    shop_already, shop_name0 = is_shop_dialog_open(session, log=log)
    # 店已开时不要再 Hello：会打断当前兑换会话，随后 BuyShopSlot 常 ret=0
    if not shop_already:
        try:
            from app.core.plg_interact import set_target

            set_target(session, eid, log=log)
        except Exception as e:
            log(f"badao: soft SetTarget err: {e}")
        try:
            r = npc_say_hello(session, eid, log=log)
            log(f"badao: soft Hello {getattr(r, 'message', r)}")
        except Exception as e:
            log(f"badao: soft Hello err: {e}")
            return False, int(eid), f"soft Hello err: {e}"
        time.sleep(max(0.25, float(getattr(cfg, "service_settle_s", 0.48) or 0.48)))
    else:
        # soft keep shop: high-frequency, omit
        time.sleep(0.12)
    if bool(getattr(cfg, "auto_select_service", True)) and not shop_already:
        prof = _npc_menu_profile(npc_name)
        if prof == "bubai":
            y_try = int(getattr(cfg, "_token_menu_y_try", 0) or 0)
        elif prof == "dongfang":
            y_try = int(getattr(cfg, "_page_menu_y_try", 0) or 0)
        else:
            y_try = 0
        click_note = _click_npc_service_options(
            session,
            hwnd=int(getattr(cfg, "hwnd", 0) or 0),
            log=log,
            max_clicks=(6 if prof == "bubai" else 14 if prof == "dongfang" else 6),
            npc_name=str(npc_name or ""),
            profile=prof,
            y_try=y_try,
            cfg=cfg,
        )
        time.sleep(0.15)
    shop_ok, shop_name = is_shop_dialog_open(session, log=log)
    cur = cur_serv_npc_id(session, log=log)
    if shop_ok:
        return True, int(eid), f"soft_ok shop={shop_name} cur={cur} entity={eid}"
    return False, int(eid), f"soft_fail shop_closed cur={cur} entity={eid}"


def ensure_npc_service(
    session: GameAttachSession,
    cfg: BadaoExchangeConfig,
    *,
    log: LogFn | None = None,
    npc_name: str | None = None,
    force_reopen: bool = False,
    prefer_entity_id: int = 0,
    skip_entity_scan: bool = False,
) -> tuple[bool, int, str]:
    """
    与「自动出售」同一思路：
      - 玩家先打开兑换/商店界面（Win_Shop）
      - 脚本只检查服务是否已开，不反复横跳 Hello/乱点菜单
      - 返回附近实体 id（找人提示用）；真正 BuyItem 用 resolve_buy_npc_id→CurServ

    force_reopen=True：先关店再 Hello+点兑换（连续无法用NPC 时恢复会话）。
    """
    log = log or (lambda _m: None)
    name = (npc_name or cfg.npc_name_page or cfg.npc_name or "东方姑娘").strip()

    # 0) 强制重开：关当前商店，落到 Hello 路径
    if bool(force_reopen):
        close_note = close_shop_dialogs(session, log=log)
        log(f"badao: force_reopen close -> {close_note}")
        time.sleep(max(0.12, float(getattr(cfg, "service_settle_s", 0.22) or 0.22)))
        # 不 return；下面按「未开店」路径 Hello

    # 1) 商店已开 → 直接过（和出售「保持杂货NPC打开」一致）
    shop_ok, shop_name = is_shop_dialog_open(session, log=log)
    if bool(force_reopen):
        # 关完仍显示开着则继续尝试 Hello 覆盖
        shop_ok = False
    cur = cur_serv_npc_id(session, log=log)
    # 店已开且同 NPC 流水线：跳过附近实体全量扫描（降 CRT，减少无法使用NPC）
    entity_id = int(prefer_entity_id or 0)
    find_note = "skip_scan"
    if shop_ok and bool(getattr(cfg, "reuse_open_shop", True)) and not bool(force_reopen):
        if bool(skip_entity_scan) or entity_id > 0 or int(cur or 0) > 0:
            eid = int(entity_id or cur or 0)
            note = (
                f"shop_keep shop={shop_name} cur={cur} entity={eid} "
                f"need={name} (no_entity_scan)"
            )
            return True, int(eid), note
    entity_id, find_note = find_npc_by_name(
        session, name, radius=float(cfg.npc_radius), log=log
    )

    def _ids_match(a: int, b: int) -> bool:
        a, b = int(a or 0), int(b or 0)
        if a <= 0 or b <= 0:
            return False
        if a == b:
            return True
        # 兼容曾出现的低 32 位截断
        return (a & 0xFFFFFFFF) == (b & 0xFFFFFFFF)

    if shop_ok:
        # 商店已开：卖切糕/组丹/换残页 同一东方店，直接复用，绝不关窗重开
        # 仅当明确开着「另一边」NPC（东方↔不败）才切换
        if entity_id <= 0:
            # 附近扫不到实体，但店开着且 require 宽松：仍允许继续（CurServ 下单）
            if bool(getattr(cfg, "reuse_open_shop", True)) and (cur > 0 or shop_ok):
                note = f"shop_reuse_no_entity shop={shop_name} cur={cur} need={name}"
                return True, int(cur or 0), note
            return (
                False,
                0,
                f"附近无{name}，无法确认当前商店是否可用 ({find_note})",
            )
        if cur > 0 and not _ids_match(cur, entity_id):
            # 同 NPC 的 id 空间差异可复用；两个大 id 不同 = 东方/不败 开错店 → 必须切换
            if (
                bool(getattr(cfg, "reuse_open_shop", True))
                and not bool(force_reopen)
                and _likely_same_npc_session(cur, entity_id)
            ):
                note = (
                    f"shop_reuse shop={shop_name} cur={cur} entity={entity_id} "
                    f"need={name} (session_id_space)"
                )
                return True, int(entity_id or cur), note
            log(
                f"badao: shop open but wrong NPC cur={cur} need_entity={entity_id} "
                f"need={name} — switch"
            )
            # fall through → Hello 目标 NPC
        else:
            note = (
                f"shop_open={shop_name} cur={cur} entity={entity_id} "
                f"need={name} ({find_note})"
            )
            return True, int(entity_id), note

    # 2) 未开/开错店：自动 Hello+点兑换（双 NPC 必须自动，否则无法连续）
    if not bool(getattr(cfg, "auto_npc_hello", False)):
        role = "换霸刀牌" if "不败" in str(name) else "换残页/卖切糕组丹"
        msg = (
            f"请打开【{name}】兑换商店（{role}）；"
            f"目标牌数到时会自动切不败，请保持两边 NPC 在附近"
        )
        return False, int(entity_id or 0), msg

    # 3) 自动切换 / 打开
    if entity_id <= 0:
        return False, 0, f"附近无{name}且商店未开 {find_note}"
    try:
        from app.core.plg_interact import set_target

        set_target(session, entity_id, log=log)
    except Exception as e:
        log(f"badao: SetTarget err: {e}")
    try:
        r = npc_say_hello(session, entity_id, log=log)
    except Exception as e:
        log(f"badao: auto Hello err: {e}")
    settle = max(0.15, float(getattr(cfg, "service_settle_s", 0.45) or 0.45))
    if "不败" in str(name or ""):
        settle = max(settle, 0.70)  # 菜单列表出全再点第一格
    time.sleep(settle)
    if bool(getattr(cfg, "auto_select_service", False)):
        prof = _npc_menu_profile(name)
        if prof == "bubai":
            y_try = int(getattr(cfg, "_token_menu_y_try", 0) or 0)
        elif prof == "dongfang":
            y_try = int(getattr(cfg, "_page_menu_y_try", 0) or 0)
        else:
            y_try = 0
        click_note = _click_npc_service_options(
            session,
            hwnd=int(getattr(cfg, "hwnd", 0) or 0),
            log=log,
            max_clicks=(6 if prof == "bubai" else 14 if prof == "dongfang" else 6),
            npc_name=str(name or ""),
            profile=prof,
            y_try=y_try,
            cfg=cfg,
        )
        if prof == "bubai":
            log(
                f"badao: auto service click prof=bubai "
                f"(第一格【兑换商店】 yf=0.14~0.17 y_try={y_try}) {click_note}"
            )
        elif prof == "dongfang":
            log(
                f"badao: auto service click prof=dongfang "
                f"(东方第一项带 yf=0.20~0.36 y_try={y_try}) {click_note}"
            )
        else:
            log(f"badao: auto service click prof={prof} {click_note}")
        time.sleep(0.15)
    shop_ok, shop_name = is_shop_dialog_open(session, log=log)
    cur = cur_serv_npc_id(session, log=log)
    if shop_ok:
        # 开对店则复位 y_try
        try:
            if "不败" in str(name or ""):
                cfg._token_menu_y_try = 0
            elif "东方" in str(name or ""):
                cfg._page_menu_y_try = 0
        except Exception:
            pass
        return True, int(entity_id), f"auto_open shop={shop_name} cur={cur} entity={entity_id}"
    # 点不开：下次偏移 y_try（东方第一项带 / 不败第一格）
    try:
        if "不败" in str(name or ""):
            cfg._token_menu_y_try = int(getattr(cfg, "_token_menu_y_try", 0) or 0) + 1
        elif "东方" in str(name or ""):
            cfg._page_menu_y_try = int(getattr(cfg, "_page_menu_y_try", 0) or 0) + 1
        log(
            f"badao: auto_open miss name={name} "
            f"page_y_try={getattr(cfg, '_page_menu_y_try', 0)} "
            f"token_y_try={getattr(cfg, '_token_menu_y_try', 0)}"
        )
    except Exception:
        pass
    return (
        False,
        int(entity_id),
        f"自动打开[{name}]商店失败，请手动点兑换/商店 cur={cur}",
    )



def _count_kind(snap: BagSnap, key: str) -> int:
    return int(getattr(getattr(snap, key, ResourceSnap()), "count", 0) or 0)


def _material_total(snap: BagSnap) -> int:
    """Sum of tracked pipeline materials (carry snapshot)."""
    return (
        _count_kind(snap, "dan")
        + _count_kind(snap, "page")
        + _count_kind(snap, "token")
        + _count_kind(snap, "qiegao")
        + _count_kind(snap, "pack9999")
        + _count_kind(snap, "pack100")
    )


def bag_read_collapsed(before: BagSnap, after: BagSnap) -> bool:
    """
    CRT/进程异常时 list_package 失败会静默成空包；
    若交易前有大量原料、交易后瞬间全 0，视为读包崩溃而非真实清空。
    """
    btot = _material_total(before)
    if btot <= 0:
        return False
    atot = _material_total(after)
    if atot > 0:
        return False
    # 全 0 且 layout 也读失败（cap/used 为 0）→ 典型 CRT err=5
    if int(getattr(after, "cap", 0) or 0) <= 0 and int(getattr(after, "used", 0) or 0) <= 0:
        return True
    # 一次交易不可能清空全部切糕/残页；原料从很大掉到全 0 极可疑
    if btot >= 100:
        return True
    return False


def verify_delta(
    before: BagSnap,
    after: BagSnap,
    expect: dict[str, int],
    *,
    min_progress: bool = True,
) -> bool:
    if bag_read_collapsed(before, after):
        return False
    if not expect:
        return True
    ok_any = False
    for k, exp in expect.items():
        exp = int(exp)
        b = _count_kind(before, k)
        a = _count_kind(after, k)
        d = a - b
        if exp == 0:
            continue
        if exp > 0 and d > 0:
            ok_any = True
        elif exp < 0 and d < 0:
            ok_any = True
    return ok_any



def trade_settle_seconds(
    cfg: BadaoExchangeConfig,
    op: TradeOp | None,
    *,
    fail_streak: int = 0,
    page_ok_streak: int = 0,
    buy_ret: int | None = None,
) -> float:
    """
    成交/失败后的等待：
      - 残页连成功：压到 page_fast_s（手速量级）
      - 失败/ret=0：立刻长退避，禁止继续狂点打崩 NPC
      - 卖料后仍偏长，保证货架稳定
    """
    base = max(0.12, float(getattr(cfg, "trade_delay_s", 0.35) or 0.35))
    if fail_streak > 0 or (buy_ret is not None and int(buy_ret) == 0):
        b0 = max(0.6, float(getattr(cfg, "fail_backoff_base_s", 1.8) or 1.8))
        step = max(0.2, float(getattr(cfg, "fail_backoff_step_s", 0.9) or 0.9))
        fs = max(1, int(fail_streak or 1))
        # 上限 5s；ret=0 至少按 1 次失败算
        return min(5.0, b0 + step * max(0, fs - 1))
    if op is None:
        return base
    kind = str(getattr(op, "kind", "") or "")
    cnt = max(1, int(getattr(op, "count", 1) or 1))
    if kind in (OP_QIEGAO, OP_PACK9999):
        base = max(base, float(getattr(cfg, "trade_delay_after_sell_s", 1.05) or 1.05))
        base += min(0.55, (cnt / 500.0) * 0.05)
        base = max(base, 1.15)
    elif kind == OP_TOKEN:
        base = max(base, float(getattr(cfg, "trade_delay_after_token_s", 0.70) or 0.70))
        if cnt >= 20:
            base += 0.12
    elif kind == OP_PAGE:
        warm = max(0.16, float(getattr(cfg, "page_warm_s", 0.22) or 0.22))
        fast = max(0.08, float(getattr(cfg, "page_fast_s", 0.12) or 0.12))
        steady = max(fast, float(getattr(cfg, "trade_delay_after_page_s", 0.16) or 0.16))
        need = max(1, int(getattr(cfg, "page_fast_after", 2) or 2))
        pok = max(0, int(page_ok_streak or 0))
        if pok >= need:
            base = fast
        elif pok >= 1:
            base = min(steady, warm)
        else:
            # 首笔/失败刚恢复：略慢，先确认 NPC 还能用
            base = warm
        if cnt >= 8000:
            base += 0.03
        base = max(0.08, float(base))
    return float(base)


def execute_trade(
    session: GameAttachSession,
    cfg: BadaoExchangeConfig,
    op: TradeOp,
    npc_id: int,
    *,
    log: LogFn | None = None,
    trade_fn=None,
) -> PackageActionResult:
    log = log or (lambda _m: None)

    if op.kind == OP_MOVE:
        try:
            snap = snapshot_bag(session, cfg, log=log)
        except Exception as e:
            return PackageActionResult(
                ok=False,
                action=OP_MOVE,
                message=f"move snapshot fail: {e}",
                error=str(e),
            )
        wh = int(PACKAGE_INDEX_WAREHOUSE)
        main = int(DEFAULT_PACKAGE_INDEX)
        for key in ("qiegao", "pack9999", "pack100", "dan", "page"):
            res = getattr(snap, key, None)
            for it in list(getattr(res, "items", None) or []):
                if int(getattr(it, "package", -1) or -1) != wh:
                    continue
                return move_item_between_packages(
                    session,
                    int(it.package),
                    int(it.slot),
                    main,
                    int(it.count or 1),
                    log=log,
                )
        return PackageActionResult(
            ok=False,
            action=OP_MOVE,
            message="无可从仓库拉出的材料栈",
            error="nothing_to_pull",
        )

    if op.kind == OP_OPEN:
        pkg = int(getattr(op, "package", -1))
        sl = int(getattr(op, "slot", -1))
        if pkg < 0 or sl < 0:
            # re-resolve from live bag (planner may omit items in mock/partial snap)
            try:
                snap = snapshot_bag(session, cfg, log=log)
                res = getattr(snap, getattr(op, "pack_kind", "") or "pack100", None)
                items = list(getattr(res, "items", None) or [])
                if items:
                    pkg = int(getattr(items[0], "package", -1))
                    sl = int(getattr(items[0], "slot", -1))
            except Exception as e:
                log(f"badao: open resolve slot err: {e}")
        if pkg < 0 or sl < 0:
            msg = "OPEN_PACK 缺少 package/slot"
            log(f"badao: {msg}")
            return PackageActionResult(
                ok=False, action=OP_OPEN, message=msg, error=msg
            )
        if trade_fn is not None:
            return trade_fn(session, npc_id, op, log=log)
        return use_item_in_package(session, pkg, sl, log=log)

    # 组丹9999 / 切糕：与自动出售同一接口 sell_item_from_package（仅随身包）
    if op.kind in (OP_PACK9999, OP_QIEGAO):
        pkg = int(getattr(op, "package", -1))
        sl = int(getattr(op, "slot", -1))
        cnt = max(1, int(op.count or 1))
        if pkg < 0 or sl < 0:
            try:
                snap = snapshot_bag(session, cfg, log=log)
                kind = "pack9999" if op.kind == OP_PACK9999 else "qiegao"
                it = _pick_sell_item(snap, kind, cfg)
                if it is not None:
                    pkg = int(getattr(it, "package", -1))
                    sl = int(getattr(it, "slot", -1))
                    cnt = min(cnt, max(1, int(getattr(it, "count", 1) or 1)))
            except Exception as e:
                log(f"badao: sell resolve err: {e}")
        if pkg < 0 or sl < 0:
            msg = f"{op.kind} 缺少 package/slot（随身包未找到栈）"
            log(f"badao: {msg}")
            return PackageActionResult(
                ok=False, action=op.kind, message=msg, error=msg
            )
        if trade_fn is not None:
            return trade_fn(session, npc_id, op, log=log)
        return sell_item_from_package(
            session, int(pkg), int(sl), int(cnt), log=log
        )

    if op.kind not in (OP_TOKEN, OP_PAGE):
        return PackageActionResult(
            ok=False, action=op.kind, message=f"非交易 op={op.kind}"
        )

    goods = int(op.goods_tid or 0)
    buy_count = max(1, int(op.count or 1))
    # 数量：残页/通用按 9999 满组；牌默认 999；payload u16 硬顶 65535
    sm = max(1, int(getattr(cfg, "stack_max", 0) or STACK_MAX))
    tm = max(1, int(getattr(cfg, "token_stack_max", 0) or TOKEN_STACK_MAX))
    max_buy = max(1, min(0xFFFF, int(getattr(cfg, "max_buy_count", 0) or sm)))
    if op.kind == OP_PAGE:
        # live: count>9999 → 无法使用该NPC功能；硬顶 PAGE_BATCH_MAX(9999)
        pbm = int(
            getattr(cfg, "page_buy_max", 0)
            or getattr(cfg, "page_batch_max", 0)
            or sm
        )
        max_buy = max(1, min(int(PAGE_BATCH_MAX), 9999, int(sm), max(1, pbm)))
    elif op.kind == OP_TOKEN:
        # 霸刀一次可多张，单笔上限 token_stack_max(999)
        max_buy = max(1, min(tm, max_buy, 0xFFFF))
    elif op.kind == OP_PACK9999:
        max_buy = max(
            1,
            min(max_buy, int(getattr(cfg, "max_pack9999_per_trade", 0) or sm)),
        )
    elif op.kind == OP_QIEGAO:
        max_buy = max(
            1,
            min(max_buy, int(getattr(cfg, "max_qiegao_per_trade", 0) or sm)),
        )
    buy_count = max(1, min(int(buy_count), max_buy))
    gidx = int(getattr(op, "goods_index", 0) or 0)
    if trade_fn is not None:
        return trade_fn(session, npc_id, op, log=log)
    # 缺省：用配置里的产物/原料 tid（与背包 tid 一致）
    if goods <= 0:
        if op.kind == OP_PAGE:
            goods = int(getattr(cfg, "goods_tid_page", 0) or getattr(cfg, "page_tid", 0) or 0)
        elif op.kind == OP_TOKEN:
            goods = int(getattr(cfg, "goods_tid_token", 0) or getattr(cfg, "token_tid", 0) or 0)
        elif op.kind == OP_QIEGAO:
            goods = int(getattr(cfg, "goods_tid_qiegao", 0) or getattr(cfg, "qiegao_tid", 0) or 0)
        elif op.kind == OP_PACK9999:
            goods = int(
                getattr(cfg, "goods_tid_pack9999", 0)
                or getattr(cfg, "pack9999_tid", 0)
                or 0
            )
    if goods <= 0:
        msg = (
            f"{op.kind} 缺少 goods_tid（BuyItem fail-closed；霸刀牌 tid 未配置时无法出牌）。"
        )
        log(f"badao: {msg}")
        return PackageActionResult(
            ok=False, action=op.kind, message=msg, error=msg
        )
    # booth：残页用默认/配置即可（live 扫架 resolve_shop_goods_meta 生产机可达数秒，禁止每笔扫）
    booth = -1
    if op.kind == OP_PAGE:
        if gidx <= 0:
            gidx = int(getattr(cfg, "goods_index_page", 7) or 7)
        if gidx <= 0:
            gidx = int(DEFAULT_BOOTH_BY_TID.get(int(goods), 7) or 7)
        booth = int(gidx)
    else:
        try:
            meta = resolve_shop_goods_meta(session, int(goods), log=log) or {}
            booth = int(meta.get("booth", -1))
            if booth >= 0:
                if gidx != booth:
                    log(
                        f"badao: booth resolve tid={goods} cfg/op_idx={gidx} -> live={booth} "
                        f"cost={meta.get('cost_tid')}x{meta.get('cost_n')}"
                    )
                gidx = booth
        except Exception as e:
            log(f"badao: booth resolve err: {e}")
    # 关键：PAGE/卖料优先 CurServ；TOKEN 必须 cur 与目标实体同 NPC，禁止东方 cur 买霸刀
    buy_id, buy_note = resolve_buy_npc_id(
        session,
        int(npc_id or 0),
        log=log,
        prefer_cur_serv=bool(getattr(cfg, "buy_prefer_cur_serv", True)),
        require_entity_match=bool(op.kind == OP_TOKEN),
    )
    if buy_id <= 0:
        msg = (
            f"{op.kind} 无可用 buy npc id（entity={npc_id} note={buy_note}）；"
            f"多半开错店（东方/不败），需关店重开目标 NPC"
        )
        log(f"badao: {msg}")
        return PackageActionResult(
            ok=False, action=op.kind, message=msg, error=msg
        )

    # 霸刀：货架命中再买。背包 tid=101058 可能≠商店 goods tid，
    # 故同时按「花费=绝学残页 x pages_per_token(6000)」匹配。
    if op.kind == OP_TOKEN:
        page_cost_tid = int(
            getattr(cfg, "goods_tid_page", 0)
            or getattr(cfg, "page_tid", 0)
            or 100007
        )
        pages_need = max(1, int(getattr(cfg, "pages_per_token", 0) or PAGES_PER_TOKEN))
        hit = {}
        try:
            hit = find_shop_goods(
                session,
                goods_tid=int(goods),
                cost_tid=int(page_cost_tid),
                cost_n=int(pages_need),
                cost_n_candidates=[int(pages_need), 6000],
                log=log,
            ) or {}
        except Exception as e:
            log(f"badao: token find_shop_goods err: {e}")
            hit = {}
        # 兼容旧路径：按 tid 扫 page/slot
        if not hit:
            try:
                loc0 = find_shop_page_slot_for_tid(session, int(goods), log=log)
                if loc0:
                    hit = dict(loc0)
                    hit.setdefault("tid", int(goods))
            except Exception as e:
                log(f"badao: token find_shop_page_slot err: {e}")
        if not hit and booth < 0:
            # 购买失败才看货架：是否点错菜单（第2/3项时装店）
            shelf = []
            try:
                shelf = list_shop_goods(session, log=log) or []
                log(
                    f"badao: TOKEN shelf dump n={len(shelf)} "
                    + ",".join(
                        f"{x.get('tid')}:cost{x.get('cost_tid')}x{x.get('cost_n')}"
                        f"@p{x.get('page')}s{x.get('slot')}b{x.get('booth')}"
                        for x in shelf[:30]
                    )
                )
            except Exception as e:
                log(f"badao: TOKEN shelf dump err: {e}")
            wrong = _is_wrong_token_shop_shelf(shelf)
            if wrong:
                # 点到时装/银票店了：关店，下一轮上移点第一格
                try:
                    setattr(
                        cfg,
                        "_token_menu_y_try",
                        int(getattr(cfg, "_token_menu_y_try", 0) or 0) + 1,
                    )
                except Exception:
                    pass
                try:
                    close_shop_dialogs(session, log=log)
                except Exception as e:
                    log(f"badao: close wrong shop err: {e}")
                msg = (
                    f"TOKEN 点错商店（货架像银票/时装店 cost=101065，不是残页兑换）；"
                    f"已关店，下次重试第一格【兑换商店】 y_try="
                    f"{getattr(cfg, '_token_menu_y_try', 0)}"
                )
                log(f"badao: {msg}")
                return PackageActionResult(
                    ok=False, action=OP_TOKEN, message=msg, error="wrong_shop_menu"
                )
            msg = (
                f"TOKEN 货架未解析到（tid={goods} 或 花费{page_cost_tid}x{pages_need}）；"
                f"请开【不败姑娘】第一格【兑换商店】"
            )
            log(f"badao: {msg}")
            return PackageActionResult(
                ok=False, action=OP_TOKEN, message=msg, error="token_shop_meta_missing"
            )
        # 用货架真实 goods tid（可能≠背包 101058）
        buy_tid = int(hit.get("tid", 0) or goods or 0)
        buy_page = int(hit.get("page", -1))
        buy_slot = int(hit.get("slot", -1))
        buy_booth = int(hit.get("booth", -1))
        if buy_booth < 0:
            buy_booth = int(booth if booth >= 0 else gidx)
        if buy_tid > 0 and buy_tid != int(goods):
            log(
                f"badao: TOKEN goods tid remap bag={goods} -> shop={buy_tid} "
                f"cost={hit.get('cost_tid')}x{hit.get('cost_n')}"
            )
            goods = buy_tid
        if buy_page >= 0 and buy_slot >= 0:
            log(
                f"badao: TOKEN BuyShopSlot page={buy_page} slot={buy_slot} "
                f"x{buy_count} tid={goods} booth={buy_booth} via={hit.get('via')} "
                f"npc={buy_note}"
            )
            r = buy_item_by_shop_slot(
                session,
                int(buy_page),
                int(buy_slot),
                int(buy_count),
                mode=2,
                log=log,
            )
            r.message = (
                f"{r.message} tid={goods} booth0={hit.get('booth0', buy_booth)} "
                f"via={hit.get('via')} npc={buy_note}"
            )
            try:
                remember_shop_slot(int(goods), int(buy_page), int(buy_slot))
                bag_tid = int(
                    getattr(cfg, "goods_tid_token", 0)
                    or getattr(cfg, "token_tid", 0)
                    or 101058
                )
                if bag_tid > 0 and bag_tid != int(goods):
                    remember_shop_slot(bag_tid, int(buy_page), int(buy_slot))
            except Exception:
                pass
            return r
        # 有货架命中但无 slot：禁止 BuyItem（ret=1 无进度）；试默认 page/slot
        dps = DEFAULT_SHOP_SLOT_BY_TID.get(int(goods)) or DEFAULT_SHOP_SLOT_BY_TID.get(
            int(getattr(cfg, "goods_tid_token", 0) or getattr(cfg, "token_tid", 0) or 101058)
        )
        if dps:
            log(
                f"badao: TOKEN no live slot, use default BuyShopSlot "
                f"page={dps[0]} slot={dps[1]} x{buy_count} tid={goods}"
            )
            r = buy_item_by_shop_slot(
                session, int(dps[0]), int(dps[1]), int(buy_count), mode=2, log=log
            )
            r.message = f"{r.message} tid={goods} via=default_slot npc={buy_note}"
            remember_shop_slot(int(goods), int(dps[0]), int(dps[1]))
            return r
        msg = (
            f"TOKEN 有货架条目但无 page/slot tid={goods}；禁止 BuyItem 盲买"
        )
        log(f"badao: {msg}")
        return PackageActionResult(
            ok=False, action=OP_TOKEN, message=msg, error="token_shop_meta_missing"
        )

    # 残页：BuyShopSlot only；默认/cache 直买（生产机扫架 3~4s/次，禁止每笔扫）
    log(
        f"badao: buy goods={goods} x{buy_count} idx={gidx} via {buy_note} "
        f"(BuyShopSlot only)"
    )
    return buy_goods_tid_via_shop(
        session,
        int(goods),
        int(buy_count),
        goods_index=int(gidx),
        npc_id=int(buy_id),
        log=log,
        allow_buyitem_fallback=False,
        prefer_known_slot=True,  # 默认 0,47 / cache 直买；失败才扫架（扫架生产机 3~4s）
        allow_shelf_scan=True,
    )


def _sleep_stop(seconds: float, stop_event: threading.Event | None) -> bool:
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if stop_event is not None and stop_event.is_set():
            return True
        time.sleep(min(0.2, max(0.05, end - time.time())))
    return bool(stop_event is not None and stop_event.is_set())


def run_badao_exchange(
    session: GameAttachSession,
    cfg: BadaoExchangeConfig | None = None,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    on_status: StatusFn | None = None,
    on_snap: Callable[[BagSnap, TradeOp | None], None] | None = None,
    trade_fn=None,
    max_steps: int = 0,
) -> PackageActionResult:
    log = log or (lambda _m: None)
    on_status = on_status or (lambda _m: None)
    cfg = cfg or BadaoExchangeConfig()
    # 非马上需要：开跑前预热 bag/money/scene
    try:
        from app.core.state_dispatch import StateKind, warmup_session

        warmup_session(
            session,
            (StateKind.BAG, StateKind.MONEY, StateKind.SCENE, StateKind.POS),
            log=lambda m: log(f"badao warmup: {m}") if m else None,
        )
    except Exception as e:
        log(f"badao warmup skip: {e}")
    tokens_gained = 0
    steps = 0
    fail_streak = 0
    target = max(0, int(getattr(cfg, "target_tokens", 0) or 0))
    # 显式队列：规划进队，执行只出队；每笔成功清空并按真包重规划
    # （买残页腾格后必须重算，才能继续卖组丹，避免两逻辑打架）
    op_queue: deque = deque()
    last_need_npc: str = ""  # 卖/换页同东方，保持开店不关
    last_npc_id: int = 0
    last_good_snap: BagSnap | None = None
    npc_reopen_count = 0
    page_ok_streak = 0

    while True:
        if stop_event is not None and stop_event.is_set():
            msg = f"已停止 · 本轮出牌+{tokens_gained}"
            on_status(msg)
            log(f"badao: {msg}")
            return PackageActionResult(
                ok=tokens_gained > 0, action="badao_exchange", message=msg
            )
        try:
            from app.core.safe_dispatch import session_blocked

            blocked, brsn = session_blocked(session)
        except Exception:
            blocked, brsn = False, ""
        if blocked:
            msg = (
                f"远程不可用已停止({brsn})；请确认客户端存活后重开脚本"
                f" · 本轮出牌+{tokens_gained}"
            )
            on_status(msg)
            log(f"badao: hard_stop {brsn}")
            return PackageActionResult(
                ok=tokens_gained > 0,
                action="badao_exchange",
                message=msg,
                error=brsn,
            )
        if max_steps and steps >= int(max_steps):
            msg = f"达到步数上限 steps={steps} 出牌+{tokens_gained}"
            on_status(msg)
            return PackageActionResult(
                ok=tokens_gained > 0, action="badao_exchange", message=msg
            )

        try:
            snap = snapshot_bag(session, cfg, log=log)
        except Exception as e:
            fail_streak += 1
            log(f"badao: snapshot err: {e}")
            try:
                from app.core.safe_dispatch import get_dispatch, session_blocked

                get_dispatch().note_exception(int(getattr(session, "pid", 0) or 0), e)
                b2, r2 = session_blocked(session)
                if b2:
                    msg = (
                        f"读包失败且远程不可用({r2})，已停止 · 本轮出牌+{tokens_gained}"
                    )
                    on_status(msg)
                    return PackageActionResult(
                        ok=tokens_gained > 0,
                        action="badao_exchange",
                        message=msg,
                        error=str(e),
                    )
            except Exception:
                pass
            if fail_streak >= int(cfg.fail_streak_max):
                msg = f"读背包连续失败: {e}"
                on_status(msg)
                return PackageActionResult(
                    ok=False, action="badao_exchange", message=msg, error=str(e)
                )
            if _sleep_stop(cfg.pause_poll_s, stop_event):
                continue
            continue

        if last_good_snap is not None and bag_read_collapsed(last_good_snap, snap):
            msg = (
                "读包异常（疑似游戏进程卡死/CRT失败），已停止以免误判清空；"
                "请确认客户端存活后重开脚本"
            )
            log(
                f"badao: bag-read-collapse(loop) before_mat={_material_total(last_good_snap)} "
                f"after_mat={_material_total(snap)}"
            )
            try:
                from app.core.safe_dispatch import get_dispatch

                get_dispatch().note_exception(
                    int(getattr(session, "pid", 0) or 0),
                    OSError("bag_read_collapsed VirtualAllocEx/err=5"),
                )
            except Exception:
                pass
            on_status(msg)
            return PackageActionResult(
                ok=tokens_gained > 0,
                action="badao_exchange",
                message=msg + f" · 本轮出牌+{tokens_gained}",
                error=msg,
            )
        if _material_total(snap) > 0 or int(getattr(snap, "cap", 0) or 0) > 0:
            last_good_snap = snap

        # 目标牌数达到则收工
        if target > 0 and tokens_gained >= target:
            msg = (
                f"达到目标牌数 {tokens_gained}/{target} · 本轮出牌+{tokens_gained}"
            )
            on_status(msg)
            log(f"badao: {msg}")
            return PackageActionResult(
                ok=True, action="badao_exchange", message=msg
            )

        # 队列空才进队；有存货只出队（成功后会 clear 强制按真包重进队）
        if not op_queue:
            try:
                op_queue = plan_pipeline_queue(
                    snap, cfg, tokens_gained=tokens_gained, max_ops=8
                )
                if len(op_queue) > 1:
                    # 队列规划极高频，默认静默
                    pass
            except Exception as e:
                log(f"badao: queue plan err: {e}")
                op_queue = deque(
                    [plan_next_op(snap, cfg, tokens_gained=tokens_gained)]
                )
        op = op_queue.popleft() if op_queue else plan_next_op(
            snap, cfg, tokens_gained=tokens_gained
        )
        try:
            setattr(op, "tokens_gained", int(tokens_gained))
        except Exception:
            pass
        try:
            status = format_action_status(
                snap, cfg, op, tokens_gained=tokens_gained
            )
        except Exception:
            try:
                status = format_progress_line(
                    snap, cfg, tokens_gained=tokens_gained, op_kind=getattr(op, "kind", "-")
                )
            except Exception:
                status = f"已换 {tokens_gained}/?，预计还可换 -"
        if on_snap is not None:
            try:
                on_snap(snap, op)
            except Exception:
                pass

        if op.kind == OP_DONE:
            msg = f"{describe_op_action(op, cfg)} · 本轮出牌+{tokens_gained}"
            on_status(msg)
            log(f"badao: {msg}")
            return PackageActionResult(ok=True, action="badao_exchange", message=msg)

        if op.kind == OP_PAUSE:
            op_queue.clear()  # 暂停后按真包重规划
            on_status(status)
            log(f"badao: pause {getattr(op, 'reason', '')}")
            if _sleep_stop(cfg.pause_poll_s, stop_event):
                continue
            continue

        npc_id = 0
        if op.kind not in (OP_MOVE, OP_OPEN):
            need_npc = npc_name_for_op(cfg, op.kind)
            # 卖东西 + 换残页 = 同一东方：店开着就复用，不关不 Hello
            # 仅 阶段A(东方) ↔ 阶段B(不败) 切换时 force_reopen；同阶段 keep open
            switching = bool(last_need_npc) and (last_need_npc != need_npc)
            first_need = not bool(last_need_npc)
            force_open = bool(
                getattr(cfg, "auto_npc_hello", False)
                and (switching or (first_need and op.kind == OP_TOKEN))
            )
            # 粘滞错店：TOKEN 但 last 仍是东方 → 强制切到不败
            if (
                op.kind == OP_TOKEN
                and bool(getattr(cfg, "auto_npc_hello", False))
                and last_need_npc
                and last_need_npc != need_npc
            ):
                force_open = True
            ok_npc, npc_id, npc_note = ensure_npc_service(
                session,
                cfg,
                log=log,
                npc_name=need_npc,
                force_reopen=force_open,
                prefer_entity_id=int(last_npc_id or 0) if not switching else 0,
                skip_entity_scan=bool(
                    (not switching)
                    and (not force_open)
                    and last_need_npc == need_npc
                    and int(last_npc_id or 0) > 0
                ),
            )
            if not ok_npc:
                op_queue.clear()  # 没成交，作废预规划
                on_status(f"请打开【{need_npc}】商店 · {status}")
                # 失败细节不刷盘：面板已足够；仅记简短一次级信息
                log(f"badao: 等待打开 {need_npc}")
                if _sleep_stop(cfg.pause_poll_s, stop_event):
                    continue
                continue
            last_need_npc = need_npc
            if int(npc_id or 0) > 0:
                last_npc_id = int(npc_id)
        else:
            npc_note = "open-pack" if op.kind == OP_OPEN else "warehouse-move"

        # 面板一眼能懂：动作 + 进度 + 背包；文件日志仅记动作切换/结果
        on_status(status)
        before = snap
        try:
            r = execute_trade(
                session, cfg, op, npc_id, log=log, trade_fn=trade_fn
            )
        except Exception as e:
            op_queue.clear()
            fail_streak += 1
            log(f"badao: trade err: {e}")
            reopen_after = max(
                1,
                int(
                    getattr(cfg, "npc_reopen_after_fails", 0)
                    or getattr(cfg, "fail_streak_max", 5)
                    or 5
                ),
            )
            reopen_max = max(0, int(getattr(cfg, "npc_reopen_max", 8) or 8))
            if fail_streak >= reopen_after and npc_reopen_count < reopen_max:
                npc_reopen_count += 1
                need = npc_name_for_op(cfg, op.kind)
                msg = (
                    f"交易异常连续{fail_streak}次，关店重开"
                    f"（{npc_reopen_count}/{reopen_max}）· {need}"
                )
                on_status(msg)
                log(f"badao: {msg} err={e}")
                try:
                    close_shop_dialogs(session, log=log)
                    ensure_npc_service(
                        session, cfg, log=log, npc_name=need, force_reopen=True
                    )
                except Exception as e2:
                    log(f"badao: reopen after trade-err failed: {e2}")
                fail_streak = 0
                last_need_npc = ""
                if _sleep_stop(cfg.pause_poll_s, stop_event):
                    continue
                continue
            if fail_streak >= int(cfg.fail_streak_max) and npc_reopen_count >= reopen_max:
                msg = f"交易连续失败: {e}"
                on_status(msg)
                return PackageActionResult(
                    ok=False, action="badao_exchange", message=msg, error=str(e)
                )
            if _sleep_stop(cfg.trade_delay_s, stop_event):
                continue
            continue

        # 下单后等会话/背包结算：残页自适应快；ret=0/失败立刻长退避
        buy_ret = getattr(r, "ret", None)
        try:
            buy_ret_i = int(buy_ret) if buy_ret is not None else None
        except Exception:
            buy_ret_i = None
        pre_fail = int(fail_streak)
        if buy_ret_i == 0 or (
            op.kind in (OP_PAGE, OP_TOKEN)
            and getattr(r, "ok", True) is False
            and str(getattr(r, "error", "") or "") in ("buy_slot_ret0", "no_shop_slot")
        ):
            pre_fail = max(1, pre_fail + 1)
        if _sleep_stop(
            trade_settle_seconds(
                cfg,
                op,
                fail_streak=pre_fail,
                page_ok_streak=int(page_ok_streak),
                buy_ret=buy_ret_i,
            ),
            stop_event,
        ):
            continue

        try:
            after = snapshot_bag(session, cfg, log=log)
        except Exception as e:
            fail_streak += 1
            page_ok_streak = 0
            log(f"badao: after-snapshot err: {e}")
            if fail_streak >= int(cfg.fail_streak_max):
                msg = f"读背包连续失败: {e}"
                on_status(msg)
                return PackageActionResult(
                    ok=tokens_gained > 0,
                    action="badao_exchange",
                    message=msg + f" · 本轮出牌+{tokens_gained}",
                    error=str(e),
                )
            if _sleep_stop(cfg.pause_poll_s, stop_event):
                continue
            continue

        if bag_read_collapsed(before, after):
            msg = (
                "读包异常（疑似游戏进程卡死/CRT失败），已停止以免误判清空；"
                "请确认客户端存活后重开脚本"
            )
            log(
                f"badao: bag-read-collapse before_mat={_material_total(before)} "
                f"after_mat={_material_total(after)} cap={getattr(after, 'cap', 0)} "
                f"used={getattr(after, 'used', 0)} trade={getattr(r, 'message', '')}"
            )
            on_status(msg)
            return PackageActionResult(
                ok=tokens_gained > 0,
                action="badao_exchange",
                message=msg + f" · 本轮出牌+{tokens_gained}",
                error=msg,
            )

        progressed = verify_delta(before, after, op.expect_delta)
        # 高速买页：偶发首扫偏早 → ret!=0 时再扫一次，避免误判狂刷失败路径
        if (
            (not progressed)
            and op.kind == OP_PAGE
            and buy_ret_i is not None
            and int(buy_ret_i) != 0
        ):
            recheck = max(0.05, float(getattr(cfg, "page_recheck_bag_s", 0.10) or 0.10))
            if _sleep_stop(recheck, stop_event):
                continue
            try:
                after2 = snapshot_bag(session, cfg, log=log)
                if verify_delta(before, after2, op.expect_delta):
                    after = after2
                    progressed = True
                    log("badao: page bag recheck hit (fast-path late update)")
            except Exception as e:
                log(f"badao: page bag recheck err: {e}")

        if op.kind == OP_MOVE and not progressed:
            msg = (
                getattr(r, "message", None)
                or "仓库材料请手动取到随身包后继续"
            )
            on_status(f"暂停：{msg}")
            log(f"badao: move skipped (manual warehouse): {msg}")
            if _sleep_stop(cfg.pause_poll_s, stop_event):
                continue
            continue

        steps += 1
        if progressed:
            fail_streak = 0
            # 成功成交 → 清零重开计数（长跑不能累计到上限就停）
            npc_reopen_count = 0
            if op.kind == OP_TOKEN:
                tokens_gained += max(1, int(getattr(op, "count", 1) or 1))
                page_ok_streak = 0
                try:
                    cfg._token_menu_y_try = 0  # 第一格点对了，重置
                except Exception:
                    pass
            elif op.kind == OP_PAGE:
                page_ok_streak += 1
                try:
                    cfg._page_menu_y_try = 0
                except Exception:
                    pass
                every = max(0, int(getattr(cfg, "page_burst_pause_every", 4) or 0))
                extra = float(getattr(cfg, "page_burst_pause_s", 0.55) or 0.0)
                if every > 0 and extra > 0 and page_ok_streak % every == 0:
                    log(
                        f"badao: page burst pause +{extra:.2f}s "
                        f"after {page_ok_streak} ok pages"
                    )
                    if _sleep_stop(extra, stop_event):
                        continue
            else:
                # 卖切糕/组丹后下一笔残页用 warm，不要立刻全速
                page_ok_streak = 0
            # 成功必须清空预规划：买页腾格后下一轮重新进队才能再卖组丹
            op_queue.clear()
            # 单笔成功极频繁，面板 status 已展示；仅动作类型变化时记一行
            try:
                last_ok = str(getattr(cfg, "_log_last_ok_kind", "") or "")
                if str(op.kind) != last_ok:
                    cfg._log_last_ok_kind = str(op.kind)
                    log(f"badao: 完成 {describe_op_action(op, cfg)}")
            except Exception:
                pass
        else:
            op_queue.clear()  # 失败也重规划，避免死跟过期队列
            fail_streak += 1
            page_ok_streak = 0  # 立刻退出全速，防止 NPC 不可用还狂买
            log(
                f"badao: no bag progress {op.kind} ret={getattr(r, 'ret', None)} "
                f"msg={getattr(r, 'message', '')} streak={fail_streak} "
                f"(speed=safe/backoff)"
            )
            # BuyShopSlot ret=0 / 无进度：清 slot 缓存，避免死磕错误 page/slot
            if op.kind == OP_PAGE:
                try:
                    # 可能点到东方第 2/3 项错店 → 下次微调第一格 y
                    setattr(
                        cfg,
                        "_page_menu_y_try",
                        int(getattr(cfg, "_page_menu_y_try", 0) or 0) + 1,
                    )
                    log(
                        f"badao: page menu y_try -> "
                        f"{getattr(cfg, '_page_menu_y_try', 0)} after no progress"
                    )
                except Exception:
                    pass
            if op.kind in (OP_PAGE, OP_TOKEN):
                try:
                    gtid = int(getattr(op, "goods_tid", 0) or 0)
                    if gtid <= 0 and op.kind == OP_PAGE:
                        gtid = int(
                            getattr(cfg, "goods_tid_page", 0)
                            or getattr(cfg, "page_tid", 0)
                            or 100007
                        )
                    if gtid <= 0 and op.kind == OP_TOKEN:
                        gtid = int(
                            getattr(cfg, "goods_tid_token", 0)
                            or getattr(cfg, "token_tid", 0)
                            or 101058
                        )
                    clear_shop_slot_cache(gtid)
                    if op.kind == OP_TOKEN:
                        clear_shop_slot_cache(101058)
                    log(f"badao: cleared shop slot cache tid={gtid} after no progress")
                except Exception as e:
                    log(f"badao: clear shop slot cache err: {e}")
            # 点错不败菜单：立刻丢掉 keep，下一轮 force_reopen 重点第一格
            if op.kind == OP_TOKEN and str(getattr(r, "error", "") or "") in (
                "wrong_shop_menu",
                "token_shop_meta_missing",
            ):
                last_need_npc = ""
                last_npc_id = 0
                log(
                    "badao: clear npc keep after token shop miss; "
                    f"y_try={getattr(cfg, '_token_menu_y_try', 0)}"
                )
            soft_after = max(
                1,
                int(getattr(cfg, "npc_soft_refresh_after_fails", 1) or 1),
            )
            reopen_after = max(
                soft_after + 1,
                int(
                    getattr(cfg, "npc_reopen_after_fails", 0)
                    or getattr(cfg, "fail_streak_max", 5)
                    or 5
                ),
            )
            # BuyShopSlot ret=0 = 拒单/错店：软刷往往无用，第 2 次就关店重开
            msg_l = str(getattr(r, "message", "") or "")
            err_l = str(getattr(r, "error", "") or "")
            if (
                "ret=0" in msg_l
                or err_l == "buy_slot_ret0"
                or int(getattr(r, "ret", 1) or 1) == 0
            ):
                reopen_after = min(int(reopen_after), 2)
            reopen_max = max(0, int(getattr(cfg, "npc_reopen_max", 8) or 8))
            # 1) 先软刷新（店已开则跳过 Hello），比立刻关店稳
            if fail_streak == soft_after:
                need = npc_name_for_op(cfg, op.kind)
                on_status(f"无背包变化，软刷新NPC会话 · {need}")
                log(f"badao: soft_refresh after fail streak={fail_streak} need={need}")
                try:
                    ok_s, eid_s, n_s = soft_refresh_npc_service(
                        session,
                        cfg,
                        npc_name=need,
                        entity_id=int(last_npc_id or 0),
                        log=log,
                    )
                    log(f"badao: soft_refresh ok={ok_s} note={n_s}")
                    if int(eid_s or 0) > 0:
                        last_npc_id = int(eid_s)
                    try:
                        clear_shop_slot_cache(0)
                        log("badao: cleared all shop slot cache after soft_refresh")
                    except Exception:
                        pass
                except Exception as e:
                    log(f"badao: soft_refresh err: {e}")
                if _sleep_stop(
                    trade_settle_seconds(cfg, op, fail_streak=fail_streak),
                    stop_event,
                ):
                    continue
                continue
            if fail_streak >= reopen_after:
                if npc_reopen_count < reopen_max:
                    npc_reopen_count += 1
                    need = npc_name_for_op(cfg, op.kind)
                    msg = (
                        f"连续{fail_streak}次无法用NPC，关店重开"
                        f"（{npc_reopen_count}/{reopen_max}）· {need}"
                    )
                    on_status(msg)
                    log(f"badao: {msg} last={getattr(r, 'message', '')}")
                    try:
                        close_shop_dialogs(session, log=log)
                    except Exception as e:
                        log(f"badao: close_shop err: {e}")
                    if _sleep_stop(
                        max(0.45, float(getattr(cfg, "service_settle_s", 0.48) or 0.48)),
                        stop_event,
                    ):
                        continue
                    try:
                        ok_re, _nid, nnote = ensure_npc_service(
                            session,
                            cfg,
                            log=log,
                            npc_name=need,
                            force_reopen=True,
                        )
                        log(
                            f"badao: reopen ok={ok_re} note={nnote} "
                            f"reopen={npc_reopen_count}/{reopen_max}"
                        )
                        try:
                            clear_shop_slot_cache(0)
                            log("badao: cleared all shop slot cache after reopen")
                        except Exception:
                            pass
                    except Exception as e:
                        log(f"badao: reopen err: {e}")
                        ok_re = False
                    fail_streak = 0
                    last_need_npc = ""
                    last_npc_id = 0
                    op_queue.clear()
                    if not ok_re:
                        on_status(f"重开商店失败，稍后重试 · {need}")
                        if _sleep_stop(cfg.pause_poll_s, stop_event):
                            continue
                        continue
                    # 重开后给会话一点稳定时间再下单
                    if _sleep_stop(
                        max(0.55, float(getattr(cfg, "service_settle_s", 0.48) or 0.48)),
                        stop_event,
                    ):
                        continue
                    continue
                # 不再因 reopen 次数硬停：材料还在就长歇后继续（否则整晚会误停）
                msg = (
                    f"关店重开已达 {npc_reopen_count}/{reopen_max}，"
                    f"长歇后继续（不清空原料）"
                )
                on_status(msg)
                log(f"badao: {msg} last={getattr(r, 'message', '')}")
                npc_reopen_count = max(0, reopen_max // 2)  # 降半，避免立刻再撞顶
                fail_streak = 0
                last_need_npc = ""
                last_npc_id = 0
                op_queue.clear()
                cool = max(
                    8.0,
                    float(getattr(cfg, "npc_reopen_cooldown_s", 12.0) or 12.0),
                )
                if _sleep_stop(cool, stop_event):
                    continue
                continue
            # 失败退避：别连点 Buy 刷「无法使用该NPC功能」
            if _sleep_stop(
                trade_settle_seconds(cfg, op, fail_streak=fail_streak),
                stop_event,
            ):
                continue


__all__ = [
    "STACK_MAX",
    "TOKEN_STACK_MAX",
    "DAN_PER_PAGE",
    "PAGES_PER_TOKEN",
    "QIEGAO_TO_DAN",
    "OP_TOKEN",
    "OP_PAGE",
    "OP_QIEGAO",
    "OP_PACK9999",
    "OP_OPEN",
    "PACK9999_TO_DAN",
    "OP_PAUSE",
    "OP_DONE",
    "OP_MOVE",
    "PACK_9999_TID",
    "PACK_100_TID",
    "BadaoExchangeConfig",
    "ResourceSnap",
    "BagSnap",
    "TradeOp",
    "snapshot_bag",
    "slots_needed_for_add",
    "slots_for_count",
    "net_slots_delta",
    "max_pages_by_slot_transform",
    "max_feed_units_by_slot_transform",
    "max_feed_units_safe",
    "clamp_feed_units_to_dan_room",
    "max_dan_receivable_now",
    "can_receive",
    "max_receivable",
    "pipeline_reserve_free",
    "plan_next_op",
    "plan_token_buy_count",
    "plan_pipeline_queue",
    "apply_op_expect_delta",
    "bag_snap_from_counts",
    "warehouse_resource_counts",
    "carry_resource_counts",
    "format_progress_line",
    "format_action_status",
    "describe_op_action",
    "format_bag_brief",
    "estimate_token_capacity",
    "plan_pull_to_carry",
    "materials_locations",
    "find_npc_by_name",
    "find_dongfang_npc",
    "npc_name_for_op",
    "is_shop_dialog_open",
    "cur_serv_npc_id",
    "close_shop_dialogs",
    "soft_refresh_npc_service",
    "ensure_npc_service",
    "resolve_buy_npc_id",
    "trade_settle_seconds",
    "execute_trade",
    "bag_read_collapsed",
    "verify_delta",
    "run_badao_exchange",
]
