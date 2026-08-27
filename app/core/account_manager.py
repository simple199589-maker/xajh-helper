# -*- coding: utf-8 -*-
"""
账号/角色管理：配置按 role_id 分目录、本机登录器路径记忆、批量结束游戏。

存储布局（runtime/config 可写根下）：
  global/game_paths.json        本机登录器/客户端路径（人工选一次，重启保留）
  accounts/{account_id}.json    账号密码（本机加密）+ 三角色槽
  roles/{role_id}/meta.json     角色元数据：名称/所属账号（名称仅展示）
  roles/{role_id}/hang.json     挂机预设（hang_settings 读写）
  roles/{role_id}/control.json  本机主副控（none/master/slave）
  roles/{role_id}/inject.json   是否注入（v1 仅存预设，暂不强制注入门控）

约定：
- 角色主键为数字 role_id；角色名可变，改名只更新 meta。
- v1 登录 = 启动登录器/客户端 + 配置/注入链路；自动填账密/选角为后续阶段。
- 删除账号/角色配置不结束游戏进程；批量杀进程只针对 xajh.exe（调用方确认）。
- 密码使用 Windows DPAPI 本机加密，DPAPI 不可用时降级为 XOR 混淆。

@author by ak
"""
from __future__ import annotations

import base64
import ctypes
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

LogFn = Callable[[str], None]

_DPAPI_ENTROPY = b"xajh-helper-account-v1"
_DPAPI_FLAGS = 0x01  # CRYPTPROTECT_LOCAL_MACHINE
_XOR_SEED = 0x5A
_ENC_DPAPI = "dpapi:"
_ENC_XOR = "xor:"

ACCOUNT_SLOT_COUNT = 3
# 特殊槽位值（历史数据）：勾选代表该账号登录只到选角页面（不自动选角/注入该槽）。
SPECIAL_ROLE_CHAR_SELECT = "-1"
# 槽位「角色序号」：0=未启用(停留在选角页)，1/2/3=角色1/2/3。
ROLE_INDEX_NONE = 0
ROLE_INDEX_CHOICES = (ROLE_INDEX_NONE, 1, 2, 3)
ROLE_INDEX_LABELS = {
    ROLE_INDEX_NONE: "未启用",
    1: "角色1",
    2: "角色2",
    3: "角色3",
}
ROLE_INDEX_ACTIVE = (1, 2, 3)
CONTROL_ROLE_NONE = "none"
CONTROL_ROLE_MASTER = "master"
CONTROL_ROLE_SLAVE = "slave"
CONTROL_ROLE_LABELS = {
    CONTROL_ROLE_NONE: "无控",
    CONTROL_ROLE_MASTER: "主控",
    CONTROL_ROLE_SLAVE: "副控",
}
CONTROL_ROLE_CHOICES = (CONTROL_ROLE_NONE, CONTROL_ROLE_MASTER, CONTROL_ROLE_SLAVE)

PROCESS_IMAGE_XAJH = "xajh.exe"


def config_root() -> Path:
    """Writable runtime config root (runtime/config). @author by ak"""
    from common.paths import ensure_writable_dir

    return ensure_writable_dir("runtime", "config")


def roles_root() -> Path:
    """Per-role config directory root. @author by ak"""
    return config_root() / "roles"


def accounts_root() -> Path:
    """Per-account config directory. @author by ak"""
    return config_root() / "accounts"


def global_dir() -> Path:
    """Machine-global config directory (paths / defaults). @author by ak"""
    return config_root() / "global"


