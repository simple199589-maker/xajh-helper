# -*- coding: utf-8 -*-
"""
PE32 export table helpers for xajh.exe plg symbols.

Resolves mangled export names to RVA so callers can compute VA =
module_base + rva in a live process.

@author by ak
"""
from __future__ import annotations

import struct
from functools import lru_cache
from pathlib import Path

# Full export names as stored in xajh.exe
EXPORT_HOST_MOVE_TO_SCENE_POSITION = "?HostMoveToScenePosition@plg@@YAHHMMM@Z"
EXPORT_GET_CURRENT_SCENE_POSITION = "?GetCurrentScenePosition@plg@@YAXPAHPAM11@Z"
EXPORT_GET_HOST_PLAYER = "?GetHostPlayer@plg@@YAPAXXZ"
EXPORT_GET_OBJECT_I64_STATES = "?GetObjecti64States@plg@@YA_JPAX@Z"
EXPORT_GET_USER_SKILL = "?GetUserSkill@plg@@YAPAVCECUserSkill@@XZ"
EXPORT_GET_USER_SKILL_SEQUENCE = "?GetUserSkillSenquence@plg@@YAPAVCECUserSkillSequence@@XZ"
EXPORT_IS_HOST_PLAYER_DEAD = "?IsHostPlayerDead@plg@@YA_NXZ"
EXPORT_GET_OBJECT_COUNT = "?GetObjectCount@plg@@YAHW4OBJECT_CLASSID@1@@Z"
EXPORT_GET_OBJECTS = "?GetObjects@plg@@YAHW4OBJECT_CLASSID@1@PAXI@Z"
EXPORT_GET_OBJECT_NAME = "?GetObjectName@plg@@YAPB_WPAX@Z"
EXPORT_GET_OBJECT_DIST = "?GetObjectDistToHost@plg@@YAMPAX@Z"
EXPORT_GET_OBJECT_TID = "?GetObjectTemplateID@plg@@YAHPAX@Z"
EXPORT_GET_OBJECT_ID = "?GetObjectID@plg@@YA_JPAX@Z"
EXPORT_PICK_ITEM = "?PickItem@plg@@YAX_J@Z"
EXPORT_SET_TARGET = "?SetTarget@plg@@YA_N_J@Z"
EXPORT_GET_TARGET = "?GetTarget@plg@@YA_JXZ"
EXPORT_GET_MATTER_OWNER_ID = "?GetMatterOwnerID@plg@@YA_JPAX@Z"
EXPORT_GET_MATTER_OWNER_TYPE = "?GetMatterOwnerType@plg@@YAEPAX@Z"
EXPORT_GET_HOST_PLAYER_TEAM = "?GetHostPlayerTeam@plg@@YAPAVCECTeam@@XZ"
# AUI dialog state (memory open/show checks)
EXPORT_GET_GAME_UI_DLG = "?GetGameUIDlg@plg@@YAPAVAUIDialog@@PBD@Z"
EXPORT_IS_DLG_SHOW = "?IsDlgShow@plg@@YA_NPAVAUIDialog@@@Z"
EXPORT_GET_DLG_NAME = "?GetDlgName@plg@@YAPBDPAVAUIDialog@@@Z"
EXPORT_GET_GAME_UI_DLG_NUM = "?GetGameUIDlgNum@plg@@YAHXZ"
EXPORT_GET_GAME_UI_DLGS_NAME = "?GetGameUIDlgsName@plg@@YAHQAPBDH@Z"
EXPORT_GET_GAME_STATE = "?GetGameState@plg@@YAHXZ"
EXPORT_GET_AUI_OBJ_IS_SHOW = "?GetAUIObjIsShow@plg@@YA_NPAVAUIObject@@@Z"
EXPORT_GET_AUI_OBJ_IS_ENABLE = "?GetAUIObjIsEnable@plg@@YA_NPAVAUIObject@@@Z"
EXPORT_TOGGLE_DLG_SHOW = "?ToggleDlgShow@plg@@YAXPAVAUIDialog@@@Z"
# Task interface (CECTaskInterface*)
EXPORT_GET_TASK_INTERFACE = "?GetTaskInterface@plg@@YAPAVCECTaskInterface@@XZ"
# Package / inventory (live 2026-07-16)
EXPORT_GET_PACKAGE_COUNT = "?GetPackageCount@plg@@YAHXZ"
EXPORT_GET_PACKAGE = "?GetPackage@plg@@YAPAXH@Z"
EXPORT_USE_ITEM_IN_PACKAGE = "?UseItemInPackage@plg@@YA_NHH@Z"
EXPORT_BUY_ITEM = "?BuyItem@plg@@YA_N_JPAUbuy_item_from_npc@1@@Z"
EXPORT_NPC_SAY_HELLO = "?NPCSayHello@plg@@YA_N_J@Z"
EXPORT_GET_HOST_PLAYER_CUR_SERV_NPC = "?GetHostPlayerCurServNPC@plg@@YA_JXZ"
# NPC/template config by tid: bool GetCfgObjectInfo(int tid, CfgObjInfo* out)
# out: id@0, scene@+0x30, pos f32 x/y/z @ +0x50/+0x54/+0x58 (live 2026-07-16)
EXPORT_GET_CFG_OBJECT_INFO = "?GetCfgObjectInfo@plg@@YA_NHPAUCfgObjInfo@1@@Z"
CFG_OBJ_INFO_SIZE = 0x80
CFG_OBJ_SCENE_OFF = 0x30
CFG_OBJ_POS_X_OFF = 0x50
CFG_OBJ_POS_Y_OFF = 0x54
CFG_OBJ_POS_Z_OFF = 0x58

