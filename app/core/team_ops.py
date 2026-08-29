# -*- coding: utf-8 -*-
"""
Team invite / leave / follow helpers (live-confirmed native packets).

RE (x86 preferred base 0x400000, 2026-07-22 live disasm):
  Invite c2s 0x1261 via ZEROED pkt + 0xCD04D0 (host side-obj 0x4AE440).
  CRITICAL: CEC4E0 leaves pkt+0x20 uncleared; serialize@CF56B0 still sends it → reject.
  Name→id: GNET GetPlayerIDByName type 0x76 builder 0xCF0620(host, wname*, reason),
    response 0x77 jump table:
      reason=1 → CEC240 c2s 0x125c  (Win_TeamInvite 输名字邀请，与 UI 一致)
      reason=2 → cache id only
      reason=8 → Win_ChatInvite list (NOT team)
    Id invite: zeroed c2s 0x1261 via CD04D0 (right-click / GameApi path; may need AOI).
  After 0x77, id is in world name-cache; lookup 0x499DE0(world) with wchar* → edx:eax.
  Also resolve via multi live processes / nearby AOI.
  Leave 0xCEC460; Follow 0xCEC8A0.
  Follow UI: enable via Game_TeamFollow confirm Btn_OK. Cancel via the active
    Win_BindStatus 组队跟随 row's Btn_QuitN. Packet callbacks 0x9A0680 /
    0x9A06A0 -> 0xCEC8A0 -> CD04D0 (c2s 0x1286). Wire trace 2026-07-31:
    12-byte C2S, last byte = flag 1/0; host id LE at +8.
  Team flags+品质 0x1263 via CEC560 (AutoAdmit/MemberInvite + Conbo_Pinzhi).
  Pinzhi in mode_word: (2<<8)|{0自由,1精良,4优质,7完美}; default 优质=0x0204.
  Loot 0x1273 CEC7A0 is alternate path (not team panel primary).

@author by ak
"""
from __future__ import annotations

import json
import math
import re
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from app.core.plg_exports import DEFAULT_IMAGE_BASE, EXPORT_GET_HOST_PLAYER
from app.core.plg_objects import CLASS_PLAYER, list_class_objects
from app.core.remote_runtime import remote_call_cdecl_x86, remote_call_thiscall_x86

LogFn = Callable[[str], None]


def _pid_blocked(session_or_pid) -> tuple[bool, str]:
    """True when SafeDispatch/remote gate blocks this game pid. @author by ak"""
    try:
        from app.core.safe_dispatch import session_blocked

        return session_blocked(session_or_pid)
    except Exception:
        return False, ""


NOTE_VA_GET_HOST_SIDE_OBJ = 0x004AE440  # -> host side-obj* (team pkt this)
NOTE_VA_GET_SIDE = 0x004AE400  # -> game side* (+0x18CC = CECTeam*)
NOTE_VA_GET_WORLD = 0x004AE3B0  # -> world* (name cache owner)
NOTE_VA_TEAM_INVITE = 0x00CEC4E0  # thiscall host, (id_lo, id_hi) -> builds 0x1261
NOTE_VA_TEAM_LEAVE = 0x00CEC460  # thiscall host, (leader_lo, leader_hi) action0
NOTE_VA_TEAM_FOLLOW = 0x00CEC8A0  # thiscall host, (u8 on) -> 0x1286
NOTE_VA_TEAM_SET_FLAGS = 0x00CEC560  # thiscall host, (struct*) -> 0x1263
NOTE_VA_TEAM_SET_LOOT = 0x00CEC7A0  # thiscall host, (apply, main, sub, extra) -> 0x1273
NOTE_VA_PKT_SEND = 0x00CD04D0  # thiscall host, (pkt*, flag) 实际入网
NOTE_VA_GET_PLAYER_ID_BY_NAME = 0x00CF0620  # thiscall host, (wchar* name, u8 reason) c2s 0x76
NOTE_VA_NAME_CACHE_LOOKUP = 0x00499DE0  # thiscall world, (wchar* name) -> id in edx:eax

# GetPlayerIDByName reason (s2c 0x77 jump table @ 0xDC4F08, reason-1 index)
NAME_QUERY_REASON_TEAM_INVITE = 1  # Win_TeamInvite 输名邀请 → 回包后 CEC240/0x125c
NAME_QUERY_REASON_CACHE_ONLY = 2  # 只缓存 id，无副作用
NAME_QUERY_REASON_CHAT_INVITE = 8  # Win_ChatInvite 列表，不是组队邀请

# side-host layout (NOT GetHostPlayer*):
#   +0x240 / +0x244 : self id lo/hi  (invite 发包读这里)
# GetHostPlayer* 的 id 在 +0x140，+0x240 是别的脏数据，不能当 team this。
HOST_SIDE_OFF_ID_LO = 0x240
HOST_SIDE_OFF_ID_HI = 0x244

# c2s TeamInvite 对象 (size 0x28), serialize@CF56B0:
#   +0x00 vtable 0x12C62F0
#   +0x04 type   0x1261
#   +0x08 / +0x0C zero
#   +0x10 / +0x14 inviter id (int64)
#   +0x18 / +0x1C invitee id (int64)
#   +0x20 u32     协议必带；CEC4E0 未清零，CRT 栈垃圾会导致服务端判无效
TEAM_INVITE_PKT_SIZE = 0x28
TEAM_INVITE_VT = 0x012C62F0
TEAM_INVITE_TYPE = 0x1261

# CECTeam live layout (confirmed 2026-07-25):
#   +0x10/+0x14 leader id
#   +0x34 member* array
#   +0x38 member count
# CECTeamMember:
#   +0x14 team*
#   +0x18/+0x1C player id
#   +0x20 wchar* name
TEAM_OFF_MEMBER_PTRS = 0x34
TEAM_OFF_MEMBER_COUNT = 0x38
TEAM_MEMBER_OFF_ID = 0x18
TEAM_MEMBER_OFF_NAME_PTR = 0x20
# Wire-verified 2026-08-09: follow-active flag on the followed member entry.
TEAM_MEMBER_OFF_FOLLOW_FLAG = 0x40
TEAM_MEMBER_FOLLOW_BIT = 0x100

# Team setting dialog (Win_TeamSetting):
#   Chk_AutoAdmit (+0x288) / Chk_MemberInvite (+0x350) → CEC560 / c2s 0x1263
#   struct: +0x04 u16 mode_word, +0x06 auto_admit, +0x07 member_invite
# 组队面板 Conbo_Pinzhi 品质走 c2s 0x1263（与允许邀请/自动招人同一包）：
#   mode_word = (2 << 8) | pinzhi, pinzhi: 0=自由拾取, 1=精良, 4=优质, 7=完美
# 另有 CEC7A0 / 0x1273 为独立拾取规则窗（非本面板主路径）。
TEAM_LOOT_MAIN_TEAM = 6  # 0x1273 队伍分配（备用）
TEAM_LOOT_SUB_FREE = 0  # 自由拾取
TEAM_LOOT_SUB_JINGLIANG = 1  # 精良
TEAM_LOOT_SUB_YOUZI = 4  # 优质
TEAM_LOOT_SUB_WANMEI = 7  # 完美
TEAM_LOOT_SUB_DEFAULT = TEAM_LOOT_SUB_YOUZI  # 队长默认：优质
TEAM_FLAGS_MODE_HI = 2  # UI: ebx=2 when Conbo has selection text

TEAM_SIDE_OFF = 0x18CC
TEAM_OFF_LEADER_ID = 0x10

_SPLIT_RE = re.compile(r"[,，、;；/\s]+")
_ID_RE = re.compile(r"^\d+$")


@dataclass
class TeamOpResult:
    """One team op outcome. @author by ak"""

    ok: bool
    action: str = ""
    message: str = ""
    error: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TeamMemberTarget:
    """Resolved invite target. @author by ak"""

    name: str
    obj_id: int
    source: str = ""  # multi | nearby | literal_id
    pid: int | None = None  # multi-box peer pid when known


def _note_va(session, note_va: int) -> int:
    base = int(getattr(session, "module_base", 0) or 0)
    if not base:
        raise RuntimeError("session has no module_base; attach first")
    return int(base + (int(note_va) - int(DEFAULT_IMAGE_BASE))) & 0xFFFFFFFF


def parse_team_member_names(text: str) -> list[str]:
    """
    Parse member names from UI text (顿号/逗号/空格分隔).

    Keeps order, drops empty, de-dupes case-sensitively.
    @author by ak
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT_RE.split(raw):
        name = (part or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def is_literal_member_id(token: str) -> bool:
    """True if token is pure decimal player id text. @author by ak"""
    return bool(_ID_RE.match(str(token or "").strip()))


def parse_literal_member_id(token: str) -> int | None:
    """
    Parse a pure-digit member token as player id.

    No range / validity checks: user-supplied ids are trusted as-is.
    @author by ak
    """
    s = str(token or "").strip()
    if not _ID_RE.match(s):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def names_match(a: str, b: str) -> bool:
    """Exact display-name match (strip only). @author by ak"""
    return str(a or "").strip() == str(b or "").strip() and bool(str(a or "").strip())


def host_name_in_members(host_name: str, members: Iterable[str] | str) -> bool:
    """
    True if host_name is listed in the team roster.

    Empty roster → False (fail closed for filtered group control).
    @author by ak
    """
    host = str(host_name or "").strip()
    if not host:
        return False
    if isinstance(members, str):
        names = parse_team_member_names(members)
    else:
        names = [str(n).strip() for n in (members or []) if str(n).strip()]
    if not names:
        return False
    return any(names_match(host, n) for n in names)


def host_in_team_members(
    host_name: str,
    members: Iterable[str] | str,
    *,
    host_id: int | None = None,
) -> bool:
    """
    True if this character is in the roster by display name or numeric id.

    Used by group-control filter so pure-id lists still hit the right slaves.
    Empty roster → False.
    @author by ak
    """
    if host_name_in_members(host_name, members):
        return True
    try:
        hid = int(host_id or 0)
    except Exception:
        hid = 0
    if hid <= 0:
        return False
    if isinstance(members, str):
        tokens = parse_team_member_names(members)
    else:
        tokens = [str(n).strip() for n in (members or []) if str(n).strip()]
    hid_s = str(hid)
    return any(_ID_RE.match(t) and t == hid_s for t in tokens)


def get_host_player_for_team(session, *, log: LogFn | None = None) -> int:
    """
    Host side-object* for team packet builders (note 0x4AE440).

    Do NOT fall back to GetHostPlayer export: its +0x240 is not the player id
    field that invite/leave serializers read (they use side-obj +0x240).
    @author by ak
    """
    log = log or (lambda _m: None)
    pid = int(session.pid)
    try:
        va = _note_va(session, NOTE_VA_GET_HOST_SIDE_OBJ)
        host = int(remote_call_cdecl_x86(pid, va, [], timeout_ms=2500) or 0) & 0xFFFFFFFF
        if host:
            return host
    except Exception as e:
        log(f"team host note err: {e}")
    return 0


def read_host_side_id_pair(session, host: int | None = None, *, log: LogFn | None = None) -> tuple[int, int]:
    """Read inviter id lo/hi from side-host +0x240/+0x244. @author by ak"""
    log = log or (lambda _m: None)
    if not host:
        host = get_host_player_for_team(session, log=log)
    host = int(host or 0) & 0xFFFFFFFF
    if not host:
        return 0, 0
    try:
        from app.core.remote_runtime import remote_read_bytes
        import struct

        raw = remote_read_bytes(int(session.pid), host + HOST_SIDE_OFF_ID_LO, 8)
        if len(raw) < 8:
            return 0, 0
        lo, hi = struct.unpack("<II", raw)
        return int(lo) & 0xFFFFFFFF, int(hi) & 0xFFFFFFFF
    except Exception as e:
        log(f"team read side id err: {e}")
        return 0, 0


def read_host_identity(
    session,
    *,
    need_name: bool = True,
    log: LogFn | None = None,
) -> tuple[str, int]:
    """
    Return (host_display_name, host_obj_id64).

    need_name=False: only GetHostPlayer + RPM id (no GetObjectName CRT).
    Prefer this for pure-id 校验队伍 — GetObjectName is a known hang hotspot
    under concurrent remote load.

    @author by ak
    """
    log = log or (lambda _m: None)
    name = ""
    oid = 0
    try:
        from app.core.plg_ui import (
            get_host_player_id,
            get_host_player_name,
            get_host_player_ptr,
        )

        # One GetHostPlayer CRT, then RPM for id; name CRT only when required.
        host = get_host_player_ptr(session, log=log)
        lo, hi = get_host_player_id(session, host_ptr=host, log=log)
        if lo or hi:
            oid = (int(hi) << 32) | (int(lo) & 0xFFFFFFFF)
        if need_name:
            name = str(
                get_host_player_name(session, host_ptr=host, log=log) or ""
            ).strip()
    except Exception as e:
        log(f"team identity err: {e}")
    return name, int(oid)


def leave_team(session, *, log: LogFn | None = None) -> TeamOpResult:
    """
    Leave current party if any (c2s 0x1260 action=0 via CEC460).

    WARNING: if host is the leader, server typically promotes another member
    (看起来像「把队长给别人了」). Callers that only want to invite must NOT
    leave while already captain. Change-leader is a different API (CEC490).

    Safe no-op when not in a team.
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(ok=False, action="leave", message=f"远程不可用: {brsn}", error=brsn or "remote_blocked")
    pid = int(session.pid)
    try:
        from app.core.plg_ui import get_host_team_ptr, get_team_leader_id

        team = int(get_host_team_ptr(session, log=log) or 0) & 0xFFFFFFFF
        if not team:
            return TeamOpResult(
                ok=True,
                action="leave",
                message="当前未组队",
                detail={"in_team": False},
            )
        leader_lo, leader_hi = get_team_leader_id(session, team_ptr=team, log=log)
        host = get_host_player_for_team(session, log=log)
        if not host:
            return TeamOpResult(ok=False, action="leave", message="host null", error="no_host")
        va = _note_va(session, NOTE_VA_TEAM_LEAVE)
        ret = remote_call_thiscall_x86(
            pid,
            va,
            host,
            [int(leader_lo) & 0xFFFFFFFF, int(leader_hi) & 0xFFFFFFFF],
            timeout_ms=4000,
        )
        msg = (
            f"leave team leader={leader_lo:X}:{leader_hi:X} "
            f"host=0x{host:X} ret={ret}"
        )
        log(f"team: {msg}")
        invalidate_team_states(pid)
        return TeamOpResult(
            ok=True,
            action="leave",
            message="已离队",
            detail={
                "in_team": True,
                "leader_lo": int(leader_lo),
                "leader_hi": int(leader_hi),
                "host": host,
                "ret": int(ret or 0),
            },
        )
    except Exception as e:
        log(f"team: leave failed: {e}")
        return TeamOpResult(ok=False, action="leave", message=str(e), error=str(e))



def leader_id_u64(leader_lo: int, leader_hi: int) -> int:
    """Pack team leader id lo/hi into int. @author by ak"""
    return ((int(leader_hi) & 0xFFFFFFFF) << 32) | (int(leader_lo) & 0xFFFFFFFF)