def role_dir(role_id: int | str) -> Path:
    """Directory for one role's config. @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        raise ValueError(f"invalid role_id: {role_id!r}")
    return roles_root() / str(rid)


def account_path(account_id: str) -> Path:
    """Path for one account file. @author by ak"""
    return accounts_root() / f"{str(account_id or '').strip()}.json"


def normalize_role_id(role_id: int | str | None) -> str:
    """Normalize numeric role id (digits only); '' when invalid. @author by ak"""
    if role_id is None or isinstance(role_id, bool):
        return ""
    if isinstance(role_id, int):
        n = int(role_id)
        return str(n) if n > 0 else ""
    s = str(role_id).strip()
    if not s:
        return ""
    if s.isdigit():
        n = int(s)
        return str(n) if n > 0 else ""
    try:
        n = int(float(s))
        return str(n) if n > 0 else ""
    except Exception:
        return ""


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        if not path.is_file():
            return fallback
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else fallback
    except Exception:
        return fallback


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


# ---------------------------------------------------------------- password
def _dpapi_encrypt(plain: bytes) -> bytes | None:
    """DPAPI CryptProtectData (LOCAL_MACHINE). @author by ak"""
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        plain_buf = ctypes.create_string_buffer(plain)
        ent_buf = ctypes.create_string_buffer(_DPAPI_ENTROPY)
        in_blob = DATA_BLOB(
            len(plain), ctypes.cast(plain_buf, ctypes.POINTER(ctypes.c_ubyte))
        )
        ent_blob = DATA_BLOB(
            len(_DPAPI_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_ubyte))
        )
        out_blob = DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "xajh_account",
            ctypes.byref(ent_blob),
            None,
            None,
            _DPAPI_FLAGS,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return bytes(ctypes.string_at(out_blob.pbData, out_blob.cbData))
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _dpapi_decrypt(blob: bytes) -> bytes | None:
    """DPAPI CryptUnprotectData (LOCAL_MACHINE). @author by ak"""
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        ent_buf = ctypes.create_string_buffer(_DPAPI_ENTROPY)
        in_blob = DATA_BLOB(
            len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_ubyte))
        )
        ent_blob = DATA_BLOB(
            len(_DPAPI_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_ubyte))
        )
        out_blob = DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(ent_blob),
            None,
            None,
            _DPAPI_FLAGS,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return bytes(ctypes.string_at(out_blob.pbData, out_blob.cbData))
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _xor_transform(data: bytes) -> bytes:
    """Reversible XOR obfuscation (not encryption). @author by ak"""
    return bytes((b ^ ((_XOR_SEED + i * 13) & 0xFF)) for i, b in enumerate(data))


def encrypt_password(plain: str) -> str:
    """Encrypt/obfuscate a password for local disk storage. @author by ak"""
    data = str(plain or "").encode("utf-8")
    if not data:
        return ""
    enc = _dpapi_encrypt(data)
    if enc is not None:
        return _ENC_DPAPI + base64.b64encode(enc).decode("ascii")
    return _ENC_XOR + base64.b64encode(_xor_transform(data)).decode("ascii")


def decrypt_password(blob: str) -> str:
    """Decrypt a stored password; legacy plain text passes through. @author by ak"""
    s = str(blob or "").strip()
    if not s:
        return ""
    try:
        if s.startswith(_ENC_DPAPI):
            raw = base64.b64decode(s[len(_ENC_DPAPI):])
            dec = _dpapi_decrypt(raw)
            if dec is not None:
                return dec.decode("utf-8")
            return ""
        if s.startswith(_ENC_XOR):
            raw = base64.b64decode(s[len(_ENC_XOR):])
            return _xor_transform(raw).decode("utf-8")
    except Exception:
        return ""
    return s


# ---------------------------------------------------------------- accounts
def list_accounts() -> list[dict]:
    """List account records with password masked. @author by ak"""
    root = accounts_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for f in sorted(root.glob("*.json")):
        data = _read_json(f, None)
        if not isinstance(data, dict):
            continue
        account_id = str(f.stem or "").strip()
        slots = list(data.get("slots") or []) if isinstance(data.get("slots"), list) else []
        pw_len = len(decrypt_password(str(data.get("password") or "")))
        masked = "•" * max(4, min(pw_len, 12)) if pw_len else ""
        out.append(
            {
                "account_id": account_id,
                "password_masked": masked,
                "inject": _account_inject_from(data),
                "control": str(data.get("control") or CONTROL_ROLE_NONE),
                "dungeon_hang": bool(data.get("dungeon_hang", False)),
                "hang_enabled": bool(data.get("hang_enabled", False)),
                "team_control": bool(data.get("team_control", False)),
                "slots": _normalize_slots(slots),
            }
        )
    out.sort(key=lambda a: str(a.get("account_id") or "").lower())
    return out


def _account_inject_from(data: dict) -> bool:
    """Account-level 注入脚本 flag; legacy per-slot inject migrated (all-off -> off). @author by ak"""
    if "inject" in data:
        return bool(data.get("inject", True))
    slots = data.get("slots")
    if isinstance(slots, list) and slots:
        explicit = [
            bool(s.get("inject", True))
            for s in slots
            if isinstance(s, dict) and "inject" in s
        ]
        if explicit and not any(explicit):
            return False
    return True


def get_account(account_id: str) -> dict | None:
    """Return account record with decrypted password. @author by ak"""
    account_id = str(account_id or "").strip()
    if not account_id:
        return None
    path = account_path(account_id)
    data = _read_json(path, None)
    if not isinstance(data, dict):
        return None
    slots = list(data.get("slots") or []) if isinstance(data.get("slots"), list) else []
    return {
        "account_id": account_id,
        "password": decrypt_password(str(data.get("password") or "")),
        "password_enc": str(data.get("password") or ""),
        "inject": _account_inject_from(data),
        "control": str(data.get("control") or CONTROL_ROLE_NONE),
        "dungeon_hang": bool(data.get("dungeon_hang", False)),
        "hang_enabled": bool(data.get("hang_enabled", False)),
        "team_control": bool(data.get("team_control", False)),
        "slots": _normalize_slots(slots),
    }


def save_account(
    account_id: str,
    *,
    password: str = "",
    slots: list[dict] | None = None,
    inject: bool | None = None,
    control: str | None = None,
    dungeon_hang: bool | None = None,
    hang_enabled: bool | None = None,
    team_control: bool | None = None,
) -> dict:
    """Create/update one account. Empty account_id raises ValueError. @author by ak"""
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("account_id empty")
    old = get_account(account_id)
    rec: dict = {
        "account_id": account_id,
        "password": old["password_enc"] if old is not None else "",
        "slots": old["slots"] if old is not None else _empty_slots(),
        "inject": old["inject"] if old is not None else False,
        "control": old["control"] if old is not None else CONTROL_ROLE_NONE,
        "dungeon_hang": old["dungeon_hang"] if old is not None else False,
        "hang_enabled": old["hang_enabled"] if old is not None else False,
        "team_control": old["team_control"] if old is not None else False,
    }
    # keep existing encrypted blob when password not provided on edit
    if password is not None and (str(password or "").strip() or old is None):
        rec["password"] = encrypt_password(str(password or ""))
    if inject is not None:
        rec["inject"] = bool(inject)
    if control is not None:
        c = str(control or CONTROL_ROLE_NONE).strip().lower()
        rec["control"] = c if c in CONTROL_ROLE_CHOICES else CONTROL_ROLE_NONE
    if dungeon_hang is not None:
        rec["dungeon_hang"] = bool(dungeon_hang)
    if hang_enabled is not None:
        rec["hang_enabled"] = bool(hang_enabled)
    if team_control is not None:
        rec["team_control"] = bool(team_control)
    if slots is not None:
        rec["slots"] = _normalize_slots(slots)
    rec["updated_at"] = _now_iso()
    _write_json(account_path(account_id), rec)
    return rec


def delete_account(account_id: str) -> bool:
    """Delete account config only; never touches game processes. @author by ak"""
    account_id = str(account_id or "").strip()
    if not account_id:
        return False
    path = account_path(account_id)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except Exception:
        return False
    return True


def account_slots(account_id: str) -> list[dict]:
    """Return normalized 3-slot list for an account. @author by ak"""
    acc = get_account(account_id)
    return acc["slots"] if acc is not None else _empty_slots()


def find_account_by_role_id(role_id: int | str) -> str:
    """First account whose slot references role_id; '' when none. @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return ""
    for acc in list_accounts():
        for slot in acc.get("slots") or []:
            if normalize_role_id(slot.get("role_id")) == rid:
                return str(acc.get("account_id") or "")
    return ""