# Non-export addresses from community notes.
# Notes list absolute VAs at preferred load base 0x400000.
# Live VA = module_base + (note_va - 0x400000).
DEFAULT_IMAGE_BASE = 0x400000
# ChoiceObject thiscall preferred absolute VA (disasm 0x88F2A0; was wrongly 0xC8F2A0).
NOTE_VA_CHOICE_OBJECT = 0x0088F2A0
NOTE_VA_PICKUP_MATTER = 0x007267F0
NOTE_VA_MATTER_LIST_BASE = 0x004AEF90
NOTE_VA_AUTO_CLICK_NPC = 0x006B2950
NOTE_VA_AUTO_CLICK_MONSTER = 0x006B2A80
NOTE_VA_AUTO_CLICK_MATTER = 0x006B2BB0  # Lua binder only; do not CRT/cdecl call
NOTE_VA_AUTO_CLICK_DYN_MATTER = 0x006B2CE0
# Native path used by AutoClickMatter (thiscall + tid)
NOTE_VA_MATTER_INTERACT = 0x007496B0  # bool thiscall(this, lo, hi, tid)
# Package helper / money / sell (live disasm 2026-07-16, preferred base 0x400000)
NOTE_VA_TASK_IFACE_OR_PKG_ROOT = 0x004AE420  # same helper as task; +0x8 = package mgr
NOTE_VA_GET_PACKAGE_BY_INDEX = 0x005267A0  # thiscall ecx=pkg_mgr, arg=index -> package*
# Host money read: stdcall(package_index) -> edx:eax from package+0x10 / +0x14
# Game UI compares type=2 against 0x186A0 (100000 = 10 金 if 1金=10000).
NOTE_VA_GET_MONEY_BY_PKG = 0x0072AF50
NOTE_VA_GET_MONEY2_BY_PKG = 0x0072AF80  # sibling: package+0x18/+0x1C
# Sell one stack from package: stdcall(pack_idx, slot, count) -> packet type 0xE subtype 2
NOTE_VA_SELL_FROM_PACKAGE = 0x007491F0
# Buy from shop page/slot (sibling of sell): stdcall(page, slot, count, flag) ret 0x10
# Computes booth = tab_base + page*cols + slot then c2s 0xE/1 (live RE 2026-07-22)
NOTE_VA_BUY_FROM_SHOP_SLOT = 0x00749040
NOTE_VA_SELL_PACKET = 0x00CCA450
# UseItem native (thiscall host_inv, pack, slot, flag=1) used by UseItemInPackage export
NOTE_VA_USE_ITEM_NATIVE = 0x0074BF50
# Transmit flag / 飞行旗 (RE 2026-07-21, preferred base 0x400000)
# c2s type 0x86: u8 action(0 del/1 sign/2 fly), u8 page, u8 slot, u8 namelen, name?
NOTE_VA_TRANSMIT_FLAG_PKT = 0x00CC9310
NOTE_VA_TRANSMIT_FLAG_PKT_WRAP = 0x00A4D1C0
# HostPlayer default fly template id table: 10 x u32
HOST_DEFAULT_FLY_IDS_OFF = 0x7DC