def leave_team_unless_under_leader(
    session,
    keep_leader_id: int | None = None,
    *,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Leave only when not already under the given leader id.

    keep_leader_id=None/0 → always leave (same as leave_team).
    Used by 自动组队 so members already under the captain stay put.
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        kid = int(keep_leader_id or 0)
    except Exception:
        kid = 0
    if not kid:
        return leave_team(session, log=log)
    try:
        from app.core.plg_ui import get_host_team_ptr, get_team_leader_id

        team = int(get_host_team_ptr(session, log=log) or 0) & 0xFFFFFFFF
        if not team:
            return TeamOpResult(
                ok=True,
                action="leave",
                message="当前未组队",
                detail={"in_team": False, "kept": False},
            )
        lo, hi = get_team_leader_id(session, team_ptr=team, log=log)
        cur = leader_id_u64(lo, hi)
        if cur == kid:
            msg = f"已在目标队长队伍，跳过离队 leader={kid}"
            log(f"team: {msg}")
            return TeamOpResult(
                ok=True,
                action="leave",
                message="已在本队，跳过离队",
                detail={"in_team": True, "kept": True, "leader_id": cur},
            )
    except Exception as e:
        log(f"team: leave_unless check err: {e}")
    return leave_team(session, log=log)


def should_leave_before_form(session, *, log: LogFn | None = None) -> bool:
    """
    True when host is stuck in someone else's party (in team but not leader).

    自动组队 uses this; 立即邀请 does not leave.
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.plg_ui import is_host_in_team, is_host_team_leader

        if not is_host_in_team(session, log=log):
            return False
        if is_host_team_leader(session, log=log):
            return False
        return True
    except Exception as e:
        log(f"team: should_leave check err: {e}")
        return False



def ensure_host_captain_or_solo(
    session,
    *,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Before mass invite: host must be solo or already team leader.

    Safety rules (防止「点邀请却把队长交出去」):
      - already leader → never leave
      - solo → ok
      - member of someone else's team → leave once, then invite
      - in team but role unknown → refuse (do NOT leave; leader leave promotes next)
    Note: TeamChangeLeader is CEC490 action=1; invite is CEC4E0/0x1261 (different).
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        from app.core.plg_ui import host_team_role

        role = host_team_role(session, log=log)
        log(
            "team: role "
            f"in_team={role.get('in_team')} role={role.get('role')} "
            f"host={int(role.get('host_lo') or 0):X}:{int(role.get('host_hi') or 0):X} "
            f"leader={int(role.get('leader_lo') or 0):X}:{int(role.get('leader_hi') or 0):X}"
        )
        if not role.get("in_team"):
            return TeamOpResult(
                ok=True,
                action="ensure_captain",
                message="本号未组队，可建队邀请",
                detail={"in_team": False, "left": False, "role": role},
            )
        if role.get("role") == "leader" or role.get("is_leader"):
            return TeamOpResult(
                ok=True,
                action="ensure_captain",
                message="本号已是队长（不会离队/交队长）",
                detail={"in_team": True, "is_leader": True, "left": False, "role": role},
            )
        if role.get("role") == "unknown":
            return TeamOpResult(
                ok=False,
                action="ensure_captain",
                message="无法确认队长身份，已取消（防止误离队导致队长移交）",
                error="team_role_unknown",
                detail={"in_team": True, "is_leader": False, "left": False, "role": role},
            )
        # confirmed member of someone else's party
        log("team: host is member (not leader) — leave before invite")
        lr = leave_team(session, log=log)
        time.sleep(0.3)
        return TeamOpResult(
            ok=bool(lr.ok),
            action="ensure_captain",
            message="本号原在别人队里，已先离队以便自己当队长邀请",
            error=lr.error,
            detail={
                "in_team": True,
                "is_leader": False,
                "left": True,
                "leave": lr.to_dict(),
                "role": role,
            },
        )
    except Exception as e:
        log(f"team: ensure_captain err: {e}")
        return TeamOpResult(
            ok=False,
            action="ensure_captain",
            message=str(e),
            error=str(e),
        )



def _read_utf16_z(pm, addr: int, max_chars: int = 24) -> str:
    """Read remote utf-16le C-string. @author by ak"""
    try:
        addr = int(addr or 0) & 0xFFFFFFFF
    except Exception:
        return ""
    if not addr or pm is None:
        return ""
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, max(2, int(max_chars) * 2 + 2))
    except Exception:
        return ""
    out: list[str] = []
    for i in range(0, len(raw) - 1, 2):
        ch = int.from_bytes(raw[i : i + 2], "little")
        if ch == 0:
            break
        if ch < 32 and ch not in (9, 10, 13):
            break
        try:
            out.append(chr(ch))
        except Exception:
            break
    s = "".join(out).strip()
    if not s or len(s) > int(max_chars):
        return ""
    return s


def _looks_like_role_name(s: str) -> bool:
    """Strict role-name filter to avoid CECTeam memory garbage. @author by ak"""
    s = str(s or "").strip()
    if not s or len(s) < 2 or len(s) > 12:
        return False
    bad = set("/\\.[](){}<>|")
    if any(ch in s for ch in bad) or ".dds" in s or s.startswith("Win_") or s.startswith("Btn_") or s.startswith("Txt_"):
        return False
    cjk = 0
    latin = 0
    for c in s:
        o = ord(c)
        if 0x4E00 <= o <= 0x9FFF or c in "丶々·":
            cjk += 1
            continue
        if c.isascii() and c.isalpha():
            latin += 1
            continue
        if c.isdigit() or c in (" ", "-", "_"):
            continue
        return False
    if cjk < 2:
        return False
    if latin >= 1:
        return False
    return True


def _extract_members_from_team_blob(
    session,
    team_ptr: int,
    *,
    host_id: int = 0,
    leader_id: int = 0,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Best-effort party member names/ids from CECTeam memory.

    Does not require multi-box peers. Safe RPM only (no extra CRT).
    @author by ak
    """
    log = log or (lambda _m: None)
    pm = getattr(session, "pm", None)
    team = int(team_ptr or 0) & 0xFFFFFFFF
    if not pm or not team:
        return []
    try:
        import pymem.memory
        import struct

        raw = pymem.memory.read_bytes(pm.process_handle, team, 0x300)
    except Exception as e:
        log(f"team: mem members read err: {e}")
        return []

    found: list[dict] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()

    def _push(name: str, oid: int, *, source: str) -> None:
        n = str(name or "").strip()
        try:
            i = int(oid or 0)
        except Exception:
            i = 0
        if i > 0 and i in seen_ids:
            if n and n not in seen_names:
                # upgrade empty name
                for item in found:
                    if int(item.get("obj_id") or 0) == i and not item.get("name"):
                        item["name"] = n
                        seen_names.add(n)
            return
        if n and n in seen_names and i <= 0:
            return
        if i > 0:
            seen_ids.add(i)
        if n:
            seen_names.add(n)
        if not n and i <= 0:
            return
        found.append(
            {
                "name": n or (str(i) if i else "?"),
                "obj_id": i,
                "source": source,
            }
        )

    # 1) common count + array pointer pairs (live probe candidates)
    try:
        import struct

        pairs = ((0x34, 0x38), (0x30, 0x38), (0x2C, 0x30), (0x80, 0x18), (0x20, 0x18))
        for ptr_off, cnt_off in pairs:
            if ptr_off + 4 > len(raw) or cnt_off + 4 > len(raw):
                continue
            p = struct.unpack_from("<I", raw, ptr_off)[0]
            c = struct.unpack_from("<I", raw, cnt_off)[0]
            if not (1 <= c <= 6):
                continue
            if not (0x10000 < p < 0x7FFFFFFF):
                continue
            # try a few fixed strides
            for stride in (0x30, 0x28, 0x20, 0x40, 0x48, 0x50):
                got = 0
                for idx in range(int(c)):
                    base = (p + idx * stride) & 0xFFFFFFFF
                    try:
                        import pymem.memory

                        slot = pymem.memory.read_bytes(pm.process_handle, base, stride)
                    except Exception:
                        break
                    if len(slot) < 8:
                        break
                    lo, hi = struct.unpack_from("<II", slot, 0)
                    oid = (int(hi) << 32) | (int(lo) & 0xFFFFFFFF)
                    # skip garbage ids
                    if oid <= 0 or oid > 0xFFFFFFFFFFFF:
                        # maybe id at +0x08
                        if len(slot) >= 16:
                            lo, hi = struct.unpack_from("<II", slot, 8)
                            oid = (int(hi) << 32) | (int(lo) & 0xFFFFFFFF)
                    name = ""
                    # name may be inline wstr or pointer
                    for noff in (0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C):
                        if noff + 4 > len(slot):
                            break
                        # pointer?
                        ptrn = struct.unpack_from("<I", slot, noff)[0]
                        if 0x10000 < ptrn < 0x7FFFFFFF:
                            name = _read_utf16_z(pm, ptrn)
                            if _looks_like_role_name(name):
                                break
                        # inline?
                        try:
                            chunk = slot[noff : noff + 24]
                            # utf16
                            chars = []
                            for i in range(0, len(chunk) - 1, 2):
                                ch = int.from_bytes(chunk[i : i + 2], "little")
                                if ch == 0:
                                    break
                                if ch < 32:
                                    chars = []
                                    break
                                chars.append(chr(ch))
                            cand = "".join(chars).strip()
                            if _looks_like_role_name(cand):
                                name = cand
                                break
                        except Exception:
                            pass
                    if oid > 0 or name:
                        _push(name, oid if oid < (1 << 40) else 0, source="team_mem")
                        got += 1
                if got >= 1:
                    break
            if found:
                break
    except Exception as e:
        log(f"team: mem array scan err: {e}")

    # 2) fallback: collect readable role-like wstrings near team object
    if len(found) <= 1:
        try:
            import struct

            for off in range(0, min(len(raw) - 4, 0x280), 4):
                ptrn = struct.unpack_from("<I", raw, off)[0]
                if not (0x10000 < ptrn < 0x7FFFFFFF):
                    continue
                name = _read_utf16_z(pm, ptrn)
                if _looks_like_role_name(name):
                    _push(name, 0, source="team_str")
        except Exception:
            pass

    # annotate leader/self by id when possible
    out = []
    for item in found:
        oid = int(item.get("obj_id") or 0)
        out.append(
            {
                "name": item.get("name") or "",
                "obj_id": oid,
                "is_self": bool(host_id and oid and oid == int(host_id)),
                "is_leader": bool(leader_id and oid and oid == int(leader_id)),
                "pid": None,
                "source": item.get("source") or "team_mem",
            }
        )
    return out


def read_cecteam_members(
    session,
    *,
    store=None,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Pure CECTeam member-array reader (no StateDispatch cache).

    Prefer list_party_members() / StateKind.PARTY_MEMBERS for business callers.
    Layout (live-confirmed):
      CECTeam+0x34 = member*[]
      CECTeam+0x38 = count
      member+0x18  = player id (int64)
      member+0x20  = name wchar*

    Each item: {name, obj_id, is_self, is_leader, pid, source}
    Empty list if host is not in a team.
    @author by ak
    """
    log = log or (lambda _m: None)
    _ = store  # reserved: optional multi pid map (avoid CRT in producer)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        log(f"team: read_cecteam blocked: {brsn}")
        return []
    try:
        from app.core.plg_ui import (
            get_host_team_ptr,
            get_team_leader_id,
            is_host_team_leader,
        )
        from app.core.plg_objects import read_wstr
        import struct
        import pymem.memory
    except Exception as e:
        log(f"team: list_party import err: {e}")
        return []

    team = int(get_host_team_ptr(session, log=log) or 0) & 0xFFFFFFFF
    if not team:
        return []

    host_name, host_id = read_host_identity(session, need_name=True, log=log)
    try:
        host_id_i = int(host_id or 0)
    except Exception:
        host_id_i = 0
    leader_lo, leader_hi = get_team_leader_id(session, team_ptr=team, log=log)
    leader_u = leader_id_u64(leader_lo, leader_hi)
    host_is_leader = False
    try:
        host_is_leader = bool(is_host_team_leader(session, log=log))
    except Exception:
        host_is_leader = bool(host_id_i and host_id_i == leader_u)

    pm = getattr(session, "pm", None)
    if pm is None:
        log("team: list_party no pm")
        return []

    try:
        head = pymem.memory.read_bytes(pm.process_handle, team, 0x40)
        arr_ptr = struct.unpack_from("<I", head, TEAM_OFF_MEMBER_PTRS)[0]
        count = struct.unpack_from("<I", head, TEAM_OFF_MEMBER_COUNT)[0]
    except Exception as e:
        log(f"team: list_party head err: {e}")
        return []

    if not arr_ptr or count <= 0 or count > 12:
        log(f"team: list_party bad arr=0x{int(arr_ptr or 0):X} count={count}")
        # fallback: at least self
        if host_id_i or host_name:
            return [
                {
                    "name": host_name or str(host_id_i),
                    "obj_id": host_id_i,
                    "is_self": True,
                    "is_leader": host_is_leader,
                    "pid": int(getattr(session, "pid", 0) or 0) or None,
                    "source": "self",
                }
            ]
        return []

    try:
        tab = pymem.memory.read_bytes(pm.process_handle, int(arr_ptr) & 0xFFFFFFFF, int(count) * 4)
    except Exception as e:
        log(f"team: list_party tab err: {e}")
        return []

    # optional pid map from multi store by host id
    id_to_pid: dict[int, int] = {}
    if store is not None:
        try:
            for sess in list(store.list() or []):
                try:
                    spid = int(getattr(sess, "pid", 0) or 0)
                except Exception:
                    continue
                if not spid:
                    continue
                # title hint is not authoritative for id; skip CRT here
                # pid map filled only when member id matches later via multi identity cache if any
                _ = spid
        except Exception:
            pass

    out: list[dict] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for i in range(int(count)):
        try:
            mptr = struct.unpack_from("<I", tab, i * 4)[0]
        except Exception:
            continue
        mptr = int(mptr or 0) & 0xFFFFFFFF
        if not mptr:
            continue
        try:
            mraw = pymem.memory.read_bytes(pm.process_handle, mptr, 0x28)
        except Exception:
            continue
        try:
            mlo = struct.unpack_from("<I", mraw, TEAM_MEMBER_OFF_ID)[0]
            mhi = struct.unpack_from("<I", mraw, TEAM_MEMBER_OFF_ID + 4)[0]
            oid = (int(mhi) << 32) | (int(mlo) & 0xFFFFFFFF)
            np = struct.unpack_from("<I", mraw, TEAM_MEMBER_OFF_NAME_PTR)[0]
        except Exception:
            continue
        if oid <= 0:
            continue
        if oid in seen_ids:
            continue
        name = ""
        try:
            if np:
                name = str(read_wstr(pm, int(np) & 0xFFFFFFFF, max_chars=24) or "").strip()
        except Exception:
            name = ""
        # basic name sanity: prefer non-empty CJK/role-like; keep id fallback
        if name and not _looks_like_role_name(name):
            # still accept if pure CJK-ish failed only due to length 1
            if not any(0x4E00 <= ord(c) <= 0x9FFF for c in name):
                name = ""
        if not name:
            if host_id_i and oid == host_id_i and host_name:
                name = host_name
            else:
                name = f"id={oid}"
        if name in seen_names and oid not in seen_ids:
            # same display name rare; keep with id suffix only if needed
            pass
        seen_ids.add(oid)
        if name:
            seen_names.add(name)
        is_self = bool(host_id_i and oid == host_id_i)
        is_leader = bool(leader_u and oid == leader_u)
        if not is_leader and host_is_leader and is_self:
            is_leader = True
        out.append(
            {
                "name": name,
                "obj_id": int(oid),
                "is_self": is_self,
                "is_leader": is_leader,
                "pid": int(getattr(session, "pid", 0) or 0) if is_self else id_to_pid.get(int(oid)),
                "source": "team_mem",
            }
        )

    # Ensure self appears even if member array briefly incomplete.
    if host_id_i and host_id_i not in seen_ids:
        out.append(
            {
                "name": host_name or f"id={host_id_i}",
                "obj_id": host_id_i,
                "is_self": True,
                "is_leader": host_is_leader,
                "pid": int(getattr(session, "pid", 0) or 0) or None,
                "source": "self",
            }
        )

    out.sort(
        key=lambda m: (
            0 if m.get("is_leader") else 1,
            0 if m.get("is_self") else 1,
            str(m.get("name") or ""),
        )
    )
    log(
        "team: list_party count="
        + str(len(out))
        + " names="
        + "、".join(str(x.get("name") or "") for x in out)
    )
    return out


def invalidate_team_states(session_or_pid) -> None:
    """Drop IN_TEAM / PARTY_MEMBERS caches after leave/invite/follow writes. @author by ak"""
    try:
        from app.core.state_dispatch import StateKind, invalidate_states

        invalidate_states(session_or_pid, StateKind.IN_TEAM, StateKind.PARTY_MEMBERS)
    except Exception:
        pass


def list_party_members(
    session,
    *,
    store=None,
    exclude_pid: int | None = None,
    fresh: bool = False,
    max_age: float | None = None,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Current party members via StateDispatch (StateKind.PARTY_MEMBERS).

    - UI refresh / post-write: pass fresh=True
    - invite skip-already-in-party: default soft cache (ttl ~1.5s)
    store/exclude_pid kept for call-site compat; producer ignores CRT multi-scan.
    @author by ak
    """
    log = log or (lambda _m: None)
    _ = exclude_pid
    blocked, brsn = _pid_blocked(session)
    if blocked:
        log(f"team: list_party blocked: {brsn}")
        return []
    try:
        from app.core.state_dispatch import StateKind, get_state

        party = get_state(
            session,
            StateKind.PARTY_MEMBERS,
            fresh=bool(fresh),
            max_age=max_age,
            log=log,
        )
        if party is None:
            return []
        out = list(party or [])
        # Optional: attach store only for self pid already filled by producer.
        _ = store
        return out
    except Exception as e:
        # Fallback pure read if dispatch unavailable (tests / early boot).
        log(f"team: list_party state err: {e}; fallback direct read")
        return read_cecteam_members(session, store=store, log=log)


def format_party_members_label(members: list[dict] | None) -> str:
    """Human label for current party. @author by ak"""
    if not members:
        return "当前队伍：未组队"
    parts: list[str] = []
    for m in members:
        name = str(m.get("name") or "").strip() or "?"
        # 不显示「自己」；队长仅作标记
        if m.get("is_leader"):
            parts.append(f"{name}(队长)")
        else:
            parts.append(name)
    return "当前队伍：" + "、".join(parts)


def target_already_in_party(
    target: TeamMemberTarget | dict,
    party: list[dict] | None,
) -> bool:
    """True if invite target is already in the party snapshot. @author by ak"""
    if not party:
        return False
    if isinstance(target, TeamMemberTarget):
        t_name = str(target.name or "").strip()
        try:
            t_id = int(target.obj_id or 0)
        except Exception:
            t_id = 0
    else:
        t_name = str(target.get("name") or target.get("token") or "").strip()
        try:
            t_id = int(target.get("obj_id") or 0)
        except Exception:
            t_id = 0
    for m in party:
        try:
            mid = int(m.get("obj_id") or 0)
        except Exception:
            mid = 0
        if t_id and mid and t_id == mid:
            return True
        if t_name and names_match(t_name, str(m.get("name") or "")):
            return True
        # token may be pure id matching member id
        lit = parse_literal_member_id(t_name)
        if lit is not None and mid and int(lit) == mid:
            return True
    return False


def filter_targets_not_in_party(
    targets: list[TeamMemberTarget] | list[dict],
    party: list[dict] | None,
) -> tuple[list[TeamMemberTarget], list[str]]:
    """
    Split targets into (need_invite, already_in_labels).

    @author by ak
    """
    need: list[TeamMemberTarget] = []
    skipped: list[str] = []
    for t in targets or []:
        if isinstance(t, TeamMemberTarget):
            tt = t
            label = f"{t.name}({t.source or '?'})"
        elif isinstance(t, dict):
            if t.get("is_self"):
                continue
            try:
                oid = int(t.get("obj_id") or 0)
            except Exception:
                oid = 0
            if oid <= 0 and t.get("obj_id") is None:
                continue
            tt = TeamMemberTarget(
                name=str(t.get("name") or t.get("token") or oid),
                obj_id=oid,
                source=str(t.get("source") or "cache"),
                pid=t.get("pid"),
            )
            label = f"{tt.name}({tt.source})"
        else:
            continue
        if target_already_in_party(tt, party):
            skipped.append(label)
        else:
            need.append(tt)
    return need, skipped


def _read_team_send_gate(session, host: int, *, log: LogFn | None = None) -> dict:
    """
    CD04D0 send gate on side-host:
      +0x268 u8 must be non-zero
      +0x14c conn* must be non-null
      +0x150 session != -1
    Also global 0x152BE58==2 is a fake-success early path (no wire send).
    @author by ak
    """
    log = log or (lambda _m: None)
    out = {
        "gate_ok": False,
        "flag268": 0,
        "conn": 0,
        "sess": -1,
        "global_mode": None,
        "fake_success_mode": False,
    }
    try:
        from app.core.remote_runtime import remote_read_bytes
        import struct

        host = int(host or 0) & 0xFFFFFFFF
        if not host:
            return out
        raw = remote_read_bytes(int(session.pid), host, 0x270)
        if len(raw) < 0x269:
            return out
        flag268 = int(raw[0x268]) & 0xFF
        conn = struct.unpack_from("<I", raw, 0x14C)[0]
        sess = struct.unpack_from("<i", raw, 0x150)[0]
        gmode = None
        try:
            graw = remote_read_bytes(
                int(session.pid),
                _note_va(session, 0x0152BE58),
                4,
            )
            if len(graw) == 4:
                gmode = struct.unpack("<I", graw)[0]
        except Exception:
            gmode = None
        fake = gmode == 2
        gate_ok = (not fake) and bool(flag268) and bool(conn) and (sess != -1)
        out.update(
            {
                "gate_ok": gate_ok,
                "flag268": flag268,
                "conn": int(conn) & 0xFFFFFFFF,
                "sess": int(sess),
                "global_mode": gmode,
                "fake_success_mode": fake,
            }
        )
    except Exception as e:
        log(f"team: send_gate err: {e}")
    return out


def _patch_rel8(buf: bytearray, pos: int, target: int) -> None:
    rel = int(target) - (int(pos) + 2)
    if rel < -128 or rel > 127:
        raise ValueError(f"rel8 out of range {rel}")
    buf[pos + 1] = rel & 0xFF


def _invite_zeroed_on_ui_thread(
    session,
    *,
    host: int,
    self_lo: int,
    self_hi: int,
    tgt_lo: int,
    tgt_hi: int,
    log: LogFn | None = None,
) -> tuple[bool, int, dict]:
    """
    UI 线程发送「全零 +0x20」的 0x1261 邀请包（CD04D0）。

    RE：CEC4E0 建包时未写 pkt+0x20，但 serialize@CF56B0 会 bswap 发出该字段；
    栈垃圾非 0 时服务端直接拒邀（ret 仍可能为 1）。必须显式清零。
    @author by ak
    """
    import struct
    from ctypes import wintypes

    from app.core.inject_gate import find_main_hwnd_for_pid
    from app.core.remote_runtime import (
        open_process,
        remote_alloc,
        remote_free,
        write_process,
        read_process,
        remote_call_cdecl_x86,
        remote_export_va,
        pid_call_mutex,
        kernel32,
    )

    log = log or (lambda _m: None)
    pid = int(session.pid)
    hwnd, title, cls = find_main_hwnd_for_pid(pid)
    if not hwnd:
        return False, 0, {"error": "no_hwnd"}

    send_fn = _note_va(session, NOTE_VA_PKT_SEND)
    set_long = remote_export_va(pid, "user32.dll", "SetWindowLongW")
    send_msg = remote_export_va(pid, "user32.dll", "SendMessageW")
    call_wp = remote_export_va(pid, "user32.dll", "CallWindowProcW")
    if not (send_fn and set_long and send_msg and call_wp):
        return False, 0, {"error": "export_missing"}

    WM_INVITE = 0x8000 + 0x53  # WM_APP+0x53
    GWL_WNDPROC = 0xFFFFFFFC  # -4

    # Build zeroed invite packet in remote memory
    pkt = bytearray(TEAM_INVITE_PKT_SIZE)
    struct.pack_into("<I", pkt, 0x00, TEAM_INVITE_VT)
    struct.pack_into("<I", pkt, 0x04, TEAM_INVITE_TYPE)
    struct.pack_into("<I", pkt, 0x08, 0)
    struct.pack_into("<I", pkt, 0x0C, 0)
    struct.pack_into("<I", pkt, 0x10, int(self_lo) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x14, int(self_hi) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x18, int(tgt_lo) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x1C, int(tgt_hi) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x20, 0)  # CRITICAL: must be 0
    struct.pack_into("<I", pkt, 0x24, 0)

    handle = 0
    mem = 0
    pkt_va = 0
    try:
        handle = open_process(pid)
        pkt_va = remote_alloc(handle, TEAM_INVITE_PKT_SIZE + 16)
        write_process(handle, pkt_va, bytes(pkt))
        mem = remote_alloc(handle, 0x200)
        ctx = int(mem)
        hook = ctx + 0x20
        driver = ctx + 0xA0

        # ctx: +0 old_proc; +4 host; +8 send_fn; +C ret; +10 call_wp; +14 pkt_va
        write_process(handle, ctx + 0x00, struct.pack("<I", 0))
        write_process(handle, ctx + 0x04, struct.pack("<I", int(host) & 0xFFFFFFFF))
        write_process(handle, ctx + 0x08, struct.pack("<I", send_fn & 0xFFFFFFFF))
        write_process(handle, ctx + 0x0C, struct.pack("<I", 0))
        write_process(handle, ctx + 0x10, struct.pack("<I", call_wp & 0xFFFFFFFF))
        write_process(handle, ctx + 0x14, struct.pack("<I", int(pkt_va) & 0xFFFFFFFF))

        hook_code = bytearray()
        hook_code += b"\x55\x8B\xEC"  # push ebp; mov ebp,esp
        hook_code += b"\x81\x7D\x0C" + struct.pack("<I", WM_INVITE)
        jne_pos = len(hook_code)
        hook_code += b"\x75\x00"
        # mov ecx, [ctx+4]  ; host
        hook_code += b"\x8B\x0D" + struct.pack("<I", (ctx + 0x04) & 0xFFFFFFFF)
        hook_code += b"\x85\xC9"
        jz_fail_pos = len(hook_code)
        hook_code += b"\x74\x00"
        # push 0 ; push pkt ; call send_fn (thiscall)
        hook_code += b"\x6A\x00"
        hook_code += b"\xFF\x35" + struct.pack("<I", (ctx + 0x14) & 0xFFFFFFFF)
        hook_code += b"\xA1" + struct.pack("<I", (ctx + 0x08) & 0xFFFFFFFF)
        hook_code += b"\xFF\xD0"
        hook_code += b"\xA3" + struct.pack("<I", (ctx + 0x0C) & 0xFFFFFFFF)
        hook_code += b"\xB8\x01\x00\x00\x00"
        jmp_done_pos = len(hook_code)
        hook_code += b"\xEB\x00"
        fail_off = len(hook_code)
        hook_code += b"\x33\xC0"
        jmp_done2_pos = len(hook_code)
        hook_code += b"\xEB\x00"
        forward_off = len(hook_code)
        hook_code += b"\xFF\x75\x14\xFF\x75\x10\xFF\x75\x0C\xFF\x75\x08"
        hook_code += b"\xFF\x35" + struct.pack("<I", ctx & 0xFFFFFFFF)
        hook_code += b"\xB8" + struct.pack("<I", call_wp & 0xFFFFFFFF)
        hook_code += b"\xFF\xD0"
        done_off = len(hook_code)
        hook_code += b"\x5D\xC2\x10\x00"
        _patch_rel8(hook_code, jne_pos, forward_off)
        _patch_rel8(hook_code, jz_fail_pos, fail_off)
        _patch_rel8(hook_code, jmp_done_pos, done_off)
        _patch_rel8(hook_code, jmp_done2_pos, done_off)
        write_process(handle, hook, bytes(hook_code))

        drv = bytearray()
        drv += b"\x55\x8B\xEC\x53"
        # SetWindowLongW(hwnd, -4, hook)
        drv += b"\x68" + struct.pack("<I", hook & 0xFFFFFFFF)
        drv += b"\x68" + struct.pack("<I", GWL_WNDPROC & 0xFFFFFFFF)
        drv += b"\xFF\x75\x08"
        drv += b"\xB8" + struct.pack("<I", set_long & 0xFFFFFFFF)
        drv += b"\xFF\xD0"
        drv += b"\xA3" + struct.pack("<I", ctx & 0xFFFFFFFF)
        drv += b"\x33\xC0\xA3" + struct.pack("<I", (ctx + 0x0C) & 0xFFFFFFFF)
        # SendMessageW(hwnd, WM_INVITE, 0, 0)  — tgt already in pkt
        drv += b"\x6A\x00\x6A\x00"
        drv += b"\x68" + struct.pack("<I", WM_INVITE)
        drv += b"\xFF\x75\x08"
        drv += b"\xB8" + struct.pack("<I", send_msg & 0xFFFFFFFF)
        drv += b"\xFF\xD0"
        # restore
        drv += b"\xFF\x35" + struct.pack("<I", ctx & 0xFFFFFFFF)
        drv += b"\x68" + struct.pack("<I", GWL_WNDPROC & 0xFFFFFFFF)
        drv += b"\xFF\x75\x08"
        drv += b"\xB8" + struct.pack("<I", set_long & 0xFFFFFFFF)
        drv += b"\xFF\xD0"
        drv += b"\xA1" + struct.pack("<I", (ctx + 0x0C) & 0xFFFFFFFF)
        drv += b"\x5B\x5D\xC3"
        write_process(handle, driver, bytes(drv))

        # remote_call_* already holds pid_call_mutex; do not nest outer hold.
        ret = remote_call_cdecl_x86(
            pid,
            driver,
            [int(hwnd) & 0xFFFFFFFF],
            timeout_ms=5000,
        )
        ret_i = int(ret or 0)
        old_proc = struct.unpack("<I", read_process(handle, ctx + 0x00, 4))[0]
        detail = {
            "path": "ui_thread_cd04d0_zeroed",
            "hwnd": int(hwnd),
            "title": title,
            "class": cls,
            "ret": ret_i,
            "old_proc": int(old_proc) & 0xFFFFFFFF,
            "send_fn": send_fn,
            "pkt": int(pkt_va) & 0xFFFFFFFF,
            "host": int(host) & 0xFFFFFFFF,
            "field20": 0,
        }
        ok = bool(ret_i)
        return ok, ret_i, detail
    finally:
        try:
            if handle and mem:
                time.sleep(0.05)
                remote_free(handle, mem)
        except Exception:
            pass
        try:
            if handle and pkt_va:
                remote_free(handle, pkt_va)
        except Exception:
            pass
        try:
            if handle:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass


def _build_invite_pkt(self_lo: int, self_hi: int, tgt_lo: int, tgt_hi: int) -> bytes:
    """Fully zeroed TeamInvite c2s 0x1261 object (size 0x28). @author by ak"""
    import struct

    pkt = bytearray(TEAM_INVITE_PKT_SIZE)
    struct.pack_into("<I", pkt, 0x00, TEAM_INVITE_VT)
    struct.pack_into("<I", pkt, 0x04, TEAM_INVITE_TYPE)
    struct.pack_into("<I", pkt, 0x08, 0)
    struct.pack_into("<I", pkt, 0x0C, 0)
    struct.pack_into("<I", pkt, 0x10, int(self_lo) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x14, int(self_hi) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x18, int(tgt_lo) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x1C, int(tgt_hi) & 0xFFFFFFFF)
    struct.pack_into("<I", pkt, 0x20, 0)
    struct.pack_into("<I", pkt, 0x24, 0)
    return bytes(pkt)


def invite_player_by_id(
    session,
    obj_id: int,
    *,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Send team invite (c2s 0x1261) to target player id.

    关键因修复：CEC4E0 建包不写 +0x20，serialize 仍会发出 → 服务端拒邀但 ret 常为 1。
    优先路径一律用「全零包 + CD04D0」：
      1) bridge 主线程 zeroed CD04D0（若 bridge 可用）
      2) UI 线程 zeroed CD04D0（不依赖 bridge 热更）
      3) CRT zeroed CD04D0
    ret/ok 只表示客户端发送路径成功，不保证对方弹窗（需同图同线且可收邀请）。
    @author by ak
    """
    import struct
    from ctypes import wintypes

    from app.core.remote_runtime import (
        open_process,
        remote_alloc,
        remote_free,
        write_process,
        pid_call_mutex,
        kernel32,
    )

    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False,
            action="invite",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    oid = int(obj_id)
    if oid <= 0:
        return TeamOpResult(ok=False, action="invite", message="invalid id", error="bad_id")
    if oid < 100000:
        log(f"team: invite warn suspicious low id={oid} (可能不是角色 id)")
    tgt_lo = oid & 0xFFFFFFFF
    tgt_hi = (oid >> 32) & 0xFFFFFFFF
    host = get_host_player_for_team(session, log=log)
    if not host:
        return TeamOpResult(ok=False, action="invite", message="host null", error="no_host")
    self_lo, self_hi = read_host_side_id_pair(session, host, log=log)
    if not self_lo and not self_hi:
        return TeamOpResult(
            ok=False,
            action="invite",
            message="host id@+0x240 null",
            error="no_host_id",
        )

    pid = int(session.pid)
    gate = _read_team_send_gate(session, host, log=log)
    if gate.get("fake_success_mode"):
        log(
            f"team: invite gate FAKE_SUCCESS mode global={gate.get('global_mode')} "
            f"(CD04D0 会 ret=1 但不发包)"
        )
    elif not gate.get("gate_ok"):
        log(
            f"team: invite gate FAIL flag268={gate.get('flag268')} "
            f"conn=0x{int(gate.get('conn') or 0):X} sess={gate.get('sess')}"
        )

    def _ok_msg(path: str, ret_i: int, extra: str = "") -> str:
        return (
            f"invite send id={oid} tgt={tgt_lo:X}:{tgt_hi:X} "
            f"self={self_lo:X}:{self_hi:X} host=0x{host:X} "
            f"ret={ret_i} path={path} field20=0 "
            f"gate={'ok' if gate.get('gate_ok') else 'fail'}"
            f"{extra}"
        )

    # 1) bridge main-thread zeroed invite (timer UI thread inside game)
    try:
        from app.core.xajh_bridge import ensure_bridge

        br = ensure_bridge(pid, log=log, inject_if_needed=True)
        if br is not None and hasattr(br, "team_invite"):
            br_res = br.team_invite(tgt_lo, tgt_hi, timeout_ms=4000)
            ok_b = bool(getattr(br_res, "ok", False))
            ret_b = int(getattr(br_res, "ret", 0) or 0)
            err_b = str(getattr(br_res, "error", "") or getattr(br_res, "note", "") or "")
            msg = _ok_msg("bridge_cd04d0_zeroed", ret_b, f" note={err_b[:80]}" if err_b else "")
            log(f"team: {msg}")
            if ok_b and ret_b and gate.get("gate_ok") and not gate.get("fake_success_mode"):
                return TeamOpResult(
                    ok=True,
                    action="invite",
                    message=msg,
                    detail={
                        "id": oid,
                        "tgt_lo": tgt_lo,
                        "tgt_hi": tgt_hi,
                        "self_lo": self_lo,
                        "self_hi": self_hi,
                        "host": host,
                        "ret": ret_b,
                        "path": "bridge_cd04d0_zeroed",
                        "field20": 0,
                        "gate": gate,
                        "bridge_note": err_b,
                    },
                )
            log(f"team: bridge invite weak/fail, fallback UI/CRT: ok={ok_b} ret={ret_b} {err_b}")
    except TypeError as e:
        # old signature timeout_s=... would raise here if not fixed
        log(f"team: bridge invite TypeError (param?), fallback: {e}")
    except Exception as e:
        log(f"team: bridge invite err, fallback UI/CRT: {e}")

    # 2) UI-thread zeroed CD04D0
    try:
        ok_ui, ret_ui, det_ui = _invite_zeroed_on_ui_thread(
            session,
            host=host,
            self_lo=self_lo,
            self_hi=self_hi,
            tgt_lo=tgt_lo,
            tgt_hi=tgt_hi,
            log=log,
        )
        msg_ui = _ok_msg(
            "ui_thread_cd04d0_zeroed",
            ret_ui,
            f" hwnd=0x{int(det_ui.get('hwnd') or 0):X}",
        )
        log(f"team: {msg_ui}")
        if ok_ui and gate.get("gate_ok") and not gate.get("fake_success_mode"):
            det_ui.update(
                {
                    "id": oid,
                    "tgt_lo": tgt_lo,
                    "tgt_hi": tgt_hi,
                    "self_lo": self_lo,
                    "self_hi": self_hi,
                    "host": host,
                    "gate": gate,
                    "field20": 0,
                }
            )
            return TeamOpResult(
                ok=True,
                action="invite",
                message=msg_ui,
                detail=det_ui,
            )
        log(f"team: ui_thread zeroed invite weak/fail, fallback CRT: ret={ret_ui} det={det_ui}")
    except Exception as e:
        log(f"team: ui_thread zeroed invite err, fallback CRT: {e}")

    # 3) CRT zeroed CD04D0
    handle = 0
    pkt_va = 0
    try:
        pkt = _build_invite_pkt(self_lo, self_hi, tgt_lo, tgt_hi)
        with pid_call_mutex(pid, timeout_ms=8000):
            handle = open_process(pid)
            pkt_va = remote_alloc(handle, TEAM_INVITE_PKT_SIZE + 16)
            write_process(handle, pkt_va, pkt)
            send_va = _note_va(session, NOTE_VA_PKT_SEND)
            ret = remote_call_thiscall_x86(
                pid,
                send_va,
                host,
                [int(pkt_va) & 0xFFFFFFFF, 0],
                timeout_ms=4000,
            )
        ret_i = int(ret or 0)
        wire_ok = bool(ret_i) and bool(gate.get("gate_ok")) and not gate.get(
            "fake_success_mode"
        )
        msg = _ok_msg("cd04d0_zeroed", ret_i, f" pkt=0x{int(pkt_va) & 0xFFFFFFFF:X}")
        log(f"team: {msg}")
        if wire_ok:
            return TeamOpResult(
                ok=True,
                action="invite",
                message=msg,
                detail={
                    "id": oid,
                    "tgt_lo": tgt_lo,
                    "tgt_hi": tgt_hi,
                    "self_lo": self_lo,
                    "self_hi": self_hi,
                    "host": host,
                    "pkt": int(pkt_va) & 0xFFFFFFFF,
                    "ret": ret_i,
                    "path": "cd04d0_zeroed",
                    "field20": 0,
                    "gate": gate,
                },
            )
        return TeamOpResult(
            ok=False,
            action="invite",
            message=f"{msg} (未入网/gate)",
            error="send_gate_or_ret0",
            detail={
                "id": oid,
                "ret": ret_i,
                "path": "cd04d0_zeroed",
                "field20": 0,
                "gate": gate,
            },
        )
    except Exception as e:
        log(f"team: zeroed invite failed: {e}")
        return TeamOpResult(ok=False, action="invite", message=str(e), error=str(e))
    finally:
        try:
            if handle and pkt_va:
                remote_free(handle, pkt_va)
        except Exception:
            pass
        try:
            if handle:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass



def invite_player_by_name(
    session,
    name: str,
    *,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    按角色名邀请 — 与游戏「组队添加成员」输入框完全同一条链：

      CF0620(GetPlayerIDByName, reason=1)
        → 服务器 0x77 回包
        → 客户端跳表 reason=1 调 CEC240 发 c2s 0x125c

    不要用 reason=8（那是聊天邀请列表）。
    相对 0x1261 按 id 邀请，此路径不依赖对方是否在 AOI/附近。
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False,
            action="invite_name",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    name = str(name or "").strip()
    if not name:
        return TeamOpResult(ok=False, action="invite_name", message="empty name", error="bad_name")
    if _ID_RE.match(name):
        # 纯数字当作 id
        return invite_player_by_id(session, int(name), log=log)

    ok = send_get_player_id_by_name(
        session,
        name,
        reason=NAME_QUERY_REASON_TEAM_INVITE,
        log=log,
    )
    msg = (
        f"invite_by_name name={name!r} reason=1 "
        f"path=ui_name_0x76_r1→0x125c ok={ok}"
    )
    log(f"team: {msg}")
    return TeamOpResult(
        ok=bool(ok),
        action="invite_name",
        message=msg if ok else f"{msg} (发送名查询失败)",
        error=None if ok else "name_query_send_fail",
        detail={
            "name": name,
            "reason": NAME_QUERY_REASON_TEAM_INVITE,
            "path": "ui_name_reason1",
        },
    )



# Game_TeamFollow confirm-panel button geometry (live-verified 2026-07-31 on
# 290x113 dialog): bottom-row 确定/开启 button center ≈ (0.30w, 0.72h).
# GetDlgItem returns null for this dialog
# (controls live in the XML page, not the child map), so precise click points
# are derived from the verified relative layout.
FOLLOW_OK_FRAC = (0.30, 0.72)
FOLLOW_DLG_NAMES = ("Game_TeamFollow", "Win_TeamFollow")
FOLLOW_MAIN_NAMES = ("Win_TeamMain", "Win_TeamFrame")
# Btn_TeamFollow / Btn_Follow callback: opens the Game_TeamFollow confirm panel.
NOTE_VA_TEAM_FOLLOW_OPEN = 0x00997160

# Active bindings are rendered in the bottom-right Win_BindStatus panel. Each
# visible row has Txt_TipN and Btn_QuitN; team follow is cancelled by clicking
# that row's Btn_Quit, not by opening the confirmation panel and clicking No.
FOLLOW_STATUS_DLG_NAME = "Win_BindStatus"
FOLLOW_STATUS_ROWS = 3
FOLLOW_STATUS_TIP_OFF = 0x2AC
FOLLOW_STATUS_QUIT_OFF = 0x2B8
# AUIImageButton stores a parent pointer and live local geometry. Unlike
# AUIDialog, its geometry is float-based and starts at +0x94.
AUI_IMAGE_BUTTON_PARENT_OFF = 0x10
AUI_IMAGE_BUTTON_X_OFF = 0x94
AUI_IMAGE_BUTTON_Y_OFF = 0x98
AUI_IMAGE_BUTTON_W_OFF = 0x9C
AUI_IMAGE_BUTTON_H_OFF = 0xA0
AUI_IMAGE_BUTTON_GEOMETRY_SIZE = 0xA4


def _resolve_main_hwnd(session) -> int:
    """Tuple-safe main-window hwnd for the session pid. @author by ak"""
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        return 0
    try:
        from app.core.inject_gate import find_main_hwnd_for_pid

        found = find_main_hwnd_for_pid(pid)
        if isinstance(found, tuple):
            return int(found[0] or 0)
        return int(found or 0)
    except Exception:
        return 0


def _find_shown_dialog(session, names, *, log: LogFn | None = None) -> int:
    """Return ptr of the first shown dialog in names (0 if none). @author by ak"""
    from app.core.plg_ui import get_game_ui_dlg, is_dlg_show

    for name in names:
        try:
            dlg = get_game_ui_dlg(session, name, log=log)
            if dlg and is_dlg_show(session, dlg, log=log):
                return int(dlg) & 0xFFFFFFFF
        except Exception:
            continue
    return 0


def _read_team_ui_bytes(session, addr: int, size: int) -> bytes:
    """Read a small attached-process UI block without opening another handle."""
    pm = getattr(session, "pm", None)
    if pm is None or not addr or size <= 0:
        return b""
    try:
        import pymem.memory

        return bytes(pymem.memory.read_bytes(pm.process_handle, int(addr), int(size)))
    except Exception:
        return b""


def _read_team_ui_u32(session, addr: int) -> int:
    raw = _read_team_ui_bytes(session, addr, 4)
    return struct.unpack_from("<I", raw, 0)[0] if len(raw) >= 4 else 0


def _team_ui_ctrl_shown(session, ctrl: int) -> bool:
    """Read AUI control visibility through the game export."""
    try:
        from app.core.activity_auto import _aui_obj_is_show

        shown = _aui_obj_is_show(session, int(ctrl), log=lambda _m: None)
        return bool(shown) if shown is not None else False
    except Exception:
        return False


def _read_follow_quit_rect(
    session,
    dlg: int,
    button: int,
    *,
    name: str,
    log: LogFn | None = None,
):
    """Resolve one Btn_QuitN client rect from its live parent-local geometry."""
    from app.core.aui_click import AuiCtrlRect, read_aui_ctrl_rect

    log = log or (lambda _m: None)
    raw = _read_team_ui_bytes(session, int(button), AUI_IMAGE_BUTTON_GEOMETRY_SIZE)
    if len(raw) < AUI_IMAGE_BUTTON_GEOMETRY_SIZE:
        return AuiCtrlRect(ok=False, name=name, ctrl_ptr=int(button), error="short button geometry")
    try:
        parent = struct.unpack_from("<I", raw, AUI_IMAGE_BUTTON_PARENT_OFF)[0]
        local_x = struct.unpack_from("<f", raw, AUI_IMAGE_BUTTON_X_OFF)[0]
        local_y = struct.unpack_from("<f", raw, AUI_IMAGE_BUTTON_Y_OFF)[0]
        width = struct.unpack_from("<f", raw, AUI_IMAGE_BUTTON_W_OFF)[0]
        height = struct.unpack_from("<f", raw, AUI_IMAGE_BUTTON_H_OFF)[0]
    except struct.error as e:
        return AuiCtrlRect(ok=False, name=name, ctrl_ptr=int(button), error=str(e))
    values = (local_x, local_y, width, height)
    if int(parent) != (int(dlg) & 0xFFFFFFFF):
        return AuiCtrlRect(
            ok=False,
            name=name,
            ctrl_ptr=int(button),
            error=f"unexpected parent 0x{int(parent):X}",
        )
    if not all(math.isfinite(v) for v in values) or not (
        -4096.0 <= local_x <= 4096.0
        and -4096.0 <= local_y <= 4096.0
        and 4.0 <= width <= 512.0
        and 4.0 <= height <= 512.0
    ):
        return AuiCtrlRect(
            ok=False,
            name=name,
            ctrl_ptr=int(button),
            error=f"bad local geometry {values!r}",
        )
    dlg_rect = read_aui_ctrl_rect(session, int(dlg), name=FOLLOW_STATUS_DLG_NAME, log=log)
    if not dlg_rect.ok:
        return AuiCtrlRect(
            ok=False,
            name=name,
            ctrl_ptr=int(button),
            error=f"bad parent rect: {dlg_rect.error}",
        )
    return AuiCtrlRect(
        ok=True,
        name=name,
        ctrl_ptr=int(button),
        x=int(round(float(dlg_rect.x) + local_x)),
        y=int(round(float(dlg_rect.y) + local_y)),
        w=max(1, int(round(width))),
        h=max(1, int(round(height))),
    )


def _collect_follow_status_rows(
    session,
    dlg: int,
    *,
    log: LogFn | None = None,
) -> list[dict]:
    """Resolve the three Win_BindStatus rows and their quit controls."""
    from app.core.activity_auto import aui_get_dlg_item
    from app.core.map_fly import _aui_get_text

    quiet = lambda _m: None
    rows: list[dict] = []
    for row in range(1, FOLLOW_STATUS_ROWS + 1):
        tip = int(
            aui_get_dlg_item(session, dlg, f"Txt_Tip{row}", log=quiet)
            or _read_team_ui_u32(
                session, int(dlg) + FOLLOW_STATUS_TIP_OFF + (row - 1) * 4
            )
            or 0
        )
        quit_btn = int(
            aui_get_dlg_item(session, dlg, f"Btn_Quit{row}", log=quiet)
            or _read_team_ui_u32(
                session, int(dlg) + FOLLOW_STATUS_QUIT_OFF + (row - 1) * 4
            )
            or 0
        )
        text = _aui_get_text(session, tip, log=quiet) if tip else ""
        rect = (
            _read_follow_quit_rect(
                session,
                dlg,
                quit_btn,
                name=f"Btn_Quit{row}",
                log=quiet,
            )
            if quit_btn
            else None
        )
        rows.append(
            {
                "row": row,
                "tip": tip,
                "text": str(text or "").strip(),
                "button": quit_btn,
                "shown": bool(quit_btn and _team_ui_ctrl_shown(session, quit_btn)),
                "rect": rect,
            }
        )
    if log:
        summary = [
            f"{r['row']}:{r['text'] or '-'}:{'show' if r['shown'] else 'hide'}"
            for r in rows
        ]
        log(f"follow: bind-status rows {summary}")
    return rows


def click_team_follow_cancel_status(session, log: LogFn | None = None) -> bool:
    """Click the active 组队跟随 row's Btn_Quit in Win_BindStatus."""
    from app.core.aui_click import click_client_bg
    from app.core.plg_ui import get_game_ui_dlg, is_dlg_show

    log = log or (lambda _m: None)
    try:
        hwnd = _resolve_main_hwnd(session)
        if not hwnd:
            log("follow: cancel status has no main hwnd")
            return False
        dlg = int(get_game_ui_dlg(session, FOLLOW_STATUS_DLG_NAME, log=log) or 0)
        if not dlg or not is_dlg_show(session, dlg, log=log):
            log("follow: Win_BindStatus is not shown")
            return False

        rows = _collect_follow_status_rows(session, dlg, log=log)
        visible = [r for r in rows if r["button"] and r["shown"]]
        matching = [r for r in visible if "组队跟随" in r["text"]]
        if len(matching) == 1:
            target = matching[0]
        else:
            log(
                "follow: cannot uniquely identify 组队跟随 in Win_BindStatus "
                f"(visible={len(visible)} matching={len(matching)})"
            )
            return False

        rect = target.get("rect")
        if rect is None or not getattr(rect, "ok", False):
            log(
                f"follow: cannot resolve Btn_Quit{target['row']} live rect: "
                f"{getattr(rect, 'error', 'missing rect')}"
            )
            return False
        cx, cy = rect.center

        log(
            f"follow: click Win_BindStatus Btn_Quit{target['row']} at {cx},{cy} "
            f"text={target['text']!r}"
        )
        clicked = click_client_bg(
            session,
            int(hwnd),
            int(cx),
            int(cy),
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=False,
            humanize=False,
            log=log,
        )
        if not clicked:
            return False

        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            if not is_dlg_show(session, dlg, log=lambda _m: None):
                return True
            if not _team_ui_ctrl_shown(session, int(target["button"])):
                return True
            time.sleep(0.05)
        log("follow: Btn_Quit click posted but 组队跟随 status is still shown")
        return False
    except Exception as e:
        log(f"follow: cancel status click err: {e}")
        return False