def find_account_by_role_name(name: str) -> str:
    """First account whose slot (or role meta) name matches; '' when none. @author by ak"""
    name = str(name or "").strip()
    if not name:
        return ""
    for acc in list_accounts():
        for slot in acc.get("slots") or []:
            if str(slot.get("name") or "").strip() == name:
                return str(acc.get("account_id") or "")
    for r in list_roles():
        if str(r.get("name") or "").strip() == name:
            acc = str(r.get("account_id") or "").strip()
            if acc:
                return acc
            owner = find_account_by_role_id(r.get("role_id"))
            if owner:
                return owner
    return ""


def active_role_id_for_account(account_id: str) -> str:
    """当前在线/进世界角色 role_id（账号归属反查），无则 ''.

    通过运行时 pid 标题角色名归属到该账号，再映射回该账号槽位 / 角色 meta 的
    role_id。未注入（还没 Delete）也能据此识别。@author by ak
    """
    account_id = str(account_id or "").strip()
    if not account_id:
        return ""
    # 标题角色名 → 该账号绑定的 role_id。
    name_to_rid: dict[str, str] = {}
    for s in account_slots(account_id):
        nm = str(s.get("name") or "").strip()
        rid = normalize_role_id(s.get("role_id"))
        if nm and rid:
            name_to_rid[nm] = rid
        if rid:
            meta_name = str(load_role_meta(rid).get("name") or "").strip()
            if meta_name:
                name_to_rid[meta_name] = rid
    if not name_to_rid:
        return ""
    try:
        from app.core.inject_gate import find_main_hwnd_for_pid, find_xajh_processes
        from app.core.window_title import parse_role_name_from_title

        for p in find_xajh_processes() or []:
            pid = int(p.get("pid") or 0)
            if not pid:
                continue
            try:
                hwnd, title, _cls = find_main_hwnd_for_pid(pid)
            except Exception:
                continue
            if not hwnd:
                continue
            name = parse_role_name_from_title(title or "")
            if name and name in name_to_rid:
                return name_to_rid[name]
    except Exception:
        pass
    return ""