# Skill cast / action-lock research (preferred base 0x400000).
# Live PE re-verified 2026-07-17: see .issues/lab/CAST_GATE_ANALYSIS.md
# Community notes (使用技能.md / 技能冷却.md) listed 0x755F10 / 0x756F20 / 0x75C740
# as entries; on this build those VAs are mid-function. Real chain below.
NOTE_VA_GET_HOST_SIDE = 0x004AE400  # real: *[g+0x24]+0x8C
NOTE_VA_HOST_SIDE_HELPER = 0x004AEFA0  # STALE mid-func; do NOT CRT (crashes)
NOTE_VA_HOST_SIDE_HELPER_B = 0x004AEFB0  # STALE mid-func; do NOT CRT
NOTE_VA_SKILL_SESSION_SM = 0x00755E00  # real session state machine (ret 0x10)
NOTE_VA_SKILL_CAST = 0x00755F10  # STALE note VA (mid of SESSION_SM clear path)
NOTE_VA_SKILL_CAST_OUTER = 0x0053E110  # outer cast: GetHostSide -> [+1A88] -> 75F000
NOTE_VA_SKILL_CAST_MAIN = 0x0075F000  # main cast thiscall (ecx=cast-this)
NOTE_VA_SKILL_ID_GATE = 0x0053D120  # cmp request id vs cast+0x10 / +0x80
NOTE_VA_SKILL_USE_INNER = 0x0075C740  # STALE note VA (nearby ctor/clear)
NOTE_VA_SKILL_CD_CHECK = 0x00756F20  # STALE note VA (mid; calls SESSION_SM)
# Host / host-side object offsets for skill this
HOST_SKILL_THIS_OFF = 0x1A88  # [host_or_side + 0x1A88] = skill cast this
HOST_SIDE_INV_OFF = 0x1A84  # inventory / interact this (matter notes)
HOST_SESSION_STATE_OFF = 0x41C  # Lua CancelSession gate; zero after StopSession ack
# Cast-this field offs (lab timeline + 53D120 gate)
CAST_FIELD_SKILL_ID_OFF = 0x10
CAST_FIELD_SKILL_ID_B_OFF = 0x80
CAST_FIELD_FLAGS_OFF = 0x7C
CAST_FIELD_ELAPSED_OFF = 0x20
CAST_FIELD_EXTRA_FLAGS_OFF = 0x4A0  # bit3 tested after 75F000
CAST_SESSION_BLOCK_OFF = 0x1F8  # cleared by 0x756340 / 0x755E00 paths