def _click_follow_button_point(
    session,
    hwnd: int,
    dlg: int,
    fx: float,
    fy: float,
    *,
    label: str,
    log: LogFn | None = None,
) -> bool:
    """Click a follow-dialog button in the background at its relative center."""
    from app.core.aui_click import click_client_bg, read_aui_ctrl_rect

    log = log or (lambda _m: None)
    rect = read_aui_ctrl_rect(session, dlg, name="follow_dlg", log=log)
    if not rect.ok or rect.w < 100 or rect.h < 60:
        log(f"follow: bad dialog rect: {rect.error}")
        return False
    cx = int(rect.x + float(fx) * rect.w)
    cy = int(rect.y + float(fy) * rect.h)
    log(f"follow: precise click {label} at {cx},{cy} "
        f"(dlg={rect.x},{rect.y} {rect.w}x{rect.h})")
    return bool(
        click_client_bg(
            session,
            int(hwnd),
            cx,
            cy,
            prefer_bridge=True,
            prefer_post=True,
            allow_cursor=False,
            humanize=False,
            log=log,
        )
    )


def click_team_follow_button(session, enabled: bool, log: LogFn | None = None) -> bool:
    """Run the real UI path for enabling or cancelling team follow.

    Enable opens Game_TeamFollow and clicks 确定. Cancel clicks Btn_QuitN on the
    active 组队跟随 row in the bottom-right Win_BindStatus panel.
    @author by ak
    """
    log = log or (lambda m: None)
    if not enabled:
        return click_team_follow_cancel_status(session, log=log)
    try:
        pid = int(session.pid)
        hwnd = _resolve_main_hwnd(session)
        if not hwnd:
            log("follow: no main hwnd")
            return False

        dlg = _find_shown_dialog(session, FOLLOW_DLG_NAMES, log=log)
        if not dlg:
            # Open the confirm panel via the Btn_TeamFollow callback on team main.
            main_dlg = _find_shown_dialog(session, FOLLOW_MAIN_NAMES, log=log)
            if not main_dlg:
                log("follow: team main not shown")
                return False
            try:
                ret = remote_call_thiscall_x86(
                    pid,
                    _note_va(session, NOTE_VA_TEAM_FOLLOW_OPEN),
                    int(main_dlg),
                    [1],
                    timeout_ms=4000,
                )
                if not ret:
                    log("follow: open confirm panel failed")
                    return False
            except Exception as e:
                log(f"follow: open confirm panel err: {e}")
                return False
            time.sleep(0.5)
            dlg = _find_shown_dialog(session, FOLLOW_DLG_NAMES, log=log)
        if not dlg:
            log("follow: confirm panel not shown")
            return False

        fx, fy = FOLLOW_OK_FRAC
        clicked = _click_follow_button_point(
            session,
            hwnd,
            dlg,
            fx,
            fy,
            label="Btn_OK(开启)",
            log=log,
        )
        if not clicked:
            return False
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            if not _find_shown_dialog(session, FOLLOW_DLG_NAMES, log=log):
                return True
            time.sleep(0.05)
        log("follow: click posted but confirm panel is still shown")
        return False
    except Exception as e:
        log(f"follow: precise click err: {e}")
        return False