def scan_running_role_names() -> list[str]:
    """Detect in-world role names from running (possibly non-injected) xajh.exe.

    Uses the game window title (fallback source) so accounts that are logged
    in but not yet injected can still be matched by role name.

    @author by ak
    """
    out: list[str] = []
    try:
        from app.core.inject_gate import find_main_hwnd_for_pid, find_xajh_processes
        from app.core.window_title import parse_role_name_from_title

        for p in find_xajh_processes() or []:
            pid = int(p.get("pid") or 0)
            if not pid:
                continue
            try:
                hwnd, title, _cls = find_main_hwnd_for_pid(pid)
            except Exception:
                continue
            if not hwnd:
                continue
            name = parse_role_name_from_title(title or "")
            if name:
                out.append(name)
    except Exception:
        pass
    return out


def save_account_runtime_pid(account_id: str, pid: int) -> None:
    """Record the game pid the program launched for this account (登录成功后).

    选角页/世界内窗口标题在选角页阶段不含角色名，标题匹配找不到 pid；此处
    记录登录编排返回的 pid，供「轮询测试选角」等按账号定位游戏窗口使用。
    @author by ak
    """
    account_id = str(account_id or "").strip()
    pid = int(pid or 0)
    if not account_id or pid <= 0:
        return
    data = _read_json(global_dir() / "account_runtime_pids.json", {})
    if not isinstance(data, dict):
        data = {}
    data[account_id] = pid
    _write_json(global_dir() / "account_runtime_pids.json", data)


def load_account_runtime_pid(account_id: str) -> int:
    """Read the recorded login pid for an account; 0 when none. @author by ak"""
    account_id = str(account_id or "").strip()
    if not account_id:
        return 0
    data = _read_json(global_dir() / "account_runtime_pids.json", {})
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get(account_id) or 0)
    except Exception:
        return 0


def clear_account_runtime_pid(account_id: str) -> None:
    """Drop the recorded login pid for an account (停止/关闭时). @author by ak"""
    account_id = str(account_id or "").strip()
    if not account_id:
        return
    try:
        path = global_dir() / "account_runtime_pids.json"
        data = _read_json(path, {})
        if isinstance(data, dict) and account_id in data:
            data.pop(account_id, None)
            _write_json(path, data)
    except Exception:
        pass


def running_pids_for_account(account_id: str) -> list[int]:
    """Return running xajh.exe pids that belong to this account.

    Fully dynamic, matched by ACCOUNT OWNERSHIP: scans live game processes and
    resolves the in-world role name in each window title to its owning account
    (find_account_by_role_name — slot names first, then roles meta). A pid
    belongs to ``account_id`` only when that reverse lookup lands on it, so the
    account does NOT need its slot name pre-written: a role already recorded
    under this account (or any of its slots) is enough.

    Returns de-duplicated pids; unmatched -> [].

    @author by ak
    """
    account_id = str(account_id or "").strip()
    if not account_id:
        return []
    found: list[int] = []
    seen: set[int] = set()

    def _add(pid: int) -> None:
        pid = int(pid or 0)
        if pid > 0 and pid not in seen:
            seen.add(pid)
            found.append(pid)

    # 1) 登录时记录的 pid：选角页标题无角色名，标题匹配不可用，此路径最可靠。
    try:
        rpid = load_account_runtime_pid(account_id)
        if rpid > 0:
            from app.core.inject_gate import find_main_hwnd_for_pid

            hwnd, _t, _c = find_main_hwnd_for_pid(rpid) or (0, "", "")
            if hwnd:
                _add(rpid)
            else:
                clear_account_runtime_pid(account_id)
    except Exception:
        pass

    slots = account_slots(account_id)
    # 本账号直接绑定的角色名（快速精确路径，避免跨账号同名歧义）。
    slot_names: set[str] = set()
    for s in slots:
        if s.get("role_index") not in ROLE_INDEX_ACTIVE:
            continue
        nm = str(s.get("name") or "").strip()
        if nm:
            slot_names.add(nm)
        rid = normalize_role_id(s.get("role_id"))
        if rid:
            meta_name = str(load_role_meta(rid).get("name") or "").strip()
            if meta_name:
                slot_names.add(meta_name)
    try:
        from app.core.inject_gate import find_main_hwnd_for_pid, find_xajh_processes
        from app.core.window_title import parse_role_name_from_title

        for p in find_xajh_processes() or []:
            pid = int(p.get("pid") or 0)
            if not pid or pid in seen:
                continue
            try:
                hwnd, title, _cls = find_main_hwnd_for_pid(pid)
            except Exception:
                continue
            if not hwnd:
                continue
            name = parse_role_name_from_title(title or "")
            if not name:
                continue
            if name in slot_names:
                _add(pid)
                continue
            # 按账号归属反查：标题角色名归属到本账号即命中。
            try:
                owner = find_account_by_role_name(name)
            except Exception:
                owner = ""
            if owner == account_id:
                _add(pid)
    except Exception:
        pass
    return found