RVA_CHOICE_OBJECT = NOTE_VA_CHOICE_OBJECT - DEFAULT_IMAGE_BASE  # 0x48F2A0
RVA_PICKUP_MATTER = NOTE_VA_PICKUP_MATTER - DEFAULT_IMAGE_BASE  # 0x3267F0
RVA_MATTER_LIST_BASE = NOTE_VA_MATTER_LIST_BASE - DEFAULT_IMAGE_BASE
RVA_AUTO_CLICK_NPC = NOTE_VA_AUTO_CLICK_NPC - DEFAULT_IMAGE_BASE
RVA_AUTO_CLICK_MONSTER = NOTE_VA_AUTO_CLICK_MONSTER - DEFAULT_IMAGE_BASE
RVA_AUTO_CLICK_MATTER = NOTE_VA_AUTO_CLICK_MATTER - DEFAULT_IMAGE_BASE
RVA_AUTO_CLICK_DYN_MATTER = NOTE_VA_AUTO_CLICK_DYN_MATTER - DEFAULT_IMAGE_BASE
RVA_GET_HOST_SIDE = NOTE_VA_GET_HOST_SIDE - DEFAULT_IMAGE_BASE
RVA_MATTER_INTERACT = NOTE_VA_MATTER_INTERACT - DEFAULT_IMAGE_BASE
# Task (static RE on 2026-07-16 xajh.exe; prefer pattern resolve at runtime)
# jieTask body / accept call target
NOTE_VA_TASK_ACCEPT = 0x00CF0550
# complete-task related (RE WanCheng site call target)
NOTE_VA_TASK_COMPLETE = 0x00AB9B40
# accepted-list materialize: thiscall ecx=*[GetTaskInterface()+0x30]
NOTE_VA_TASK_LIST_ACCEPTED = 0x00493360
# helper used by task code (same global root as GetTaskInterface chain)
NOTE_VA_TASK_IFACE_HELPER = 0x004AE420
# GetTaskName (2026-07-16 PE: Lua binder GetTaskName -> 0x67B510 path)
# c1: cdecl no-arg -> name table ctx (*[g+0x1F8])
# c2: thiscall ecx=ctx, push taskId -> task-desc obj (table at ctx+0x28)
# name: wchar* at *(obj + 0xA98); fallback try obj+8 / obj
NOTE_VA_GET_TASK_NAME_C1 = 0x00C2E470
NOTE_VA_GET_TASK_NAME_C2 = 0x00C4B660
RVA_TASK_ACCEPT = NOTE_VA_TASK_ACCEPT - DEFAULT_IMAGE_BASE
RVA_TASK_COMPLETE = NOTE_VA_TASK_COMPLETE - DEFAULT_IMAGE_BASE
RVA_TASK_LIST_ACCEPTED = NOTE_VA_TASK_LIST_ACCEPTED - DEFAULT_IMAGE_BASE
RVA_TASK_IFACE_HELPER = NOTE_VA_TASK_IFACE_HELPER - DEFAULT_IMAGE_BASE
RVA_GET_TASK_NAME_C1 = NOTE_VA_GET_TASK_NAME_C1 - DEFAULT_IMAGE_BASE
RVA_GET_TASK_NAME_C2 = NOTE_VA_GET_TASK_NAME_C2 - DEFAULT_IMAGE_BASE
# Accepted entry layout (RE_XAJH GetTasksData, re-verified imul 0x7e)
TASK_ENTRY_STRIDE = 0x7E
TASK_ENTRY_ID_OFF = 0x1F
TASK_ENTRY_PROGRESS_OFF = 0x01
# TaskState dword (Lua TaskHelp.TaskState).
# Native: lea entry+1; mov eax,[eax+0x26] => state at entry+0x27.
# (entry+0x26 is misaligned into 0xFF pad + low state byte.)
TASK_ENTRY_STATE_OFF = 0x27
TASK_STATE_FINISHED = 0x01
TASK_STATE_SUCCESS = 0x02
TASK_STATE_GIVEUP = 0x04
# CanFinish(taskId): thiscall ecx=GetTaskInterface*, push id -> AL
# NOTE: preferred VA is 0xC34E90 (rva 0x834E90). Was wrongly 0x834E90 once
# (mapped to rva 0x434E90 / dead code) — names+status looked empty/进行中.
NOTE_VA_TASK_CAN_FINISH = 0x00C34E90
RVA_TASK_CAN_FINISH = NOTE_VA_TASK_CAN_FINISH - DEFAULT_IMAGE_BASE
# GetTaskName: primary wchar* field on desc object
TASK_NAME_WSTR_PTR_OFF = 0xA98
# Panel title front (inline wchar at desc+8) — RE 任务列表.md:
#   add ecx,8 then push; rear part pointer at desc+0xA88.
# Live 2026-07-19: desc+8 holds concrete titles like 初级地宫杀怪 / 140每日杀怪.
TASK_DESC_PANEL_TITLE_OFF = 0x08
TASK_DESC_PANEL_TITLE_REAR_PTR_OFF = 0xA88
# richer titles / objectives used by in-game task panel:
#   +0xA9C  story / location intro (often has 初级地宫/目标怪)
#   +0xAA0  concrete objective line (击杀N只XXX / 限次)
TASK_DESC_STORY_WSTR_PTR_OFF = 0xA9C
TASK_DESC_OBJECTIVE_WSTR_PTR_OFF = 0xAA0
# legacy fallback (older RE / different table)
TASK_NAME_WSTR_OFF = 0x08
# Static task desc (GetTaskName c2 result / template):
# DelvNPC tid @ +0x138 (接/引导 NPC), AwardNPC tid @ +0x13C (交任务 NPC, from 0xC35900)
TASK_DESC_DELV_NPC_OFF = 0x138
TASK_DESC_AWARD_NPC_OFF = 0x13C
# TaskHelp Reach* fields (Lua property builder 0x66FFxx; name labels prior load):
# ReachWorldId u32 @ +0x8D1, ReachSceneId u32 @ +0x91F
# ReachSiteMin_X (stored as int, fild) @ +0x923
# ReachSiteMin_Y/Z f32 @ +0x907 / +0x90B
# ReachSiteMax_X/Y/Z f32 @ +0x90F / +0x913 / +0x917
TASK_DESC_REACH_WORLD_OFF = 0x8D1
TASK_DESC_REACH_SCENE_OFF = 0x91F
TASK_DESC_REACH_MIN_X_OFF = 0x923  # int
TASK_DESC_REACH_MIN_Y_OFF = 0x907  # float
TASK_DESC_REACH_MIN_Z_OFF = 0x90B  # float
TASK_DESC_REACH_MAX_X_OFF = 0x90F  # float
TASK_DESC_REACH_MAX_Y_OFF = 0x913  # float
TASK_DESC_REACH_MAX_Z_OFF = 0x917  # float
# Accepted list entry: runtime/static template ptr
# CanFinish: lea entry+1; mov esi,[edi+0x3A] => ptr at entry+0x3B
TASK_ENTRY_TMPL_PTR_OFF = 0x3B
# GetTaskInterface*: sub-object for list call
TASK_IFACE_LIST_THIS_OFF = 0x30
# jieTask this: *[game_root + TASK_MGR_THIS_OFF]
TASK_MGR_THIS_OFF = 0x2C
# Object id at CECMatter-like +0x140 (u64)
OBJ_ID_OFF = 0x140