def set_team_follow(
    session,
    *,
    enabled: bool = True,
    use_ui_click: bool = False,
    log: LogFn | None = None,
) -> TeamOpResult:
    """Start/stop team follow, defaulting to the raw c2s packet (no UI click).

    Primary path: raw packet b"\\x00\\x00" (open) / b"\\x1b\\x00" (close) sent
    through 0xCD1740 (fire-and-forget, no scene gate).  use_ui_click=True opts
    back into the old precise background UI click.  The function-callback paths
    (0x9A0680 / 0x9A06A0 -> CEC8A0) remain as a fallback.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not use_ui_click:
        # Raw packet primary path.
        r_pkt = send_team_follow_packet(session, bool(enabled), log=log)
        if r_pkt.ok:
            return TeamOpResult(
                ok=True,
                action="follow",
                message=(
                    "已开启组队跟随" if enabled else "已取消组队跟随"
                ),
                detail=dict(r_pkt.detail or {}),
            )
        log(f"team: follow packet primary failed, fallback: {r_pkt.message}")
    if use_ui_click:
        # Lab-only precise background UI click (bridge, never steals focus).
        if click_team_follow_button(session, enabled, log=log):
            return TeamOpResult(
                ok=True,
                action="follow",
                message="已开启组队跟随" if enabled else "已取消组队跟随",
                detail={"path": "precise_ui_click"},
            )
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False,
            action="follow",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    flag = 1 if enabled else 0
    if enabled:
        try:
            from app.core.plg_ui import is_host_in_team

            if not is_host_in_team(session, log=log):
                return TeamOpResult(
                    ok=False,
                    action="follow",
                    message="当前账号未组队，不能发起组队跟随",
                    error="not_in_team",
                )
        except Exception as e:
            log(f"team: follow team probe err: {e}")
    host = get_host_player_for_team(session, log=log)
    if not host:
        return TeamOpResult(ok=False, action="follow", message="host null", error="no_host")
    label = "开启" if enabled else "取消"
    # 1) Packet callback on the game UI thread: enable=0x9A0680 / disable=0x9A06A0.
    try:
        from app.core.xajh_bridge import ensure_bridge

        bridge = ensure_bridge(int(session.pid), log=log, inject_if_needed=True)
        if bridge is not None and hasattr(bridge, "team_follow"):
            bridge_result = bridge.team_follow(bool(enabled), timeout_ms=4000)
            ret = int(getattr(bridge_result, "ret", 0) or 0)
            ok = bool(getattr(bridge_result, "ok", False)) and bool(ret)
            note = str(
                getattr(bridge_result, "note", "")
                or getattr(bridge_result, "error", "")
                or ""
            )
            msg = (
                f"team_follow {label} ui_callback flag={flag} host=0x{host:X} "
                f"ret={ret} note={note[:120]}"
            )
            log(f"team: {msg}")
            if ok:
                return TeamOpResult(
                    ok=True,
                    action="follow",
                    message="已开启组队跟随" if enabled else "已取消组队跟随",
                    detail={
                        "enabled": bool(enabled),
                        "flag": flag,
                        "host": host,
                        "ret": ret,
                        "path": "bridge_ui_callback",
                        "bridge_note": note,
                    },
                )
            log("team: follow UI callback weak/fail, fallback remote call")
    except Exception as e:
        log(f"team: follow UI callback err, fallback remote call: {e}")
    # 2) Last-resort remote thiscall CEC8A0 (client-side builder; ret=1 = queued).
    try:
        va = _note_va(session, NOTE_VA_TEAM_FOLLOW)
        ret = remote_call_thiscall_x86(
            int(session.pid),
            va,
            host,
            [flag],
            timeout_ms=4000,
        )
        msg = f"team_follow {label} flag={flag} host=0x{host:X} ret={ret}"
        log(f"team: {msg}")
        ok = bool(ret)
        if ok:
            ui_msg = "已开启组队跟随" if enabled else "已取消组队跟随"
        else:
            ui_msg = (
                f"组队跟随未生效（ret={ret}）"
                if enabled
                else f"取消跟随未生效（ret={ret}）"
            )
        return TeamOpResult(
            ok=ok,
            action="follow",
            message=ui_msg,
            error=None if ok else "follow_ret0",
            detail={
                "enabled": bool(enabled),
                "flag": flag,
                "host": host,
                "ret": int(ret or 0),
                "path": "remote_fallback",
            },
        )
    except Exception as e:
        log(f"team: follow failed: {e}")
        return TeamOpResult(ok=False, action="follow", message=str(e), error=str(e))


# Raw team-follow c2s payloads (wire-verified 2026-08-09):
#   b"\\x00\\x00" = 开启组队跟随, b"\\x1b\\x00" = 关闭组队跟随.
TEAM_FOLLOW_PKT_OPEN = bytes.fromhex("0000")
TEAM_FOLLOW_PKT_CLOSE = bytes.fromhex("1B00")


def send_team_follow_packet(
    session,
    enabled: bool,
    *,
    log: LogFn | None = None,
) -> TeamOpResult:
    """Send the raw team-follow c2s packet through 0xCD1740 (no UI click).

    Payload is the wire-verified follow body: b"\\x00\\x00" opens follow,
    b"\\x1b\\x00" closes it.  ret 1 means the client accepted the packet;
    the live effect is confirmed by probe_team_follow_status().
    @author by ak
    """
    log = log or (lambda _m: None)
    payload = TEAM_FOLLOW_PKT_OPEN if enabled else TEAM_FOLLOW_PKT_CLOSE
    try:
        from app.core.raw_c2s import send_raw_c2s_packet

        ret = send_raw_c2s_packet(session, payload, log=log)
    except Exception as e:
        log(f"team: follow packet err: {e}")
        return TeamOpResult(
            ok=False,
            action="follow_packet",
            message=str(e),
            error=str(e),
        )
    ok = bool(ret)
    label = "开启" if enabled else "关闭"
    return TeamOpResult(
        ok=ok,
        action="follow_packet",
        message=(
            f"已{label}组队跟随(封包)" if ok else f"组队跟随封包未生效 ret={ret}"
        ),
        error=None if ok else "follow_ret0",
        detail={
            "enabled": bool(enabled),
            "payload": payload.hex().upper(),
            "ret": int(ret),
            "path": "raw_c2s_0xCD1740",
        },
    )


def probe_team_follow_status(
    session,
    *,
    log: LogFn | None = None,
) -> bool | None:
    """True if team follow is active, read directly from game memory.

    Wire-verified flag (2026-08-09): CECTeamMember+0x40 bit 0x100 is set on
    the followed member while 组队跟随 is on and cleared when off.  None means
    the host is not in a team or the flag cannot be read.
    @author by ak
    """
    try:
        from app.core.plg_ui import get_host_team_ptr

        team = int(get_host_team_ptr(session, log=log) or 0) & 0xFFFFFFFF
        if not team:
            return None
        pm = getattr(session, "pm", None)
        if pm is None:
            return None
        import pymem.memory

        head = pymem.memory.read_bytes(pm.process_handle, team, 0x40)
        arr = struct.unpack_from("<I", head, TEAM_OFF_MEMBER_PTRS)[0]
        count = struct.unpack_from("<I", head, TEAM_OFF_MEMBER_COUNT)[0]
        if not arr or count <= 0:
            return False
        for i in range(min(count, 12)):
            mp = pymem.memory.read_uint(pm.process_handle, arr + i * 4)
            if not mp:
                continue
            flags = pymem.memory.read_uint(pm.process_handle, mp + TEAM_MEMBER_OFF_FOLLOW_FLAG)
            if flags & TEAM_MEMBER_FOLLOW_BIT:
                return True
        return False
    except Exception:
        return None


def set_team_invite_flags(
    session,
    *,
    auto_admit: bool = True,
    member_invite: bool = True,
    mode_word: int | None = None,
    pinzhi: int | None = None,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Set 自动招人 / 允许队员邀请 / 队伍分配品质 (c2s 0x1263).

    RE (s2c@5F9600 + serialize@CF6620):
      wire 在 id 与 empty-octets 之后只发 **u32 @ pkt+0x20** (bswap)。
      LE 字节: [0]=pinzhi, [1]=mode_hi(2), [2]=auto_admit, [3]=member_invite
      pinzhi: 0=自由拾取, 1=精良, 4=优质, 7=完美
    CEC560 把字段写到 +0x1c 而 serialize 读 +0x20 → 自动招人常丢；
    这里直接组零包走 CD04D0，与邀请修复同思路。
    @author by ak
    """
    import struct as _st

    from app.core.remote_runtime import (
        open_process,
        remote_alloc,
        remote_free,
        write_process,
    )

    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False,
            action="invite_flags",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    if mode_word is None:
        pz = TEAM_LOOT_SUB_DEFAULT if pinzhi is None else int(pinzhi)
        mode_hi = TEAM_FLAGS_MODE_HI & 0xFF
        mode_word = (mode_hi << 8) | (int(pz) & 0xFF)
    else:
        mode_word = int(mode_word) & 0xFFFF
        if pinzhi is not None:
            mode_word = (mode_word & 0xFF00) | (int(pinzhi) & 0xFF)
    pinzhi_val = int(mode_word) & 0xFF
    mode_hi = (int(mode_word) >> 8) & 0xFF
    auto_b = 1 if auto_admit else 0
    invite_b = 1 if member_invite else 0
    # LE u32 @+0x20 (and mirror @+0x1c for local layout parity with s2c)
    flags_u32 = (
        (pinzhi_val & 0xFF)
        | ((mode_hi & 0xFF) << 8)
        | ((auto_b & 0xFF) << 16)
        | ((invite_b & 0xFF) << 24)
    )

    host = get_host_player_for_team(session, log=log)
    if not host:
        return TeamOpResult(
            ok=False, action="team_flags", message="host null", error="no_host"
        )
    self_lo, self_hi = read_host_side_id_pair(session, host, log=log)

    pid = int(session.pid)
    handle = 0
    pkt_va = 0
    try:
        send_va = _note_va(session, NOTE_VA_PKT_SEND)
        vt = _note_va(session, 0x012C631C)
        empty_oct = _note_va(session, 0x01275F74)
        pkt = bytearray(0x28)
        _st.pack_into("<I", pkt, 0x00, vt)
        _st.pack_into("<I", pkt, 0x04, 0x1263)
        _st.pack_into("<I", pkt, 0x08, 0)
        _st.pack_into("<I", pkt, 0x0C, 0)
        _st.pack_into("<I", pkt, 0x10, int(self_lo) & 0xFFFFFFFF)
        _st.pack_into("<I", pkt, 0x14, int(self_hi) & 0xFFFFFFFF)
        # empty Octets first dword (game uses static empty instance addr)
        _st.pack_into("<I", pkt, 0x18, empty_oct)
        _st.pack_into("<I", pkt, 0x1C, flags_u32)  # s2c reads +0x1c..+0x1f
        _st.pack_into("<I", pkt, 0x20, flags_u32)  # serialize sends +0x20
        _st.pack_into("<I", pkt, 0x24, 0)

        handle = open_process(pid)
        pkt_va = remote_alloc(handle, 0x30)
        write_process(handle, pkt_va, bytes(pkt))
        ret = remote_call_thiscall_x86(
            pid,
            send_va,
            host,
            [int(pkt_va) & 0xFFFFFFFF, 0],
            timeout_ms=4000,
        )
        qname = {
            0: "自由拾取",
            1: "精良",
            4: "优质",
            7: "完美",
        }.get(pinzhi_val, str(pinzhi_val))
        msg = (
            f"team_flags auto={auto_b} invite={invite_b} "
            f"pinzhi={qname} u32=0x{flags_u32:08X} "
            f"host=0x{host:X} ret={ret}"
        )
        log(f"team: {msg}")
        return TeamOpResult(
            ok=True,
            action="team_flags",
            message=(
                f"招人={'开' if auto_admit else '关'} · "
                f"队员邀请={'开' if member_invite else '关'} · "
                f"{qname}"
            ),
            detail={
                "auto_admit": bool(auto_admit),
                "member_invite": bool(member_invite),
                "mode_word": int(mode_word) & 0xFFFF,
                "pinzhi": pinzhi_val,
                "pinzhi_name": qname,
                "flags_u32": flags_u32,
                "host": host,
                "ret": int(ret or 0),
                "path": "cd04d0_0x1263_zeroed",
            },
        )
    except Exception as e:
        log(f"team: set_invite_flags failed: {e}")
        return TeamOpResult(
            ok=False, action="team_flags", message=str(e), error=str(e)
        )
    finally:
        try:
            if handle and pkt_va:
                remote_free(handle, pkt_va)
        except Exception:
            pass
        try:
            if handle:
                from app.core.remote_runtime import kernel32

                kernel32.CloseHandle(handle)
        except Exception:
            pass