def apply_char_select_roles(account_id: str, roles: list[dict]) -> None:
    """Write char-select role cards into the account's slots (角色下拉可见).

    ``roles`` is the list read from the char-select page:
    [{slot, name, level, role_id}, ...]. Each card is mapped to the matching
    slot (slot 1/2/3 -> role_index 1/2/3) and merged into the account config;
    the role meta name/owner is also written.

    If the account currently has NO enabled slot (all role_index == 0, i.e. the
    user chose 未启用), the role_index is kept 0 — the selected role names/ids
    are still recorded (for the dropdown / PID matching) but auto char-select
    stays off. Enabled slots are written normally.

    @author by ak
    """
    account_id = str(account_id or "").strip()
    if not account_id or not roles:
        return
    slots = account_slots(account_id)
    # 「未启用」= 账号已绑定过真实角色（有 role_id）但 role_index 全为 0（用户手动选
    # 未启用）；此时保留 role_index=0，不覆盖。全空槽位（无任何 role_id）视为新账号，
    # 正常写回选角角色。
    has_bound = any(str(s.get("role_id") or "").strip() for s in slots)
    all_disabled = has_bound and all(
        int(s.get("role_index") or ROLE_INDEX_NONE) not in ROLE_INDEX_ACTIVE
        for s in slots
    )
    changed = False
    for r in roles:
        try:
            slot = int(r.get("slot") or 0)
        except Exception:
            slot = 0
        if slot < 1 or slot > ACCOUNT_SLOT_COUNT:
            continue
        rid = normalize_role_id(r.get("role_id"))
        name = str(r.get("name") or "").strip()
        idx = slot - 1
        if all_disabled:
            # 未启用：保留 role_index=0，仅记录角色名/ID 与 meta，不覆盖设置。
            slots[idx]["role_index"] = ROLE_INDEX_NONE
        else:
            slots[idx]["role_index"] = slot
        slots[idx]["role_id"] = rid if rid else ""
        slots[idx]["name"] = name
        if rid:
            save_role_meta(rid, name=name, account_id=account_id)
        changed = True
    if changed:
        try:
            save_account(account_id, slots=slots)
        except Exception:
            pass


def _empty_slots() -> list[dict]:
    return [
        {"role_index": ROLE_INDEX_NONE, "role_id": "", "name": ""},
        {"role_index": ROLE_INDEX_NONE, "role_id": "", "name": ""},
        {"role_index": ROLE_INDEX_NONE, "role_id": "", "name": ""},
    ]