DEFAULT_XAJH_CANDIDATES = (
    Path(r"D:/WeGameApps/笑傲江湖OL/bin/xajh.exe"),
)


def note_va_to_live(module_base: int, note_va: int) -> int:
    """Map notes absolute VA to live process VA. @author by ak"""
    rva = int(note_va) - DEFAULT_IMAGE_BASE
    if rva < 0:
        raise ValueError(f"note_va 0x{note_va:X} below default base")
    return int(module_base) + rva


def find_xajh_exe(hint: str | Path | None = None) -> Path | None:
    """Locate xajh.exe from attach hint or default install path. @author by ak"""
    if hint:
        p = Path(hint)
        if p.is_file() and p.name.lower() == "xajh.exe":
            return p
        if p.is_dir():
            cand = p / "xajh.exe"
            if cand.is_file():
                return cand
            cand = p / "bin" / "xajh.exe"
            if cand.is_file():
                return cand
    for c in DEFAULT_XAJH_CANDIDATES:
        if c.is_file():
            return c
    return None


@lru_cache(maxsize=4)
def list_exports(pe_path: str) -> dict[str, int]:
    """
    Parse PE32 export directory: name -> RVA.

    Returns empty dict on parse failure.
    @author by ak
    """
    path = Path(pe_path)
    if not path.is_file():
        return {}
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return {}
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 + 100 >= len(data):
        return {}
    magic = struct.unpack_from("<H", data, e_lfanew + 24)[0]
    if magic != 0x10B:  # PE32 only (xajh is x86)
        return {}
    export_rva = struct.unpack_from("<I", data, e_lfanew + 24 + 96)[0]
    if not export_rva:
        return {}
    nsec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    sec_off = e_lfanew + 24 + opt_size
    sections: list[tuple[int, int, int, int]] = []
    for i in range(nsec):
        o = sec_off + i * 40
        if o + 40 > len(data):
            break
        vsize, va, rsize, ro = struct.unpack_from("<IIII", data, o + 8)
        sections.append((va, vsize, ro, rsize))

    def rva_to_off(rva: int) -> int | None:
        for va, vsize, ro, rsize in sections:
            span = max(vsize, rsize)
            if va <= rva < va + span:
                return ro + (rva - va)
        return None

    off = rva_to_off(export_rva)
    if off is None or off + 40 > len(data):
        return {}
    nnames = struct.unpack_from("<I", data, off + 24)[0]
    aof = struct.unpack_from("<I", data, off + 28)[0]
    aon = struct.unpack_from("<I", data, off + 32)[0]
    aoo = struct.unpack_from("<I", data, off + 36)[0]
    aof_off = rva_to_off(aof)
    aon_off = rva_to_off(aon)
    aoo_off = rva_to_off(aoo)
    if None in (aof_off, aon_off, aoo_off):
        return {}
    out: dict[str, int] = {}
    for i in range(nnames):
        name_rva = struct.unpack_from("<I", data, aon_off + i * 4)[0]
        name_off = rva_to_off(name_rva)
        if name_off is None:
            continue
        end = data.find(b"\x00", name_off)
        if end < 0:
            continue
        try:
            name = data[name_off:end].decode("ascii")
        except Exception:
            continue
        ordinal_index = struct.unpack_from("<H", data, aoo_off + i * 2)[0]
        func_rva = struct.unpack_from("<I", data, aof_off + ordinal_index * 4)[0]
        out[name] = int(func_rva)
    return out