def set_team_loot_rule(
    session,
    *,
    mainrule: int = TEAM_LOOT_MAIN_TEAM,
    subrule: int = TEAM_LOOT_SUB_DEFAULT,
    apply: bool = True,
    extra: int = 0,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Set team loot rule (c2s 0x1273 via CEC7A0).

    UI: 队伍分配品质. mainrule=6/7 → 队伍分配; mainrule=1 → 自由拾取.
    subrule: 1=精良, 4=优质, 7=完美 (Lua OnEventTeamChangeLootRule).
    Default: 队伍分配 + 优质.
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False,
            action="team_loot",
            message=f"远程不可用: {brsn}",
            error=brsn or "remote_blocked",
        )
    host = get_host_player_for_team(session, log=log)
    if not host:
        return TeamOpResult(
            ok=False, action="team_loot", message="host null", error="no_host"
        )
    try:
        va = _note_va(session, NOTE_VA_TEAM_SET_LOOT)
        a0 = 1 if apply else 0
        a1 = int(mainrule) & 0xFF
        a2 = int(subrule) & 0xFFFFFFFF
        a3 = int(extra) & 0xFF
        ret = remote_call_thiscall_x86(
            int(session.pid),
            va,
            host,
            [a0, a1, a2, a3],
            timeout_ms=4000,
        )
        qname = {1: "精良", 4: "优质", 7: "完美"}.get(int(subrule), str(subrule))
        mode = "自由拾取" if int(mainrule) == 1 else "队伍分配"
        msg = (
            f"team_loot {mode}/{qname} main={a1} sub={a2} "
            f"host=0x{host:X} ret={ret}"
        )
        log(f"team: {msg}")
        return TeamOpResult(
            ok=True,
            action="team_loot",
            message=f"已设置拾取：{mode} · {qname}",
            detail={
                "mainrule": a1,
                "subrule": a2,
                "apply": bool(apply),
                "extra": a3,
                "host": host,
                "ret": int(ret or 0),
                "path": "cec7a0_0x1273",
            },
        )
    except Exception as e:
        log(f"team: set_loot_rule failed: {e}")
        return TeamOpResult(
            ok=False, action="team_loot", message=str(e), error=str(e)
        )