def _normalize_slots(slots: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for i in range(ACCOUNT_SLOT_COUNT):
        raw = slots[i] if isinstance(slots, list) and i < len(slots) else {}
        raw = raw if isinstance(raw, dict) else {}
        role_id = str(raw.get("role_id") or "").strip()
        name = str(raw.get("name") or "").strip()
        if "role_index" in raw:
            try:
                ri = int(raw.get("role_index"))
            except Exception:
                ri = ROLE_INDEX_NONE
        else:
            # legacy {enabled, role_id} -> derive role_index
            enabled = bool(raw.get("enabled", bool(role_id or name)))
            if role_id == SPECIAL_ROLE_CHAR_SELECT:
                # legacy 停留(-1) -> 未启用（停留在选角页）
                ri = ROLE_INDEX_NONE
            elif enabled:
                ri = 1
            else:
                ri = ROLE_INDEX_NONE
        if ri not in ROLE_INDEX_CHOICES:
            ri = ROLE_INDEX_NONE
        out.append(
            {
                "role_index": int(ri),
                "role_id": role_id,
                "name": name,
            }
        )
    return out


# ---------------------------------------------------------------- roles
def load_role_meta(role_id: int | str) -> dict:
    """Read roles/{role_id}/meta.json. @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    return dict(_read_json(role_dir(rid) / "meta.json", {}))


def save_role_meta(
    role_id: int | str, *, name: str = "", account_id: str = ""
) -> dict:
    """Merge-write role meta.json (name is display-only). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    meta = load_role_meta(rid)
    if name is not None and str(name).strip():
        meta["name"] = str(name).strip()
    if account_id is not None:
        meta["account_id"] = str(account_id or "").strip()
    meta["role_id"] = rid
    _write_json(role_dir(rid) / "meta.json", meta)
    return meta


def load_role_hang(role_id: int | str) -> dict:
    """Read roles/{role_id}/hang.json (raw prefs). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    return dict(_read_json(role_dir(rid) / "hang.json", {}))


def save_role_hang(role_id: int | str, prefs: dict) -> dict:
    """Merge-write roles/{role_id}/hang.json. @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    cur = load_role_hang(rid)
    if isinstance(prefs, dict):
        cur.update(prefs)
    path = role_dir(rid) / "hang.json"
    _write_json(path, cur)
    written = _read_json(path, {})
    if not path.is_file() or not isinstance(written, dict):
        raise OSError(f"挂机配置写入后文件不存在: {path}")
    for key, value in cur.items():
        if written.get(key) != value:
            raise OSError(f"挂机配置写入校验失败: {path} key={key}")
    return cur


def load_role_control(role_id: int | str) -> dict:
    """Read roles/{role_id}/control.json (本机主副控 + 队内控). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {"role": CONTROL_ROLE_NONE, "team": False}
    data = _read_json(role_dir(rid) / "control.json", {"role": CONTROL_ROLE_NONE})
    role = str(data.get("role") or CONTROL_ROLE_NONE).strip().lower()
    if role not in CONTROL_ROLE_CHOICES:
        role = CONTROL_ROLE_NONE
    return {"role": role, "team": bool(data.get("team", False))}


def save_role_control(
    role_id: int | str, role: str = CONTROL_ROLE_NONE, team: bool | None = None
) -> dict:
    """Write roles/{role_id}/control.json (本机主副控 + 队内控). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    role = str(role or CONTROL_ROLE_NONE).strip().lower()
    if role not in CONTROL_ROLE_CHOICES:
        role = CONTROL_ROLE_NONE
    # 读取现有 team 标志（未传则保留），避免覆盖其它窗口保存的队内控状态。
    rec = {"role": role}
    if team is not None:
        rec["team"] = bool(team)
    else:
        rec["team"] = bool(load_role_control(rid).get("team", False))
    _write_json(role_dir(rid) / "control.json", rec)
    return rec


def load_role_inject(role_id: int | str) -> dict:
    """Read roles/{role_id}/inject.json (是否注入预设). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {"enabled": True}
    data = _read_json(role_dir(rid) / "inject.json", {"enabled": True})
    return {"enabled": bool(data.get("enabled", True))}


def save_role_inject(role_id: int | str, enabled: bool = True) -> dict:
    """Write roles/{role_id}/inject.json. @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    rec = {"enabled": bool(enabled)}
    _write_json(role_dir(rid) / "inject.json", rec)
    return rec


def load_role_team(role_id: int | str) -> dict:
    """Read roles/{role_id}/team.json (组队成员配置). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {"members": ""}
    data = _read_json(role_dir(rid) / "team.json", {})
    if not isinstance(data, dict):
        return {"members": ""}
    return {"members": str(data.get("members") or "").strip()}


def save_role_team(role_id: int | str, members: str = "") -> dict:
    """Write roles/{role_id}/team.json (组队成员配置). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    rec = {"members": str(members or "").strip()}
    _write_json(role_dir(rid) / "team.json", rec)
    return rec


def get_role(role_id: int | str) -> dict | None:
    """Merged role record (meta + hang + control + inject). @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return None
    meta = load_role_meta(rid)
    d: dict = {
        "role_id": rid,
        "name": str(meta.get("name") or "").strip(),
        "account_id": str(meta.get("account_id") or "").strip(),
        "hang": load_role_hang(rid),
        "control": load_role_control(rid).get("role") or CONTROL_ROLE_NONE,
        "inject": bool(load_role_inject(rid).get("enabled", True)),
    }
    if not d["account_id"]:
        d["account_id"] = find_account_by_role_id(rid)
    return d


def role_config_path(role_id: int | str, name: str) -> Path:
    """Path of an arbitrary per-role config file roles/{role_id}/{name}.json. @author by ak"""
    rid = normalize_role_id(role_id)
    name = str(name or "").strip()
    if not rid or not name:
        raise ValueError(f"invalid role_id/name: {role_id!r}/{name!r}")
    return role_dir(rid) / f"{name}.json"


def load_role_config(role_id: int | str, name: str, fallback: Any = None) -> Any:
    """Read an arbitrary per-role config (None/missing -> fallback). @author by ak"""
    try:
        path = role_config_path(role_id, name)
    except Exception:
        return fallback
    return _read_json(path, fallback)


def save_role_config(role_id: int | str, name: str, data: Any) -> Path:
    """Atomically write an arbitrary per-role config file. @author by ak"""
    path = role_config_path(role_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)
    return path


def list_roles() -> list[dict]:
    """All role records sorted by role_id. @author by ak"""
    root = roles_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir(), key=lambda p: str(p.name).zfill(20)):
        if not child.is_dir():
            continue
        role = get_role(child.name)
        if role is not None:
            out.append(role)
    return out


def has_role(role_id: int | str) -> bool:
    rid = normalize_role_id(role_id)
    return bool(rid) and role_dir(rid).is_dir()


def delete_role(role_id: int | str) -> bool:
    """Delete a role's config directory; never touches game processes. @author by ak"""
    rid = normalize_role_id(role_id)
    if not rid:
        return False
    path = role_dir(rid)
    if not path.is_dir():
        return False
    try:
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        return False
    return not path.exists()


def merge_role_from_inject(role_id: int | str, name: str = "", pid: int = 0) -> dict:
    """Post-inject binding: write/merge roles/{role_id} and fill account slots.

    读到角色 id+名后创建/合并对应角色目录。若某账号槽位已引用该 role_id 则只
    同步名称；否则优先写入「统一登录」时标记的待绑定账号的启用空槽，其次任意
    账号的启用空槽/选角槽(-1)。写入时把该槽的「是否注入」应用到角色配置。

    @author by ak
    """
    rid = normalize_role_id(role_id)
    if not rid:
        return {}
    meta = save_role_meta(rid, name=name)
    name_clean = str(name or "").strip()
    matched = False
    matched_account = ""
    for acc in list_accounts():
        account_id = str(acc.get("account_id") or "")
        slots = account_slots(account_id)
        changed = False
        for slot in slots:
            if normalize_role_id(slot.get("role_id")) == rid:
                if name_clean and str(slot.get("name") or "").strip() != name_clean:
                    slot["name"] = name_clean
                    changed = True
                matched = True
                matched_account = account_id
        if changed:
            try:
                save_account(account_id, slots=slots)
            except Exception:
                pass
    if not matched:
        # Only bind to the explicit login target. Never scan arbitrary accounts:
        # a stale marker or a late role-bind callback must not cross-fill slots.
        target = get_pending_launch_account(pid=int(pid or 0)) or ""
        if target:
            _fill_role_slot(target, rid, name_clean)
    clear_pending_launch_account(target if not matched else matched_account, pid=int(pid or 0))
    if not meta.get("account_id"):
        owner = find_account_by_role_id(rid)
        if owner:
            save_role_meta(rid, account_id=owner)
    role = get_role(rid)
    return role if role is not None else {}


def apply_account_attrs_to_roles(account_id: str) -> dict:
    """勾选「注入」后把账号属性落到已绑定角色的配置（正式面板直接读取）。

    仅当账号 inject=True 时生效。角色属性 > 账号属性：账号属性只作为**兜底**，
    已存在的角色文件字段（inject/control/hang）优先保留，账号属性不覆盖；
    例外是「副本挂机」勾选时明确把挂机模式写为副本挂机。未绑定角色的账号跳过；
    inject=False 时不动角色配置。

    Returns {"applied": [role_id...], "skipped": [...]} for diagnostics.
    @author by ak
    """
    account_id = str(account_id or "").strip()
    out: dict = {"applied": [], "skipped": []}
    if not account_id:
        return out
    acc = get_account(account_id)
    if acc is None:
        return out
    if not acc.get("inject"):
        return out
    control = str(acc.get("control") or CONTROL_ROLE_NONE).strip().lower()
    if control not in CONTROL_ROLE_CHOICES:
        control = CONTROL_ROLE_NONE
    dungeon_hang = bool(acc.get("dungeon_hang", False))
    team_control = bool(acc.get("team_control", False))
    from app.core.hang_settings import save_hang_prefs

    for slot in acc.get("slots") or []:
        rid = str(slot.get("role_id") or "").strip()
        if not rid or slot.get("role_index") not in ROLE_INDEX_ACTIVE:
            continue
        try:
            # 角色属性优先：角色目录已存在则保留其 inject/control/hang 值；
            # 账号属性只兜底「角色目录还不存在」的缺失配置。
            if not has_role(rid):
                save_role_inject(rid, True)
                save_role_control(rid, control, team=team_control)
                save_hang_prefs(char_id=rid, mode=1 if dungeon_hang else 0)
            else:
                # 队内控/副控：账号勾选时同步到角色 control.json 的 team 标志；
                # 角色已存在时保留其主副控值，不覆盖（角色属性优先）。
                cur_role = load_role_control(rid).get("role", CONTROL_ROLE_NONE)
                if cur_role not in CONTROL_ROLE_CHOICES:
                    cur_role = CONTROL_ROLE_NONE
                save_role_control(rid, cur_role, team=team_control)
                if dungeon_hang:
                    # 副本挂机：账号勾选时把角色挂机模式写为副本挂机。
                    save_hang_prefs(char_id=rid, mode=1)
            out["applied"].append(rid)
        except Exception:
            out["skipped"].append(rid)
    return out


def _fill_role_slot(account_id: str, rid: str, name: str) -> bool:
    """Write a role into the account's first active (1/2/3) slot that is still empty. @author by ak"""
    slots = account_slots(account_id)
    for slot in slots:
        try:
            ri = int(slot.get("role_index") or ROLE_INDEX_NONE)
        except Exception:
            ri = ROLE_INDEX_NONE
        if ri not in ROLE_INDEX_ACTIVE:
            continue
        if str(slot.get("role_id") or "").strip():
            continue
        slot["role_id"] = rid
        slot["name"] = str(name or "").strip()
        try:
            save_account(account_id, slots=slots)
        except Exception:
            return False
        # 注入脚本是账号属性：绑定角色时把账号级 inject 应用到角色配置。
        acc = get_account(account_id)
        save_role_inject(rid, bool(acc["inject"]) if acc is not None else True)
        save_role_meta(rid, account_id=account_id)
        return True
    return False


def _pending_login_path() -> Path:
    return global_dir() / "pending_login.json"


def set_pending_launch_account(account_id: str, pid: int = 0) -> None:
    """Mark an account as the next login target (统一登录 with account selected). @author by ak"""
    account_id = str(account_id or "").strip()
    if not account_id:
        return
    _write_json(_pending_login_path(), {"account_id": account_id, "pid": int(pid or 0), "at": _now_iso()})


def get_pending_launch_account(pid: int = 0) -> str:
    """The account marked for login, optionally restricted to a game pid. @author by ak"""
    data = _read_json(_pending_login_path(), {})
    marked_pid = int(data.get("pid") or 0)
    requested_pid = int(pid or 0)
    if requested_pid > 0 and marked_pid > 0 and marked_pid != requested_pid:
        return ""
    return str(data.get("account_id") or "").strip()


def clear_pending_launch_account(account_id: str | None = None, pid: int = 0) -> None:
    """Clear the pending login marker, optionally only for ``account_id``. @author by ak"""
    try:
        if account_id is not None and get_pending_launch_account(pid=int(pid or 0)) != str(account_id or "").strip():
            return
        _pending_login_path().unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- game path
def load_game_paths() -> dict:
    """Load remembered machine-local game paths. @author by ak"""
    data = _read_json(global_dir() / "game_paths.json", {})
    return {
        "launcher": str(data.get("launcher") or "").strip(),
        "client": str(data.get("client") or "").strip(),
    }


def save_game_path(kind: str, path: str) -> dict:
    """Remember one game path (launcher/client). @author by ak"""
    kind = str(kind or "").strip().lower()
    if kind not in ("launcher", "client"):
        raise ValueError(f"unsupported game_path kind: {kind}")
    paths = load_game_paths()
    paths[kind] = str(path or "").strip()
    paths["updated_at"] = _now_iso()
    _write_json(global_dir() / "game_paths.json", paths)
    return paths


def remembered_launcher() -> str:
    """Remembered launcher/client path or ''. @author by ak"""
    paths = load_game_paths()
    return str(paths.get("launcher") or paths.get("client") or "").strip()


def launch_launcher(path: str | None = None, *, log: LogFn | None = None) -> tuple[bool, str]:
    """Start the selected launcher/client exe (v1 unified login = launch only). @author by ak"""
    log = log or (lambda _m: None)
    target = str(path or "").strip() or remembered_launcher()
    if not target:
        return False, "未设置登录器路径，请先选择"
    if not Path(target).is_file():
        return False, f"登录器不存在: {target}"
    try:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            [target],
            cwd=str(Path(target).parent),
            creationflags=flags,
            close_fds=True,
        )
        log(f"已启动登录器: {target}")
        return True, target
    except Exception as e:
        return False, f"启动登录器失败: {e}"