def resolve_export_rva(pe_path: str | Path, export_name: str) -> int | None:
    """Return export RVA or None. @author by ak"""
    exports = list_exports(str(pe_path))
    return exports.get(export_name)


def resolve_export_symbol(
    pe_path: str | Path,
    module_base: int,
    export_name: str,
    *,
    key: str | None = None,
):
    """Return a provenance-bearing export result (compatibility facade)."""
    from app.core.symbol_resolver import resolve_symbol

    return resolve_symbol(
        key or export_name,
        pe_path=pe_path,
        module_base=int(module_base),
        export_name=export_name,
    )


def resolve_profile_symbol(
    pe_path: str | Path,
    module_base: int,
    key: str,
    *,
    pattern: str | None = None,
):
    """Resolve a pattern/profile symbol without unsafe note-VA fallback."""
    from app.core.symbol_resolver import resolve_symbol

    return resolve_symbol(
        key,
        pe_path=pe_path,
        module_base=int(module_base),
        pattern=pattern,
    )


def resolve_plg_vas(module_base: int, pe_path: str | Path) -> dict[str, int]:
    """
    Map short keys to absolute VA for live process.

    Keys: host_move, get_scene_pos, get_host
    @author by ak
    """
    exports = list_exports(str(pe_path))
    mapping = {
        "host_move": EXPORT_HOST_MOVE_TO_SCENE_POSITION,
        "get_scene_pos": EXPORT_GET_CURRENT_SCENE_POSITION,
        "get_host": EXPORT_GET_HOST_PLAYER,
        "get_task_iface": EXPORT_GET_TASK_INTERFACE,
    }
    out: dict[str, int] = {}
    base = int(module_base)
    for key, full in mapping.items():
        rva = exports.get(full)
        if rva is not None:
            out[key] = base + int(rva)
    return out