def apply_captain_team_defaults(
    session,
    *,
    auto_admit: bool = True,
    member_invite: bool = True,
    loot_main: int = TEAM_LOOT_MAIN_TEAM,
    loot_sub: int = TEAM_LOOT_SUB_DEFAULT,
    require_captain: bool = True,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Captain defaults after create/invite:
      - 允许队员邀请
      - 自动招人
      - 拾取：队伍分配 + 优质

    Safe to call when solo (pending team) or already captain.
    Skips when require_captain and host is a non-leader member.
    @author by ak
    """
    log = log or (lambda _m: None)
    detail: dict = {}
    # 0x1263 在 UI 侧仅在已有队伍时发送；刚发出邀请尚未入队时稍等再试
    for attempt in range(1, 4):
        try:
            from app.core.plg_ui import host_team_role

            role = host_team_role(session, log=log) or {}
            detail["role"] = role
            detail["role_attempt"] = attempt
            if role.get("in_team") and role.get("role") == "member":
                if require_captain:
                    msg = "跳过队伍默认设置：本号是队员"
                    log(f"team: defaults skip ({msg})")
                    return TeamOpResult(
                        ok=True,
                        action="team_defaults",
                        message=msg,
                        detail=detail,
                    )
            if role.get("in_team") or attempt >= 3:
                break
            time.sleep(0.7)
        except Exception as e:
            log(f"team: defaults role probe: {e}")
            break

    # 品质必须打进 0x1263 的 mode_word；0x1273 不是组队面板主路径
    fr = set_team_invite_flags(
        session,
        auto_admit=auto_admit,
        member_invite=member_invite,
        pinzhi=int(loot_sub),
        log=log,
    )
    detail["flags"] = fr.to_dict()
    time.sleep(0.12)
    # 备用：再发一次 0x1273（队伍分配+同品质），兼容其他 UI 状态源
    lr = set_team_loot_rule(
        session,
        mainrule=loot_main,
        subrule=loot_sub,
        log=log,
    )
    detail["loot"] = lr.to_dict()
    ok = bool(fr.ok)
    # 面板只给短句；技术细节进日志
    if fr.ok:
        msg = "招人开 · 队员可邀请 · 拾取优质"
    else:
        msg = "队伍规则未生效"
    log(
        f"team: defaults ok={ok} flags={getattr(fr, 'message', '')} "
        f"loot_ok={bool(getattr(lr, 'ok', False))} loot={getattr(lr, 'message', '')}"
    )
    return TeamOpResult(
        ok=ok,
        action="team_defaults",
        message=msg,
        error=None if ok else (fr.error or "defaults_failed"),
        detail=detail,
    )


def find_nearby_players_by_names(

    session,
    names: list[str],
    *,
    radius: float | None = None,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Match AOI CLASS_PLAYER objects by exact display name.

    radius=None → no client-side radius filter (still only AOI-visible players).
    @author by ak
    """
    log = log or (lambda _m: None)
    want = [n for n in names if n]
    if not want:
        return []
    want_set = set(want)
    try:
        objs = list_class_objects(
            session,
            CLASS_PLAYER,
            limit=96,
            radius=radius,
            read_name=True,
            read_tid=False,
            log=log,
        )
    except Exception as e:
        log(f"team: list players err: {e}")
        return []

    from app.core.plg_interact import get_object_id64

    hits: list[dict] = []
    for o in objs or []:
        nm = str(getattr(o, "name", "") or "").strip()
        if not nm or nm not in want_set:
            continue
        ptr = int(getattr(o, "ptr", 0) or 0) & 0xFFFFFFFF
        oid = get_object_id64(session, ptr) if ptr else None
        if not oid:
            continue
        hits.append(
            {
                "name": nm,
                "ptr": ptr,
                "obj_id": int(oid),
                "dist": getattr(o, "dist", None),
            }
        )
    return hits



def list_live_xajh_pids(*, exclude_pid: int | None = None) -> list[int]:
    """
    Diagnostic helper: all running xajh.exe pids.

    Do NOT use this for validate/invite multi resolve — whole-machine CRT
    attach was a helper crash source. Prefer store.list() mounted pids only.
    @author by ak
    """
    ex = int(exclude_pid or 0)
    out: list[int] = []
    try:
        import psutil

        for p in psutil.process_iter(["pid", "name"]):
            try:
                n = (p.info.get("name") or "").lower()
                pid = int(p.info.get("pid") or 0)
            except Exception:
                continue
            if not pid or pid == ex:
                continue
            if "xajh" in n:
                out.append(pid)
    except Exception:
        pass
    return out


def _safe_peer_identity(
    pid: int,
    *,
    need_name: bool = False,
    log: LogFn | None = None,
) -> tuple[str, int]:
    """
    Safely read another mounted multi-box host identity.

    Dispatch rules:
      - skip blocked / hard_dead / hung pids
      - ensure_callable(CRT_READ) before any remote attach/CRT
      - prefer StateKind.HOST_ID when name is not required
      - never raise; failures return ("", 0)

    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        spid = int(pid or 0)
    except Exception:
        spid = 0
    if spid <= 0:
        return "", 0

    blocked, brsn = _pid_blocked(spid)
    if blocked:
        log(f"team: multi skip pid={spid} blocked={brsn}")
        return "", 0

    try:
        from app.core.safe_dispatch import OpKind, get_dispatch

        get_dispatch().ensure_callable(spid, kind=OpKind.CRT_READ)
    except Exception as e:
        # Gate already recorded health; do not note_exception here (avoid false hard_dead).
        log(f"team: multi skip pid={spid} gate={e}")
        return "", 0

    attach = None
    try:
        from app.core.loot import open_attach_session

        attach = open_attach_session(spid, log=lambda _m: None)
        if not int(getattr(attach, "module_base", 0) or 0):
            log(f"team: multi skip pid={spid} no module_base")
            return "", 0

        if not need_name:
            try:
                from app.core.state_dispatch import StateKind, get_state

                oid = get_state(
                    attach,
                    StateKind.HOST_ID,
                    fresh=True,
                    log=lambda _m: None,
                )
                if oid:
                    return "", int(oid)
            except Exception:
                pass

        name, oid = read_host_identity(
            attach, need_name=bool(need_name), log=lambda _m: None
        )
        return str(name or "").strip(), int(oid or 0)
    except Exception as e:
        log(f"team: multi resolve pid={spid} err: {e}")
        try:
            from app.core.safe_dispatch import get_dispatch

            get_dispatch().note_exception(spid, e)
        except Exception:
            pass
        return "", 0
    finally:
        if attach is not None:
            try:
                attach.close()
            except Exception:
                pass


def _alloc_remote_wstring(pid: int, text: str) -> tuple[int, int, int]:
    """
    Write utf-16-le + NUL into remote process.

    Returns (handle, addr, nbytes). Caller must CloseHandle + remote_free.
    @author by ak
    """
    from app.core.remote_runtime import open_process, remote_alloc, write_process

    raw = (str(text or "") + "\0").encode("utf-16-le")
    handle = open_process(int(pid))
    addr = remote_alloc(handle, len(raw) + 8)
    write_process(handle, addr, raw)
    return handle, int(addr) & 0xFFFFFFFF, len(raw)


def get_world_ptr(session, *, log: LogFn | None = None) -> int:
    """Game world* via note 0x4AE3B0. @author by ak"""
    log = log or (lambda _m: None)
    try:
        va = _note_va(session, NOTE_VA_GET_WORLD)
        return int(remote_call_cdecl_x86(int(session.pid), va, [], timeout_ms=2500) or 0) & 0xFFFFFFFF
    except Exception as e:
        log(f"team world err: {e}")
        return 0


def lookup_cached_player_id_by_name(
    session,
    name: str,
    *,
    log: LogFn | None = None,
) -> int:
    """
    Read name→id from client cache (filled by GetPlayerIDByName_Re).

    Uses 0x499DE0(world, wchar* name) → edx:eax.
    @author by ak
    """
    from app.core.remote_runtime import (
        kernel32,
        open_process,
        remote_free,
        remote_call_x86,
        CallConvention,
        ReturnKind,
        pid_call_mutex,
    )
    from ctypes import wintypes

    log = log or (lambda _m: None)
    name = str(name or "").strip()
    if not name:
        return 0
    world = get_world_ptr(session, log=log)
    if not world:
        return 0
    pid = int(session.pid)
    handle = 0
    addr = 0
    try:
        handle, addr, _n = _alloc_remote_wstring(pid, name)
        va = _note_va(session, NOTE_VA_NAME_CACHE_LOOKUP)
        # thiscall world, arg0=wchar*; 64-bit id in edx:eax
        # Do NOT wrap pid_call_mutex here: remote_call_x86 already serializes.
        # Nested outer mutex extended hold through grace-wait and worsened
        # concurrent UI features (校验队伍) under load.
        ret = remote_call_x86(
            pid,
            va,
            [addr],
            convention=CallConvention.THISCALL,
            this_ptr=world,
            return_kind=ReturnKind.U64,
            timeout_ms=3000,
        )
        oid = int(ret or 0)
        if oid <= 0:
            return 0
        # filter obvious garbage (type tag only in high bits of lo dword is ok)
        return oid
    except Exception as e:
        log(f"team: name cache lookup {name!r} err: {e}")
        return 0
    finally:
        try:
            if handle and addr:
                remote_free(handle, addr)
        except Exception:
            pass
        try:
            if handle:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass


def send_get_player_id_by_name(
    session,
    name: str,
    *,
    reason: int = NAME_QUERY_REASON_CACHE_ONLY,
    log: LogFn | None = None,
) -> bool:
    """
    Send c2s GetPlayerIDByName (0x76) via 0xCF0620(host, wname*, reason).

    reason=1 → 组队输名邀请(回包自动 0x125c)；reason=2 → 只缓存 id；reason=8 → 聊天邀请列表。
    @author by ak
    """
    from app.core.remote_runtime import kernel32, remote_free
    from ctypes import wintypes

    log = log or (lambda _m: None)
    name = str(name or "").strip()
    if not name:
        return False
    host = get_host_player_for_team(session, log=log)
    if not host:
        log("team: getplayeridbyname no host")
        return False
    pid = int(session.pid)
    handle = 0
    addr = 0
    try:
        handle, addr, _n = _alloc_remote_wstring(pid, name)
        va = _note_va(session, NOTE_VA_GET_PLAYER_ID_BY_NAME)
        ret = remote_call_thiscall_x86(
            pid,
            va,
            host,
            [addr, int(reason) & 0xFF],
            timeout_ms=4000,
        )
        log(
            f"team: getplayeridbyname name={name!r} reason={int(reason)} "
            f"host=0x{host:X} ret={ret}"
        )
        return True
    except Exception as e:
        log(f"team: getplayeridbyname {name!r} err: {e}")
        return False
    finally:
        try:
            if handle and addr:
                remote_free(handle, addr)
        except Exception:
            pass
        try:
            if handle:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:
            pass


def resolve_player_id_by_protocol_name(
    session,
    name: str,
    *,
    timeout_s: float = 2.5,
    poll_s: float = 0.25,
    log: LogFn | None = None,
) -> int:
    """
    协议按名查 id：发 0x76 (reason=2) → 轮询客户端名缓存。

    @author by ak
    """
    log = log or (lambda _m: None)
    name = str(name or "").strip()
    if not name:
        return 0
    # already cached?
    oid = lookup_cached_player_id_by_name(session, name, log=log)
    if oid > 0:
        log(f"team: protocol cache hit name={name!r} id={oid}")
        return oid
    if not send_get_player_id_by_name(
        session, name, reason=NAME_QUERY_REASON_CACHE_ONLY, log=log
    ):
        return 0
    deadline = time.time() + max(0.5, float(timeout_s))
    while time.time() < deadline:
        time.sleep(max(0.05, float(poll_s)))
        oid = lookup_cached_player_id_by_name(session, name, log=log)
        if oid > 0:
            log(f"team: protocol resolve name={name!r} id={oid}")
            return int(oid)
    log(f"team: protocol resolve timeout name={name!r}")
    return 0


def resolve_member_targets(
    session,
    names: list[str] | str,
    *,
    store=None,
    exclude_pid: int | None = None,
    host_name: str | None = None,
    host_id: int | None = None,
    prefer_nearby_radius: float | None = None,
    log: LogFn | None = None,
) -> list[TeamMemberTarget]:
    """
    Resolve roster names → invite targets.

    Order per token:
      0) pure decimal id — trusted as-is
      1) multi-box: only UI-mounted store pids (SafeDispatch gated)
      2) nearby/AOI
      3) protocol GetPlayerIDByName (0x76) + name cache

    Skips the captain's own display name (and own id when known) for invite lists.
    validate_team_roster writes host id into the verified cache separately.
    If every invitee token is a pure id, multi/nearby lookups are skipped entirely.
    @author by ak
    """
    log = log or (lambda _m: None)
    if isinstance(names, str):
        name_list = parse_team_member_names(names)
    else:
        name_list = [str(n).strip() for n in (names or []) if str(n).strip()]

    if host_name is None or host_id is None:
        # Prefer HOST_ID warm; only take name CRT when host_name is missing.
        try:
            if host_id is None:
                from app.core.state_dispatch import StateKind, get_state, prefetch_states

                prefetch_states(session, (StateKind.HOST_ID,), log=lambda _m: None)
                warm = get_state(
                    session, StateKind.HOST_ID, fresh=False, log=lambda _m: None
                )
                if warm:
                    host_id = int(warm)
        except Exception as e:
            log(f"team: resolve host_id warm err: {e}")
        try:
            need_name = host_name is None
            if need_name or host_id is None or int(host_id or 0) <= 0:
                hn, hid = read_host_identity(
                    session, need_name=bool(need_name), log=log
                )
                if host_name is None:
                    host_name = hn
                if host_id is None or int(host_id or 0) <= 0:
                    host_id = hid
        except Exception as e:
            log(f"team: resolve host identity err: {e}")
            if host_name is None:
                host_name = ""
            if host_id is None:
                host_id = 0
    host_name = str(host_name or "").strip()
    try:
        host_id_i = int(host_id or 0)
    except Exception:
        host_id_i = 0

    # Drop self (own character name or own numeric id)
    pending: list[str] = []
    for n in name_list:
        if names_match(n, host_name):
            continue
        lit = parse_literal_member_id(n)
        if lit is not None and host_id_i and lit == host_id_i:
            log(f"team: skip self id={lit}")
            continue
        pending.append(n)
    if not pending:
        return []

    found: dict[str, TeamMemberTarget] = {}

    # 0) literal ids first — trusted, no query
    for n in pending:
        lit = parse_literal_member_id(n)
        if lit is not None:
            found[n] = TeamMemberTarget(name=n, obj_id=int(lit), source="literal_id")
            log(f"team: accept literal id={lit}")

    need_lookup = [n for n in pending if n not in found]
    if not need_lookup:
        # All invitees are pure ids — do not touch multi-box / AOI.
        return [found[n] for n in pending if n in found]

    # 1) multi-box: ONLY already-mounted store sessions (no whole-machine CRT scan)
    ex = (
        int(exclude_pid)
        if exclude_pid is not None
        else int(getattr(session, "pid", 0) or 0)
    )
    peer_rows: list[tuple[int, str]] = []
    seen_pid: set[int] = set()
    if store is not None:
        try:
            from app.core.window_title import parse_role_name_from_title

            for sess in list(store.list() or []):
                try:
                    spid = int(getattr(sess, "pid", 0) or 0)
                except Exception:
                    continue
                if not spid or spid == ex or spid in seen_pid:
                    continue
                seen_pid.add(spid)
                title_name = ""
                try:
                    title_name = str(
                        parse_role_name_from_title(
                            str(getattr(sess, "title", "") or "")
                            or str(getattr(sess, "original_title", "") or "")
                        )
                        or ""
                    ).strip()
                except Exception:
                    title_name = ""
                peer_rows.append((spid, title_name))
        except Exception as e:
            log(f"team: multi store scan err: {e}")
    if peer_rows:
        log(f"team: multi scan pids={[p for p, _ in peer_rows]}")

    for spid, title_name in peer_rows:
        if all(n in found for n in need_lookup):
            break
        candidates = [n for n in need_lookup if n not in found]
        if not candidates:
            break

        # Title hint: if present and matches none, skip CRT entirely for this pid.
        if title_name:
            matched = [n for n in candidates if names_match(n, title_name)]
            if not matched:
                continue
            _peer_name, peer_id = _safe_peer_identity(
                spid, need_name=False, log=log
            )
            if peer_id <= 0:
                continue
            for n in matched:
                if n in found:
                    continue
                found[n] = TeamMemberTarget(
                    name=n,
                    obj_id=int(peer_id),
                    source="multi",
                    pid=spid,
                )
                log(f"team: resolve multi name={n!r} id={peer_id} pid={spid}")
            continue

        # No title hint: need name CRT, still fully gated.
        peer_name, peer_id = _safe_peer_identity(spid, need_name=True, log=log)
        if not peer_name or peer_id <= 0:
            continue
        for n in candidates:
            if n in found:
                continue
            if names_match(n, peer_name):
                found[n] = TeamMemberTarget(
                    name=n,
                    obj_id=int(peer_id),
                    source="multi",
                    pid=spid,
                )
                log(f"team: resolve multi name={n!r} id={peer_id} pid={spid}")

    # 2) nearby / AOI
    still = [n for n in need_lookup if n not in found]
    if still:
        near = find_nearby_players_by_names(
            session, still, radius=prefer_nearby_radius, log=log
        )
        for hit in near:
            n = str(hit.get("name") or "")
            if n and n not in found:
                found[n] = TeamMemberTarget(
                    name=n,
                    obj_id=int(hit["obj_id"]),
                    source="nearby",
                )
                log(f"team: resolve nearby name={n!r} id={hit['obj_id']}")

    # 3) protocol GetPlayerIDByName (0x76 reason=2) → name cache
    still = [n for n in need_lookup if n not in found]
    for n in still:
        oid = resolve_player_id_by_protocol_name(session, n, log=log)
        if oid > 0:
            found[n] = TeamMemberTarget(name=n, obj_id=int(oid), source="protocol")
            log(f"team: resolve protocol name={n!r} id={oid}")

    # preserve roster order
    out: list[TeamMemberTarget] = []
    for n in pending:
        if n in found:
            out.append(found[n])
    return out


def roster_fingerprint(text: str | Iterable[str]) -> str:
    """
    Stable fingerprint for a roster text / name list.

    Used to invalidate the verified-id cache when the user edits members.
    @author by ak
    """
    if isinstance(text, str):
        names = parse_team_member_names(text)
    else:
        names = [str(n).strip() for n in (text or []) if str(n).strip()]
    return "\u3001".join(names)


def targets_from_verified_roster(
    roster: Iterable[dict] | None,
    *,
    skip_host_name: str | None = None,
    skip_host_id: int | None = None,
) -> list[TeamMemberTarget]:
    """
    Build invite targets from a cached verified roster.

    缓存里「校验时的 is_self」只是历史标记，id 一律保留，方便换主号后邀请原队长。
    真正跳过自己：用当前会话 host_name / host_id 判断。

    @author by ak
    """
    out: list[TeamMemberTarget] = []
    skip_name = str(skip_host_name or "").strip()
    try:
        skip_id = int(skip_host_id or 0)
    except Exception:
        skip_id = 0
    for raw in roster or []:
        if not isinstance(raw, dict):
            continue
        try:
            oid = int(raw.get("obj_id") or 0)
        except Exception:
            oid = 0
        if oid <= 0:
            continue
        token = str(raw.get("token") or raw.get("name") or "").strip()
        name = str(raw.get("name") or token).strip() or token
        # 当前主号不邀请自己
        if skip_id and oid == skip_id:
            continue
        if skip_name and (
            names_match(name, skip_name)
            or names_match(token, skip_name)
            or (is_literal_member_id(token) and parse_literal_member_id(token) == skip_id)
        ):
            continue
        src = str(raw.get("source") or "cache")
        pid = raw.get("pid")
        try:
            pid_i = int(pid) if pid is not None else None
        except Exception:
            pid_i = None
        out.append(
            TeamMemberTarget(
                name=name or token,
                obj_id=oid,
                source=src,
                pid=pid_i,
            )
        )
    return out


def roster_all_literal_ids(members_text: str | Iterable[str]) -> bool:
    """
    True when every roster token is a pure decimal id.

    Self-name tokens are not pure ids, so mixed name+id returns False.
    @author by ak
    """
    if isinstance(members_text, str):
        names = parse_team_member_names(members_text)
    else:
        names = [str(n).strip() for n in (members_text or []) if str(n).strip()]
    if not names:
        return False
    return all(is_literal_member_id(n) for n in names)


def build_literal_id_roster(members_text: str | Iterable[str]) -> list[dict]:
    """Build a verified-style roster from pure numeric tokens. @author by ak"""
    if isinstance(members_text, str):
        names = parse_team_member_names(members_text)
    else:
        names = [str(n).strip() for n in (members_text or []) if str(n).strip()]
    roster: list[dict] = []
    for n in names:
        lit = parse_literal_member_id(n)
        if lit is None:
            continue
        roster.append(
            {
                "token": n,
                "name": n,
                "obj_id": int(lit),
                "source": "literal_id",
                "is_self": False,
                "pid": None,
            }
        )
    return roster


def is_team_roster_verified(members_text: str | Iterable[str], settings: dict | None) -> bool:
    """
    True when roster is ready to invite:

    - every token is a pure numeric id (trusted; no query / no prior verify), or
    - settings hold a complete verified id cache for this exact roster

    @author by ak
    """
    settings = settings or {}
    fp = roster_fingerprint(members_text)
    if not fp:
        return False

    # Pure numeric roster: always ready (no cache required).
    if roster_all_literal_ids(members_text):
        return True

    if str(settings.get("team_verified_text") or "") != fp:
        return False
    roster = settings.get("team_verified_roster") or []
    if not isinstance(roster, list) or not roster:
        return False
    # 全员（含自己）都必须有有效 id，换主号时原队长 id 仍可用
    any_ok = False
    for r in roster:
        if not isinstance(r, dict):
            return False
        try:
            oid = int(r.get("obj_id") or 0)
        except Exception:
            return False
        if oid <= 0:
            return False
        any_ok = True
    return any_ok


def invite_targets_for_members(
    members_text: str | Iterable[str],
    settings: dict | None = None,
    *,
    host_name: str | None = None,
    host_id: int | None = None,
) -> list[TeamMemberTarget]:
    """
    Invite targets from pure-id text or verified cache.

    host_name / host_id: 当前会话身份，用来排除自己（不依赖缓存里的 is_self）。
    Empty list means not ready / nothing to invite.
    @author by ak
    """
    settings = settings or {}
    if isinstance(members_text, str):
        names = parse_team_member_names(members_text)
    else:
        names = [str(n).strip() for n in (members_text or []) if str(n).strip()]
    if not names:
        return []

    try:
        skip_id = int(host_id or 0)
    except Exception:
        skip_id = 0
    skip_name = str(host_name or "").strip()

    if roster_all_literal_ids(names):
        out: list[TeamMemberTarget] = []
        for n in names:
            lit = parse_literal_member_id(n)
            if lit is None:
                continue
            if skip_id and int(lit) == skip_id:
                continue
            out.append(
                TeamMemberTarget(name=n, obj_id=int(lit), source="literal_id")
            )
        return out

    if str(settings.get("team_verified_text") or "") != roster_fingerprint(names):
        return []
    return targets_from_verified_roster(
        settings.get("team_verified_roster"),
        skip_host_name=skip_name or None,
        skip_host_id=skip_id or None,
    )



def _team_prefs_path() -> Path:
    """Disk path for team roster + verified id cache. @author by ak"""
    try:
        from common.paths import ensure_writable_dir

        base = ensure_writable_dir("runtime", "config")
    except Exception:
        try:
            base = Path(__file__).resolve().parents[2] / "runtime" / "config"
        except Exception:
            base = Path.cwd() / "runtime" / "config"
    return Path(base) / "team_prefs.json"


def load_team_prefs() -> dict:
    """
    Load persisted team roster + verified cache (survives restart).

    Shape:
      {
        "team_members": str,
        "team_verified_text": str,
        "team_verified_roster": list[dict],
      }
    @author by ak
    """
    out = {
        "team_members": "",
        "team_verified_text": "",
        "team_verified_roster": [],
    }
    path = _team_prefs_path()
    try:
        if not path.is_file():
            return out
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return out
        out["team_members"] = str(data.get("team_members") or "").strip()
        out["team_verified_text"] = str(data.get("team_verified_text") or "").strip()
        roster = data.get("team_verified_roster") or []
        if isinstance(roster, list):
            out["team_verified_roster"] = [r for r in roster if isinstance(r, dict)]
        # consistency: if members empty, drop cache
        if not out["team_members"]:
            out["team_verified_text"] = ""
            out["team_verified_roster"] = []
        elif out["team_verified_text"] and out["team_verified_text"] != roster_fingerprint(
            out["team_members"]
        ):
            # fingerprint mismatch → keep members text, drop stale cache
            out["team_verified_text"] = ""
            out["team_verified_roster"] = []
    except Exception:
        pass
    return out


def save_team_prefs(
    *,
    members: str = "",
    verified_text: str = "",
    verified_roster: list | None = None,
    settings: dict | None = None,
) -> None:
    """
    Persist team roster + verified cache to disk.

    If settings dict is given, also mirror values into it.
    @author by ak
    """
    members_s = str(members or "").strip()
    if settings is not None and isinstance(settings, dict):
        members_s = str(settings.get("team_members") or members_s).strip()
        verified_text = str(settings.get("team_verified_text") or verified_text or "")
        vr = settings.get("team_verified_roster")
        if isinstance(vr, list):
            verified_roster = vr
    roster = list(verified_roster or []) if isinstance(verified_roster, list) else []
    # drop cache when members empty
    if not members_s:
        verified_text = ""
        roster = []
    payload = {
        "team_members": members_s,
        "team_verified_text": str(verified_text or ""),
        "team_verified_roster": roster,
    }
    if isinstance(settings, dict):
        settings["team_members"] = members_s
        settings["team_verified_text"] = payload["team_verified_text"]
        settings["team_verified_roster"] = roster
    path = _team_prefs_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def clear_team_verified_cache(
    settings: dict | None, *, persist: bool = False
) -> None:
    """Drop verified roster cache in settings. @author by ak"""
    if not isinstance(settings, dict):
        return
    settings["team_verified_text"] = ""
    settings["team_verified_roster"] = []
    if persist:
        save_team_prefs(settings=settings)


def save_team_verified_cache(
    settings: dict | None,
    members_text: str | Iterable[str],
    roster: list[dict],
    *,
    persist: bool = True,
) -> None:
    """Persist verified roster + fingerprint (memory + disk by default). @author by ak"""
    if not isinstance(settings, dict):
        return
    if isinstance(members_text, str):
        members_s = members_text
    else:
        members_s = "\u3001".join(
            [str(n).strip() for n in (members_text or []) if str(n).strip()]
        )
    settings["team_members"] = str(
        settings.get("team_members") or members_s or ""
    ).strip() or str(members_s or "").strip()
    settings["team_verified_text"] = roster_fingerprint(members_text)
    settings["team_verified_roster"] = list(roster or [])
    if persist:
        save_team_prefs(settings=settings)


def validate_team_roster(
    session,
    names: list[str] | str,
    *,
    store=None,
    exclude_pid: int | None = None,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Resolve every roster token to a player id (or mark self).

    Success only when every non-self token has obj_id > 0.
    Literal decimal ids in the list are accepted without lookup.

    @author by ak
    """
    log = log or (lambda _m: None)
    if isinstance(names, str):
        name_list = parse_team_member_names(names)
        members_text = names
    else:
        name_list = [str(n).strip() for n in (names or []) if str(n).strip()]
        members_text = "\u3001".join(name_list)

    if not name_list:
        return TeamOpResult(
            ok=False,
            action="validate_roster",
            message="请先填写成员（角色名或数字 id）",
            error="empty",
            detail={"roster": [], "fingerprint": ""},
        )

    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False,
            action="validate_roster",
            message=f"远程不可用，请稍后重试: {brsn}",
            error=brsn or "remote_blocked",
            detail={"roster": [], "fingerprint": ""},
        )

    # Pure decimal id roster: never call GetObjectName (historical crash hotspot).
    pure_id_roster = bool(name_list) and all(
        parse_literal_member_id(n) is not None for n in name_list
    )
    need_host_name = not pure_id_roster
    host_name = ""
    host_id_i = 0
    # Always warm HOST_ID first (CRT_READ, no GetObjectName).
    try:
        from app.core.state_dispatch import StateKind, get_state, prefetch_states

        prefetch_states(session, (StateKind.HOST_ID,), log=lambda _m: None)
        warm_id = get_state(
            session, StateKind.HOST_ID, fresh=False, log=lambda _m: None
        )
        if warm_id:
            host_id_i = int(warm_id)
    except Exception as e:
        log(f"team: validate host_id warm err: {e}")

    if host_id_i <= 0 or need_host_name:
        try:
            hn, hid = read_host_identity(
                session, need_name=bool(need_host_name), log=log
            )
            host_name = str(hn or "").strip()
            if host_id_i <= 0:
                host_id_i = int(hid or 0)
        except Exception as e:
            log(f"team: validate host identity err: {e}")

    host_name = str(host_name or "").strip()
    if need_host_name and host_id_i <= 0 and not host_name:
        return TeamOpResult(
            ok=False,
            action="validate_roster",
            message="读取本号身份失败，请稍后重试",
            error="host_identity",
            detail={
                "roster": [],
                "fingerprint": roster_fingerprint(name_list),
            },
        )

    # Pure-id roster: accept locally, skip multi/AOI entirely (resolve short-circuits).
    targets = resolve_member_targets(
        session,
        name_list,
        store=store,
        exclude_pid=exclude_pid,
        host_name=host_name,
        host_id=host_id_i,
        log=log,
    )
    by_token = {t.name: t for t in targets}

    roster: list[dict] = []
    missing: list[str] = []
    self_tokens: list[str] = []
    self_labels: list[str] = []
    for n in name_list:
        lit = parse_literal_member_id(n)
        is_self_tok = names_match(n, host_name) or (
            lit is not None and host_id_i > 0 and int(lit) == host_id_i
        )
        if is_self_tok:
            self_tokens.append(n)
            # 显示真实角色名；若名单填的是自己 id，也回落成读到的 host_name
            label = host_name or (str(n).strip() if lit is None else f"id={host_id_i}")
            if label and label not in self_labels:
                self_labels.append(label)
            roster.append(
                {
                    "token": n,
                    "name": host_name or n,
                    "obj_id": host_id_i,
                    "source": "self",
                    "is_self": True,
                    "pid": int(getattr(session, "pid", 0) or 0) or None,
                }
            )
            continue
        t = by_token.get(n)
        if t is None:
            # resolve_member_targets may keep original token as name
            for cand in targets:
                if names_match(cand.name, n):
                    t = cand
                    break
        if t is None or int(t.obj_id or 0) <= 0:
            missing.append(n)
            roster.append(
                {
                    "token": n,
                    "name": n,
                    "obj_id": 0,
                    "source": "",
                    "is_self": False,
                    "pid": None,
                }
            )
            continue
        roster.append(
            {
                "token": n,
                "name": t.name,
                "obj_id": int(t.obj_id),
                "source": str(t.source or ""),
                "is_self": False,
                "pid": t.pid,
            }
        )

    invitees = [r for r in roster if not r.get("is_self")]
    fp = roster_fingerprint(name_list)
    self_skip_label = "、".join(self_labels) if self_labels else (host_name or "")

    def _disp_token(r: dict) -> str:
        """Show entered token; only keep numeric id form when user typed id. @author by ak"""
        tok = str(r.get("token") or "").strip()
        name = str(r.get("name") or "").strip()
        if is_literal_member_id(tok):
            return tok
        return tok or name

    found_names = [
        _disp_token(r)
        for r in invitees
        if int(r.get("obj_id") or 0) > 0 and _disp_token(r)
    ]
    # 全名单展示（含本号 token），不写「自己」、不附加 #id
    all_disp = []
    for r in roster:
        if int(r.get("obj_id") or 0) <= 0 and not r.get("is_self"):
            continue
        lab = _disp_token(r)
        if lab and lab not in all_disp:
            all_disp.append(lab)

    if missing:
        parts = [f"未找到：{'、'.join(missing)}"]
        if found_names:
            parts.append("已找到：" + "、".join(found_names))
        msg = "；".join(parts)

        log(f"team: validate fail {msg} host={host_name!r}#{host_id_i}")
        return TeamOpResult(
            ok=False,
            action="validate_roster",
            message=msg,
            error="unresolved",
            detail={
                "host_name": host_name,
                "host_id": host_id_i,
                "roster": roster,
                "missing": missing,
                "found": found_names,
                "fingerprint": fp,
            },
        )
    if not invitees:
        msg = "名单里没有可邀请成员"
        log(f"team: validate fail {msg} host={host_name!r}#{host_id_i}")
        return TeamOpResult(
            ok=False,
            action="validate_roster",
            message=msg,
            error="only_self",
            detail={
                "host_name": host_name,
                "host_id": host_id_i,
                "roster": roster,
                "missing": [],
                "fingerprint": fp,
            },
        )
    if host_id_i <= 0 and self_tokens:
        msg = f"未读到本号角色id（{self_skip_label or '本号'}），请确认已挂载"
        log(f"team: validate fail {msg}")
        return TeamOpResult(
            ok=False,
            action="validate_roster",
            message=msg,
            error="no_host_id",
            detail={
                "host_name": host_name,
                "host_id": host_id_i,
                "roster": roster,
                "missing": [],
                "fingerprint": fp,
            },
        )

    pure = all((r.get("source") == "literal_id") for r in invitees)
    prefix = "校验通过（纯id）：" if pure else "校验通过："
    msg = prefix + "、".join(all_disp or found_names)
    log(f"team: validate ok {msg} host={host_name!r}#{host_id_i}")
    return TeamOpResult(
        ok=True,
        action="validate_roster",
        message=msg,
        error=None,
        detail={
            "host_name": host_name,
            "host_id": host_id_i,
            "roster": roster,
            "missing": [],
            "fingerprint": fp,
            "members_text": members_text,
            "targets": [asdict(t) for t in targets],
        },
    )