# ---------------------------------------------------------------- batch kill
def kill_all_games(*, log: LogFn | None = None) -> tuple[bool, str]:
    """Batch-kill every xajh.exe (and children). Only that image name. @author by ak"""
    log = log or (lambda _m: None)
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/IM", PROCESS_IMAGE_XAJH, "/T"],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (completed.stdout or "") + (completed.stderr or "")
        ok = completed.returncode == 0 or (
            "没有找到" in out or "not found" in out.lower()
        )
        log(out.strip() or f"exit={completed.returncode}")
        return ok, out.strip() or f"exit={completed.returncode}"
    except Exception as e:
        return False, str(e)


def kill_pids(pids: list[int] | None, *, log: LogFn | None = None) -> tuple[bool, str]:
    """End specific game pids (batch close for checked accounts). @author by ak"""
    log = log or (lambda _m: None)
    pids = [int(p) for p in (pids or []) if int(p) > 0]
    if not pids:
        return False, "no pids"
    killed = 0
    for pid in pids:
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="ignore",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = (completed.stdout or "") + (completed.stderr or "")
            if completed.returncode == 0 or "not found" in out.lower():
                killed += 1
        except Exception as e:
            log(f"kill pid={pid} err={e}")
    ok = killed > 0
    msg = f"已结束 {killed}/{len(pids)} 个游戏进程"
    log(msg)
    return ok, msg


# ---------------------------------------------------------------- helpers
def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