def invite_members_by_targets(
    session,
    targets: list[TeamMemberTarget] | list[dict],
    *,
    leave_if_teamed: bool = False,
    skip_already_in_party: bool = True,
    store=None,
    exclude_pid: int | None = None,
    party: list[dict] | None = None,
    pause_s: float = 0.35,
    rounds: int = 2,
    apply_defaults: bool = True,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Invite using pre-resolved / cached targets (no re-resolve).

    - leave_if_teamed: leave only when stuck (caller should use should_leave_before_form)
    - skip_already_in_party: do not re-invite people already in current team

    @author by ak
    """
    log = log or (lambda _m: None)
    left = False
    ensure_note = ""
    if leave_if_teamed:
        if should_leave_before_form(session, log=log):
            leave_team(session, log=log)
            left = True
            time.sleep(0.25)
        else:
            log("team: skip leave (未组队或已是队长)")
    else:
        # 全部邀请：只发包，绝不离队（避免误判队长后 leave 把队长交出去）
        try:
            from app.core.plg_ui import host_team_role

            role = host_team_role(session, log=log)
            log(
                "team: role "
                f"in_team={role.get('in_team')} role={role.get('role')} "
                f"host={int(role.get('host_lo') or 0):X}:{int(role.get('host_hi') or 0):X} "
                f"leader={int(role.get('leader_lo') or 0):X}:{int(role.get('leader_hi') or 0):X} "
                f"(invite_only no-leave)"
            )
            if role.get("role") == "member":
                ensure_note = "本号是队员仍发邀请（可能被服务器忽略）"
        except Exception as e:
            log(f"team: role probe err: {e}")

    # If already captain, never re-invite people already in party (even if caller
    # passed skip_already_in_party=False). Re-inviting in-party members is useless
    # and can disturb party state / look like leadership shuffle.
    try:
        from app.core.plg_ui import host_team_role

        role_now = host_team_role(session, log=log)
        if role_now.get("role") == "leader":
            if not skip_already_in_party:
                log("team: already leader — force skip_already_in_party")
            skip_already_in_party = True
    except Exception as e:
        log(f"team: role recheck err: {e}")

    host_name, host_id = read_host_identity(session, log=log)
    host_name = str(host_name or "").strip()
    try:
        host_id_i = int(host_id or 0)
    except Exception:
        host_id_i = 0
    resolved: list[TeamMemberTarget] = []
    skipped_self: list[str] = []
    for t in targets or []:
        if isinstance(t, TeamMemberTarget):
            try:
                oid = int(t.obj_id)
            except Exception:
                oid = 0
            if t.obj_id is None or oid <= 0:
                continue
            nm = str(t.name or "").strip()
            if (host_id_i and oid == host_id_i) or (host_name and names_match(nm, host_name)):
                skipped_self.append(nm or f"id={oid}")
                continue
            resolved.append(t)
            continue
        if isinstance(t, dict):
            if t.get("obj_id") is None:
                continue
            try:
                oid = int(t.get("obj_id"))
            except Exception:
                continue
            if oid <= 0:
                continue
            nm = str(t.get("name") or t.get("token") or oid).strip()
            tok = str(t.get("token") or "").strip()
            if host_id_i and oid == host_id_i:
                skipped_self.append(nm or f"id={oid}")
                continue
            if host_name and (names_match(nm, host_name) or names_match(tok, host_name)):
                skipped_self.append(nm or host_name)
                continue
            # 不再用缓存 is_self 永久排除：换主号后原队长也要能被邀请
            resolved.append(
                TeamMemberTarget(
                    name=nm or str(oid),
                    obj_id=oid,
                    source=str(t.get("source") or "cache"),
                    pid=t.get("pid"),
                )
            )

    skipped_in: list[str] = []
    if skip_already_in_party:
        if party is None:
            party = list_party_members(
                session, store=store, exclude_pid=exclude_pid, log=log
            )
        resolved, skipped_in = filter_targets_not_in_party(resolved, party)

    if not resolved:
        parts = []
        if skipped_in:
            parts.append("已在队内跳过 " + "、".join(skipped_in))
        else:
            parts.append("无需要邀请的成员")
        if host_name:
            parts.append(f"队长={host_name}")
        msg = "；".join(parts)
        return TeamOpResult(
            ok=True,
            action="invite_members",
            message=msg,
            error=None,
            detail={
                "host_name": host_name,
                "invited": [],
                "failed": [],
                "skipped_in_party": skipped_in,
                "left": left,
                "party": party or [],
            },
        )

    invited: list[str] = []
    failed: list[str] = []
    # 优先走 UI 同款「按名 reason=1→0x125c」；纯 id 才走 0x1261。
    # 普通“全部邀请”保留两轮；自动整队会传 rounds=1，并按实到名册决定是否补邀。
    round_count = max(1, min(3, int(rounds or 1)))
    for round_i in range(1, round_count + 1):
        if round_i == 2:
            time.sleep(max(0.8, float(pause_s) * 2 if pause_s else 0.8))
            log(f"team: invite round 2 / {len(resolved)}")
        for tgt in resolved:
            nm = str(getattr(tgt, "name", "") or "").strip()
            oid = int(tgt.obj_id)
            # 二次兜底：绝不邀请自己（名或 id）
            if host_id_i and oid == host_id_i:
                if nm not in skipped_self and f"id={oid}" not in skipped_self:
                    skipped_self.append(nm or f"id={oid}")
                log(f"team: skip self invite id={oid} name={nm!r}")
                continue
            if host_name and names_match(nm, host_name):
                if nm not in skipped_self:
                    skipped_self.append(nm)
                log(f"team: skip self invite name={nm!r}")
                continue
            label = f"{nm}#{oid}" if nm else f"id={oid}"
            use_name = bool(nm) and (not _ID_RE.match(nm))
            if use_name:
                r = invite_player_by_name(session, nm, log=log)
            else:
                r = invite_player_by_id(session, oid, log=log)
            if r.ok:
                if label not in invited:
                    invited.append(label)
                if label in failed:
                    try:
                        failed.remove(label)
                    except ValueError:
                        pass
            else:
                if label not in invited and label not in failed:
                    failed.append(label)
            if pause_s > 0:
                time.sleep(float(pause_s))

    def _short(label: str) -> str:
        s = str(label or "").strip()
        if "#" in s:
            head = s.split("#", 1)[0].strip()
            if head and not head.lower().startswith("id="):
                return head
        return s

    short_invited = [_short(x) for x in invited]
    short_in = [_short(x) for x in skipped_in]
    short_failed = [_short(x) for x in failed]

    parts: list[str] = []
    if short_invited:
        parts.append("已邀请 " + "、".join(short_invited))
    if short_in:
        parts.append("已在队 " + "、".join(short_in))
    if short_failed:
        parts.append("失败 " + "、".join(short_failed))
    if left:
        parts.append("已先离队")
    msg = "；".join(parts) if parts else "无操作"
    log(
        f"team: invite_by_targets {msg} host={host_name!r} "
        f"invited={invited} skipped_self={skipped_self} "
        f"skipped_in={skipped_in} failed={failed}"
    )
    defaults_detail = None
    # 成为/保持队长后：允许队员邀请 + 自动招人 + 拾取优质
    if apply_defaults and (invited or skipped_in):
        try:
            time.sleep(0.2)
            dr = apply_captain_team_defaults(session, log=log)
            defaults_detail = dr.to_dict()
            if dr.ok:
                msg = f"{msg}；规则已开"
            elif dr.message:
                msg = f"{msg}；规则未生效"
        except Exception as e:
            log(f"team: defaults after invite err: {e}")
            defaults_detail = {"error": str(e)}
    # invite may create party / change membership; drop warm cache
    if invited or left:
        try:
            invalidate_team_states(session)
        except Exception:
            pass
    return TeamOpResult(
        ok=bool(invited) or bool(skipped_in),
        action="invite_members",
        message=msg,
        error=None if (invited or skipped_in) else "none_invited",
        detail={
            "host_name": host_name,
            "invited": invited,
            "failed": failed,
            "skipped_in_party": skipped_in,
            "skipped_self": skipped_self,
            "left": left,
            "targets": [asdict(t) for t in resolved],
            "party": party or [],
            "defaults": defaults_detail,
        },
    )


def invite_members_by_names(
    session,
    names: list[str] | str,
    *,
    store=None,
    exclude_pid: int | None = None,
    leave_if_teamed: bool = False,
    pause_s: float = 0.35,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Invite roster members by resolved player id.

    - Skip host's own name.
    - Optionally leave current team first (captain side).
    - Discovery: multi-box id → AOI name → literal id.

    @author by ak
    """
    log = log or (lambda _m: None)
    if leave_if_teamed:
        leave_team(session, log=log)
        time.sleep(0.25)

    host_name, _hid = read_host_identity(session, log=log)
    targets = resolve_member_targets(
        session,
        names,
        store=store,
        exclude_pid=exclude_pid,
        host_name=host_name,
        log=log,
    )
    if isinstance(names, str):
        name_list = parse_team_member_names(names)
    else:
        name_list = [str(n).strip() for n in (names or []) if str(n).strip()]
    wanted = [n for n in name_list if not names_match(n, host_name)]
    if not wanted:
        return TeamOpResult(
            ok=False,
            action="invite_members",
            message="无有效成员（名单为空或全是自己的角色名）",
            error="no_targets",
            detail={"host_name": host_name},
        )

    invited: list[str] = []
    failed: list[str] = []
    missing = [n for n in wanted if all(not names_match(n, t.name) for t in targets)]
    for t in targets:
        r = invite_player_by_id(session, int(t.obj_id), log=log)
        label = str(t.name or f"id={int(t.obj_id)}").strip()
        if r.ok:
            invited.append(label)
        else:
            failed.append(label)
        if pause_s > 0:
            time.sleep(float(pause_s))

    parts = []
    if invited:
        parts.append("已邀请 " + "、".join(invited))
    if missing:
        parts.append("未找到 " + "、".join(missing))
    if failed:
        parts.append("失败 " + "、".join(failed))
    msg = "；".join(parts) if parts else "无操作"
    log(f"team: invite_members {msg} host={host_name!r}")
    return TeamOpResult(
        ok=bool(invited),
        action="invite_members",
        message=msg,
        error=None if invited else "none_invited",
        detail={
            "host_name": host_name,
            "invited": invited,
            "missing": missing,
            "failed": failed,
            "targets": [asdict(t) for t in targets],
        },
    )


def prepare_member_leave_and_invite(
    session,
    names: list[str] | str,
    *,
    store=None,
    exclude_pid: int | None = None,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    Captain path: leave own stuck team if any, then invite roster.

    Peer leave is done via group-control TEAM_LEAVE on member windows.
    @author by ak
    """
    return invite_members_by_names(
        session,
        names,
        store=store,
        exclude_pid=exclude_pid,
        leave_if_teamed=True,
        log=log,
    )


def gather_after_fly(
    session,
    *,
    member_names: list[str] | str = (),
    targets: list[TeamMemberTarget] | list[dict] | None = None,
    store=None,
    exclude_pid: int | None = None,
    reinvite: bool = True,
    follow: bool = True,
    log: LogFn | None = None,
) -> TeamOpResult:
    """
    After flying to the same city: re-invite by id + start team follow.

    Prefer cached `targets` when provided (from 校验队伍).
    @author by ak
    """
    log = log or (lambda _m: None)
    detail: dict = {}
    if reinvite:
        if targets:
            inv = invite_members_by_targets(
                session,
                targets,
                leave_if_teamed=False,
                skip_already_in_party=True,
                store=store,
                exclude_pid=exclude_pid,
                log=log,
            )
        else:
            inv = invite_members_by_names(
                session,
                member_names,
                store=store,
                exclude_pid=exclude_pid,
                leave_if_teamed=False,
                log=log,
            )
        detail["invite"] = inv.to_dict()
        time.sleep(0.6)
    try:
        dr = apply_captain_team_defaults(session, log=log)
        detail["defaults"] = dr.to_dict()
    except Exception as e:
        log(f"team: gather defaults err: {e}")
        detail["defaults"] = {"error": str(e)}
    if follow:
        # Raw follow packet (fire-and-forget, no scene gate / UI click).
        fr = set_team_follow(session, enabled=True, log=log)
        ui_ok = bool(fr.ok)
        detail["follow"] = {
            "ok": ui_ok,
            "action": "team_follow_packet",
            "message": str(fr.message or ""),
        }
        return TeamOpResult(
            ok=ui_ok,
            action="gather",
            message=detail["follow"]["message"],
            error=None if ui_ok else "follow_failed",
            detail=detail,
        )
    return TeamOpResult(ok=True, action="gather", message="已处理邀请", detail=detail)


class TeamFormService:
    """
    Shared auto-team service usable by the 自动整队 page button and the
    ScheduleTaskRunner.

    - audit_party(): no-op check — ready when every target is already in the
      current live party (extra members allowed).
    - form(): full flow — group-control members leave → captain re-forms →
      batch invite → wait live roster confirms every target arrived → fly Fuzhou.

    Group-control publishes (slave leave / slave fly) are injected as callbacks
    by the page; without them only the captain-side ops run.

    @author by ak
    """

    def __init__(
        self,
        *,
        store=None,
        on_slave_leave: Callable[[str], None] | None = None,
        on_slave_fly: Callable[[str], None] | None = None,
        private_precheck_enabled: Callable[[], bool] | None = None,
        log: LogFn | None = None,
        status: Callable[[str], None] | None = None,
    ):
        self.store = store
        self.on_slave_leave = on_slave_leave or (lambda _members: None)
        self.on_slave_fly = on_slave_fly or (lambda _members: None)
        # 私聊预检查开关（None=默认启用）。生产接 team_control_flag_enabled：
        # 没开队内控（纯本机群控部署）时群控已能送达离队命令，跳过私聊省时。
        self._private_precheck_enabled = private_precheck_enabled or (lambda: True)
        self.log = log or (lambda _m: None)
        self.status = status or (lambda _m: None)

    def _target_label(self, t) -> str:
        if isinstance(t, TeamMemberTarget):
            return str(t.name or f"id={t.obj_id}")
        if isinstance(t, dict):
            return str(t.get("name") or t.get("token") or t.get("obj_id") or "?")
        return str(t or "?")

    @staticmethod
    def _target_rid(t) -> int:
        if isinstance(t, TeamMemberTarget):
            return int(t.obj_id or 0)
        if isinstance(t, dict):
            return int(t.get("obj_id") or 0)
        return 0

    def _private_precheck_leave(self, session, targets) -> None:
        """组队前私聊预检查：对名单逐个发"在队则离队"命令（PLEAVE）。

        生产策略（2026-08-29 定）：只管发，不等回执 —— 副控收到就执行离队，
        没收到、没离队都不阻断（后续邀请会兜底暴露未离队成员）。发送后固定
        等待 PRIVATE_LEAVE_SETTLE_S 作为离队缓冲。
        跨设备时群控（本机 IPC）不通、队内控依赖已组队 —— 私聊是组队前唯一
        控制面。
        @author by ak
        """
        pid = int(getattr(session, "pid", 0) or 0)
        roster = [
            {"name": self._target_label(t), "obj_id": self._target_rid(t)}
            for t in (targets or [])
            if self._target_rid(t)
        ]
        if not pid or not roster:
            return
        from app.core.private_team_link import (
            PRIVATE_LEAVE_SETTLE_S,
            notify_slaves_leave,
        )

        sent = notify_slaves_leave(pid, roster, log=self.log)
        self.status(f"自动整队：离队缓冲 {PRIVATE_LEAVE_SETTLE_S:.0f}s…")
        self.log(
            f"team: 私聊预检查 sent={sent}/{len(roster)}，"
            f"离队缓冲 {PRIVATE_LEAVE_SETTLE_S:.0f}s"
        )
        time.sleep(PRIVATE_LEAVE_SETTLE_S)

    def audit_party(
        self,
        session,
        targets: list[TeamMemberTarget] | list[dict],
        *,
        exclude_pid: int | None = None,
        fresh: bool = True,
    ) -> dict:
        """Return {ready, missing, party, targets} from the live roster. @author by ak"""
        party: list[dict] = []
        try:
            party = list_party_members(
                session,
                store=self.store,
                exclude_pid=exclude_pid,
                fresh=bool(fresh),
                log=self.log,
            )
        except Exception as e:
            self.log(f"team: audit_party list err: {e}")
        missing = [
            self._target_label(t)
            for t in (targets or [])
            if not target_already_in_party(t, party)
        ]
        return {
            "ready": not missing,
            "missing": missing,
            "party": party,
            "targets": list(targets or []),
        }

    def form(
        self,
        session,
        targets: list[TeamMemberTarget] | list[dict],
        *,
        members_text: str = "",
        exclude_pid: int | None = None,
        stop_event=None,
    ) -> TeamOpResult:
        """Full auto-team flow; no-op when the current party already conforms. @author by ak"""
        log = self.log
        members = str(members_text or "")

        audit = self.audit_party(session, targets, exclude_pid=exclude_pid, fresh=True)
        if audit["ready"]:
            msg = "队伍已包含全部名单成员，无需整队"
            log(f"team: auto_form noop {msg}")
            return TeamOpResult(
                ok=True,
                action="auto_form",
                message=msg,
                detail={"reformed": False, "missing": [], "party": audit["party"]},
            )

        if self._private_precheck_enabled():
            self.status("自动整队：私聊预检查（在队先离队）…")
            try:
                self._private_precheck_leave(session, targets)
            except Exception as e:
                log(f"team: form private precheck err: {e}")
        self.status("自动整队：全员离队…")
        try:
            self.on_slave_leave(members)
        except Exception as e:
            log(f"team: form slave leave publish err: {e}")
        # captain leaves too (twice: once before peers settle, once to be sure)
        try:
            leave_team(session, log=log)
        except Exception:
            pass
        time.sleep(1.2)
        try:
            leave_team(session, log=log)
        except Exception:
            pass
        time.sleep(1.0)

        self.status("自动整队：批量邀请…")
        invite_batches = [targets[i : i + 3] for i in range(0, len(targets), 3)]
        inv = None
        for batch_i, batch in enumerate(invite_batches, start=1):
            if batch_i > 1:
                log(
                    f"team: auto_form invite batch settle 3.0s "
                    f"before={batch_i}/{len(invite_batches)}"
                )
                time.sleep(3.0)
            inv = invite_members_by_targets(
                session,
                batch,
                leave_if_teamed=False,
                skip_already_in_party=True,
                store=self.store,
                exclude_pid=exclude_pid,
                pause_s=0.35,
                rounds=1,
                apply_defaults=batch_i == len(invite_batches),
                log=log,
            )
        # 按名邀请是异步链路：0x76 返回成功后，还要等 0x77 回包再发
        # 0x125c。首轮结束后先只观察，避免立即补邀撞进服务端限频窗口。
        self.status("自动整队：等待成员实到…")
        settle_deadline = time.monotonic() + 15.0
        while True:
            audit2 = self.audit_party(
                session, targets, exclude_pid=exclude_pid, fresh=True
            )
            if audit2["ready"]:
                break
            if stop_event is not None and stop_event.is_set():
                return TeamOpResult(
                    ok=False,
                    action="auto_form",
                    message="自动整队中断",
                    error="stopped",
                    detail={"missing": audit2["missing"], "party": audit2["party"]},
                )
            remaining = settle_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.75, remaining))

        audit3 = audit2
        if not audit2["ready"]:
            self.status("自动整队：补邀未实到成员…")
            for target_i, t in enumerate(targets):
                if target_already_in_party(t, audit2["party"]):
                    continue
                label = self._target_label(t)
                log(
                    f"team: auto_form re-invite {label} "
                    f"attempt=1 missing={audit2['missing']}"
                )
                try:
                    invite_members_by_targets(
                        session,
                        [t],
                        leave_if_teamed=False,
                        skip_already_in_party=True,
                        store=self.store,
                        exclude_pid=exclude_pid,
                        pause_s=0.0,
                        rounds=1,
                        apply_defaults=False,
                        log=log,
                    )
                except Exception as e:
                    log(f"team: auto_form re-invite err {label}: {e}")
                time.sleep(1.2)

            retry_deadline = time.monotonic() + 6.0
            while True:
                audit3 = self.audit_party(
                    session, targets, exclude_pid=exclude_pid, fresh=True
                )
                if audit3["ready"]:
                    break
                if stop_event is not None and stop_event.is_set():
                    return TeamOpResult(
                        ok=False,
                        action="auto_form",
                        message="自动整队中断",
                        error="stopped",
                        detail={
                            "missing": audit3["missing"],
                            "party": audit3["party"],
                        },
                    )
                remaining = retry_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
        if not audit3["ready"]:
            msg = "整队未完成 · 未入队：" + "、".join(audit3["missing"])
            log(f"team: auto_form blocked {msg}")
            return TeamOpResult(
                ok=False,
                action="auto_form",
                message=msg,
                error="missing_members",
                detail={
                    "missing": audit3["missing"],
                    "party": audit3["party"],
                    "invite": inv.to_dict() if hasattr(inv, "to_dict") else None,
                },
            )

        self.status("自动整队：全员已入队，稳定 2s…")
        time.sleep(2.0)
        self.status("自动整队：统一飞福州…")
        try:
            self.on_slave_fly(members)
        except Exception as e:
            log(f"team: form slave fly publish err: {e}")
        try:
            from app.core.map_fly import fly_page_slot_packet

            fly = fly_page_slot_packet(
                session,
                0xFF,
                0,
                label="福州城",
                wait_cooldown=False,
                respect_cooldown=True,
                stop_event=stop_event,
                log=log,
            )
        except Exception as e:
            fly = None
            log(f"team: auto_form fly err: {e}")
        if fly is not None:
            ok_fly = bool(getattr(fly, "ok", False))
            fly_detail = getattr(fly, "detail", None) or {}
            moved = bool(fly_detail.get("verified_move"))
            fly_s = "已飞福州" if ok_fly and moved else ("飞行已发" if ok_fly else "飞行失败")
        else:
            ok_fly, moved, fly_s = False, False, "飞行失败"
        msg = f"整队完成 · 全员{len(targets) + 1}人已入队 · {fly_s}"
        log(f"team: auto_form done {msg}")
        return TeamOpResult(
            ok=True,
            action="auto_form",
            message=msg,
            detail={
                "reformed": True,
                "missing": [],
                "party": audit3["party"],
                "invite": inv.to_dict() if hasattr(inv, "to_dict") else None,
                "fly": {"ok": ok_fly, "moved": moved, "message": fly_s},
            },
        )


# ===========================================================================
# 右侧缩略队伍面板右键动作 + 队伍集合（纯客户端本地，RE 2026-08-25 断点验证）
# ---------------------------------------------------------------------------
# 跟随     0x009A0120  thiscall(命令对象, u32)  — 右键队员头像 → Btn_Follow
# 选中同步 0x009A0390  thiscall(命令对象, u32)  — 右键队员头像 → Btn_TargetSame
# 队伍集合 Win_TeamMain 顶部 / Win_TeamFrame 面板 → Btn_Gather；
#          命令对象 this = [get_game_ui_dlg("Win_TeamMain") + 0x34]，
#          核心处理 0x00885300（当前被注入 xajh_chat_tap.dll hook 到 0x04181820）。
# 命令对象 vtable：
#   跟随/选中同步 = Win_QuickTeamLeader / Win_QuickTeamMember（vtable 0x128AB34）
#   队伍集合      = vtable 0x1269B9C
# ===========================================================================
NOTE_VA_QUICK_FOLLOW = 0x009A0120
NOTE_VA_QUICK_TARGET_SYNC = 0x009A0390
NOTE_VA_TEAM_GATHER_CORE = 0x00885300
QUICK_CMD_DLG_NAMES = ("Win_QuickTeamMember", "Win_QuickTeamLeader")
TEAM_MAIN_GATHER_THIS_OFF = 0x34


def _quick_cmd_this(session, *, log: LogFn | None = None) -> int:
    """Command object for follow / target-sync (right-click team menu).

    Both Win_QuickTeamLeader and Win_QuickTeamMember share vtable 0x128AB34
    and expose the same action methods.
    @author by ak
    """
    from app.core.plg_ui import get_game_ui_dlg

    for name in QUICK_CMD_DLG_NAMES:
        dlg = int(get_game_ui_dlg(session, name, log=log) or 0) & 0xFFFFFFFF
        if dlg:
            return dlg
    return 0


def _read_team_u32(session, addr: int) -> int:
    """Read one remote u32 through the session pymem handle. @author by ak"""
    pm = getattr(session, "pm", None)
    if pm is None or not addr:
        return 0
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(
            pm.process_handle, int(addr) & 0xFFFFFFFF, 4
        )
        return struct.unpack_from("<I", raw, 0)[0] if len(raw) >= 4 else 0
    except Exception:
        return 0


def _write_team_u32(session, addr: int, value: int) -> bool:
    """Write one remote u32 through the session pymem handle. @author by ak"""
    pm = getattr(session, "pm", None)
    if pm is None or not addr:
        return False
    try:
        import pymem.memory

        pymem.memory.write_bytes(
            pm.process_handle, int(addr) & 0xFFFFFFFF, struct.pack("<I", int(value) & 0xFFFFFFFF), 4
        )
        return True
    except Exception:
        return False


def _gather_cmd_this(session, *, log: LogFn | None = None) -> int:
    """队伍集合命令对象 = [Win_TeamMain + 0x34]. @author by ak"""
    from app.core.plg_ui import get_game_ui_dlg

    main = int(get_game_ui_dlg(session, "Win_TeamMain", log=log) or 0) & 0xFFFFFFFF
    if not main:
        return 0
    return _read_team_u32(session, main + TEAM_MAIN_GATHER_THIS_OFF)


def quick_team_follow(
    session,
    *,
    target_id: int = 0,
    log: LogFn | None = None,
) -> TeamOpResult:
    """指定跟随（右键队员头像 → Btn_Follow）。纯客户端本地，不发网络包。

    ⚠️ 区分：这是「指定跟随」——跟随当前选中的目标（右键菜单 Btn_Follow）。
        与 Win_TeamMain 顶部的「组队跟随」（set_team_follow / CEC8A0，会发包
        c2s 0x1286）是**两个不同的功能**，不要混用。

    - target_id>0 时先 plg::SetTarget 选中该目标，再执行跟随（跟随当前选中）。
    - target_id=0 时直接跟随当前选中目标。
    命令对象按目标类型取 Win_QuickTeamMember（普通队员）或
    Win_QuickTeamLeader（队长）；两者共用 vtable 0x128AB34，并保留互相回退。
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False, action="quick_follow",
            message=f"远程不可用: {brsn}", error=brsn or "remote_blocked",
        )
    pid = int(session.pid)
    this = _quick_cmd_this(session, log=log)
    if not this:
        return TeamOpResult(
            ok=False, action="quick_follow",
            message="找不到右键菜单命令对象 (Win_QuickTeamMember/Leader)",
            error="no_cmd_obj",
        )
    try:
        oid = int(target_id or 0)
        if oid > 0:
            oid = int(oid) & 0xFFFFFFFFFFFFFFFF
            low = oid & 0xFFFFFFFF
            high = (oid >> 32) & 0xFFFFFFFF
            from app.core.plg_exports import EXPORT_SET_TARGET, resolve_export_rva

            pe = getattr(session, "exe_path", None)
            rva = resolve_export_rva(str(pe), EXPORT_SET_TARGET) if pe else None
            base = int(getattr(session, "module_base", 0) or 0)
            if rva and base:
                sret = remote_call_cdecl_x86(
                    pid,
                    base + int(rva),
                    [low, high],
                    timeout_ms=4000,
                    skip_scene_gate=True,
                )
                log(f"team: quick_follow SetTarget id={oid} ret={sret}")
                if not sret:
                    time.sleep(0.15)
            else:
                log("team: quick_follow SetTarget va unresolved, skip preset")
        else:
            # target_id=0：跟随 command 对象当前记录的目标（真实右键/上次跟随写入的）
            oid = (
                _read_team_u32(session, this + 0x174) << 32
            ) | _read_team_u32(session, this + 0x170)
        # 关键：0x009A0120 从 Win_QuickTeamMember +0x170/+0x174 读跟随目标，
        # SetTarget 不写这两个字段（真实右键时游戏写入）。必须手动写入目标 id。
        if oid > 0:
            _write_team_u32(session, this + 0x170, oid & 0xFFFFFFFF)
            _write_team_u32(session, this + 0x174, (oid >> 32) & 0xFFFFFFFF)
            log(
                f"team: quick_follow cmd target 0x{oid:X} "
                f"-> +0x170=0x{oid & 0xFFFFFFFF:X} +0x174=0x{(oid >> 32) & 0xFFFFFFFF:X}"
            )
        va = _note_va(session, NOTE_VA_QUICK_FOLLOW)
        ret = remote_call_thiscall_x86(
            pid, va, this, [0], timeout_ms=4000, skip_scene_gate=True,
        )
        msg = (
            f"quick_follow this=0x{this:X} target={int(target_id or 0)} "
            f"ret={ret}"
        )
        log(f"team: {msg}")
        # 命令对象方法返回值来自收尾 vtable 调用，不代表动作成败；执行未抛异常即视为已触发。
        return TeamOpResult(
            ok=True,
            action="quick_follow",
            message="已跟随当前选中目标（客户端本地动作已触发）",
            detail={
                "this": this,
                "target_id": int(target_id or 0),
                "ret": int(ret or 0),
                "path": "quick_follow_0x9A0120",
                "client_local": True,
            },
        )
    except Exception as e:
        log(f"team: quick_follow err: {e}")
        return TeamOpResult(
            ok=False, action="quick_follow", message=str(e), error=str(e)
        )


def quick_team_target_sync(
    session,
    *,
    target_id: int = 0,
    log: LogFn | None = None,
) -> TeamOpResult:
    """选中同步（右键队员头像 → Btn_TargetSame）。纯客户端本地。

    把自己的当前选中切换为目标当前选中的目标。
    target_id>0 时先 SetTarget 选中该目标队员（模拟右键点中），再执行同步；
    target_id=0 时直接同步「当前选中目标」当前选中的目标。
    命令对象取 Win_QuickTeamLeader / Win_QuickTeamMember。
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False, action="quick_target_sync",
            message=f"远程不可用: {brsn}", error=brsn or "remote_blocked",
        )
    pid = int(session.pid)
    this = _quick_cmd_this(session, log=log)
    if not this:
        return TeamOpResult(
            ok=False, action="quick_target_sync",
            message="找不到右键菜单命令对象 (Win_QuickTeamMember/Leader)",
            error="no_cmd_obj",
        )
    try:
        oid = int(target_id or 0)
        if oid > 0:
            oid = int(oid) & 0xFFFFFFFFFFFFFFFF
            low = oid & 0xFFFFFFFF
            high = (oid >> 32) & 0xFFFFFFFF
            from app.core.plg_exports import EXPORT_SET_TARGET, resolve_export_rva

            pe = getattr(session, "exe_path", None)
            rva = resolve_export_rva(str(pe), EXPORT_SET_TARGET) if pe else None
            base = int(getattr(session, "module_base", 0) or 0)
            if rva and base:
                sret = remote_call_cdecl_x86(
                    pid,
                    base + int(rva),
                    [low, high],
                    timeout_ms=4000,
                    skip_scene_gate=True,
                )
                log(f"team: quick_target_sync SetTarget id={oid} ret={sret}")
                if not sret:
                    time.sleep(0.15)
            else:
                log("team: quick_target_sync SetTarget va unresolved, skip preset")
        else:
            # target_id=0：同步 command 对象当前记录的目标（真实右键/上次操作写入的）
            oid = (
                _read_team_u32(session, this + 0x174) << 32
            ) | _read_team_u32(session, this + 0x170)
        # 关键：0x009A0390 从 Win_QuickTeamMember +0x170/+0x174 读同步目标，
        # SetTarget 不写这两个字段（真实右键时游戏写入）。必须手动写入目标 id。
        if oid > 0:
            _write_team_u32(session, this + 0x170, oid & 0xFFFFFFFF)
            _write_team_u32(session, this + 0x174, (oid >> 32) & 0xFFFFFFFF)
            log(
                f"team: quick_target_sync cmd target 0x{oid:X} "
                f"-> +0x170=0x{oid & 0xFFFFFFFF:X} +0x174=0x{(oid >> 32) & 0xFFFFFFFF:X}"
            )
        va = _note_va(session, NOTE_VA_QUICK_TARGET_SYNC)
        ret = remote_call_thiscall_x86(
            pid, va, this, [0], timeout_ms=4000, skip_scene_gate=True,
        )
        msg = f"quick_target_sync this=0x{this:X} ret={ret}"
        log(f"team: {msg}")
        # 返回值为收尾调用结果，不代表同步成败；执行未抛异常即视为已触发。
        return TeamOpResult(
            ok=True,
            action="quick_target_sync",
            message="已执行选中同步（客户端本地动作已触发）",
            detail={
                "this": this,
                "target_id": int(target_id or 0),
                "ret": int(ret or 0),
                "path": "quick_target_sync_0x9A0390",
                "client_local": True,
            },
        )
    except Exception as e:
        log(f"team: quick_target_sync err: {e}")
        return TeamOpResult(
            ok=False, action="quick_target_sync", message=str(e), error=str(e)
        )


def quick_team_gather(
    session,
    *,
    log: LogFn | None = None,
) -> TeamOpResult:
    """队伍集合（Win_TeamMain 顶部「队伍集合」/ Win_TeamFrame「集合」）。队长独有。

    命令对象 = [Win_TeamMain + 0x34]，通过其核心入口 0x00885300 执行
    （该入口当前被注入 xajh_chat_tap.dll hook，实际效果以游戏为准）。
    调用约定 thiscall(this=命令对象, 目标串指针, 0x03, -1, -1, 0*6)。
    @author by ak
    """
    log = log or (lambda _m: None)
    blocked, brsn = _pid_blocked(session)
    if blocked:
        return TeamOpResult(
            ok=False, action="quick_gather",
            message=f"远程不可用: {brsn}", error=brsn or "remote_blocked",
        )
    pid = int(session.pid)
    this = _gather_cmd_this(session, log=log)
    if not this:
        return TeamOpResult(
            ok=False, action="quick_gather",
            message="找不到队伍集合命令对象 ([Win_TeamMain+0x34])",
            error="no_cmd_obj",
        )
    try:
        va = _note_va(session, NOTE_VA_TEAM_GATHER_CORE)
        # 命令处理调用：thiscall(this, name_ptr, 0x03, -1, -1, 0,0,0,0,0,0)
        # name 传空字符串指针即可（集合不依赖目标名）。
        handle = 0
        remote = 0
        try:
            from app.core.plg_ui import remote_alloc_bytes, remote_free

            handle, remote, _sz = remote_alloc_bytes(pid, b"\x00")
            args = [
                int(remote) & 0xFFFFFFFF,
                0x03,
                0xFFFFFFFF,
                0xFFFFFFFF,
                0, 0, 0, 0, 0, 0,
            ]
            ret = remote_call_thiscall_x86(
                pid, va, this, args, timeout_ms=5000, skip_scene_gate=True,
            )
        finally:
            if handle and remote:
                try:
                    from app.core.plg_ui import remote_free

                    remote_free(handle, remote)
                except Exception:
                    pass
        msg = f"quick_gather this=0x{this:X} ret={ret}"
        log(f"team: {msg}")
        # 返回值为收尾调用结果，不代表集合成败；执行未抛异常即视为已触发。
        return TeamOpResult(
            ok=True,
            action="quick_gather",
            message="已发起队伍集合（客户端本地动作已触发）",
            detail={
                "this": this,
                "ret": int(ret or 0),
                "path": "quick_gather_0x885300",
                "core_hooked": True,
                "client_local": True,
            },
        )
    except Exception as e:
        log(f"team: quick_gather err: {e}")
        return TeamOpResult(
            ok=False, action="quick_gather", message=str(e), error=str(e)
        )



