# -*- coding: utf-8 -*-
"""
Static crypto / protocol knowledge for current xajh client build.

Purpose: one place to read "where is plain, where is cipher, what hooks matter"
without re-debugging every time. Addresses assume preferred image base 0x400000
(x32dbg shows absolute VA).

@author by ak
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Preferred image base for this 32-bit client (x32dbg attach default).
IMAGE_BASE = 0x00400000


@dataclass(frozen=True)
class HookPoint:
    """One stable analysis anchor. @author by ak"""

    name: str
    va: int
    role: str  # plain_in | cipher_out | key_setup | send_wrapper | net_send | other
    tag: str  # log tag e.g. PLAIN / SEND10
    summary: str
    log_text: str = ""
    pause_cond: str = ""
    notes: str = ""

    @property
    def rva(self) -> int:
        return self.va - IMAGE_BASE if self.va else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "va": f"0x{self.va:08X}" if self.va else "ws2_32.send",
            "rva": f"0x{self.rva:X}" if self.va else "",
            "role": self.role,
            "tag": self.tag,
            "summary": self.summary,
            "log_text": self.log_text,
            "pause_cond": self.pause_cond,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class KnownShell:
    """Frozen plaintext shell used to label PLAIN lines. @author by ak"""

    name: str
    hex_prefix: str
    meaning: str
    fields: tuple[tuple[str, str, str], ...] = ()  # (off, hex, note)


# GNET ARCFourSecurity (static RE + live confirmed).
ARCFOUR_VTABLE = 0x012CD3EC
ARCFOUR_SETKEY = 0x00DAFA60  # KSA
ARCFOUR_UPDATE = 0x00DAFAF0  # PRGA in-place; entry = plain (C2S)
ARCFOUR_UPDATE_RET = 0x00DAFB74  # after transform
SEND_WRAPPER = 0x00D0C130  # thin call into ws2_32.send
SEND_STACK_HIT = 0x00DB0CC2  # earlier stack site seen in RE

# Object layout
ARCFOUR_OFF_VPTR = 0x00
ARCFOUR_OFF_SBOX = 0x08
ARCFOUR_OFF_I = 0x108
ARCFOUR_OFF_J = 0x109

# Octets: +4 begin*, +8 end*
OCTETS_OFF_BEGIN = 0x4
OCTETS_OFF_END = 0x8


CORE_HOOKS: tuple[HookPoint, ...] = (
    HookPoint(
        name="ARCFourSecurity::Update",
        va=ARCFOUR_UPDATE,
        role="plain_in",
        tag="PLAIN",
        summary="C2S 出站入口仍是明文；就地 RC4 后变密文。首选自动打明文点。",
        log_text="PLAIN {mem;16@[[esp+4]+4]}",
        pause_cond="",
        notes="参数 [esp+4]=Octets*；begin=[oct+4], end=[oct+8], len=end-begin。入站时入口是密文。",
    ),
    HookPoint(
        name="ARCFourSecurity::Update.ret",
        va=ARCFOUR_UPDATE_RET,
        role="cipher_out",
        tag="UPD_OUT",
        summary="Update 返回后同一缓冲：出站=密文，入站=解密后明文（S2C 关键点）。",
        log_text="UPD_OUT {mem;24@[[esp+4]+4]}",
        notes="若表达式读空，改用会话内点射 + 对照 UPD_IN。",
    ),
    HookPoint(
        name="ARCFourSecurity::SetKey",
        va=ARCFOUR_SETKEY,
        role="key_setup",
        tag="SETKEY",
        summary="RC4 KSA：会话密钥写入 S-box（低频，握手/重连时）。",
        log_text="SETKEY {mem;16@[[esp+4]+4]}",
    ),
    HookPoint(
        name="send_wrapper",
        va=SEND_WRAPPER,
        role="send_wrapper",
        tag="SEND_W",
        summary="xajh 侧 send 包装；缓冲已是密文。",
        notes="call WS2_32!send。",
    ),
    HookPoint(
        name="ws2_32.send len=10",
        va=0,  # module export
        role="net_send",
        tag="SEND10",
        summary="网络出口密文锚点；len==10 常对应开箱类 C2S 壳。",
        log_text="SEND10 {mem;10@[esp+8]}",
        pause_cond="[esp+0xC]==0xA",
        notes="bp ws2_32.send + 暂停条件过滤长度。密文每次不同=RC4 正常。",
    ),
)


KNOWN_SHELLS: tuple[KnownShell, ...] = (
    KnownShell(
        name="open_chest_c2s10",
        hex_prefix="22080721000100000000",
        meaning="开箱/采集类 C2S 10B 明文壳（同图多样本冻结）",
        fields=(
            ("0", "22", "GNET type 34，疑 GamedataSend"),
            ("1-4", "08 07 21 00", "子命令/常量（同图未变）"),
            ("5-8", "01 00 00 00", "LE int32 = 1"),
            ("9", "00", "pad"),
        ),
    ),
    KnownShell(
        name="gamedata_type22",
        hex_prefix="22",
        meaning="GNET type=0x22 前缀（多类业务内嵌）",
        fields=(("0", "22", "CompactUINT type 34"),),
    ),
)


PIPELINE_TEXT = """\
marshal 明文 Octets
  -> GNET::ARCFourSecurity::Update (0x00DAFAF0)   // 入口=明文 (C2S)
  -> 缓冲就地变密文
  -> send_wrapper (0x00D0C130)
  -> ws2_32.send                                  // 出口=密文
"""


ROLE_GUIDE: dict[str, str] = {
    "PLAIN": "Update 入口 dump → 出站明文 / 入站密文",
    "SEND10": "ws2_32.send len=10 → 网络密文（开箱锚点）",
    "UPD_IN": "同 PLAIN，Update 入口",
    "UPD_OUT": "Update 返回 → 出站密文 / 入站明文",
    "SETKEY": "SetKey 密钥材料（握手）",
}


def hooks_by_role(role: str) -> list[HookPoint]:
    return [h for h in CORE_HOOKS if h.role == role]


def match_shell(hex_body: str) -> KnownShell | None:
    """Longest-prefix shell match. @author by ak"""
    h = (hex_body or "").lower().replace(" ", "")
    best: KnownShell | None = None
    for sh in KNOWN_SHELLS:
        p = sh.hex_prefix.lower()
        if h.startswith(p):
            if best is None or len(p) > len(best.hex_prefix):
                best = sh
    return best


def core_points_markdown() -> str:
    """Short reference block for UI / report. @author by ak"""
    lines = [
        "# 封包核心点（当前 build）",
        "",
        f"- 映像基址: `0x{IMAGE_BASE:08X}`",
        f"- ARCFour vtable: `0x{ARCFOUR_VTABLE:08X}`",
        f"- 对象: `+0x08 S[256]`, `+0x108 i`, `+0x109 j`",
        f"- Octets: `+4 begin*`, `+8 end*`",
        "",
        "## 管道",
        "",
        "```",
        PIPELINE_TEXT.strip(),
        "```",
        "",
        "## 钩子",
        "",
        "| 标签 | 地址 | 角色 | 说明 |",
        "|------|------|------|------|",
    ]
    for h in CORE_HOOKS:
        va = f"`0x{h.va:08X}`" if h.va else "`ws2_32.send`"
        lines.append(f"| `{h.tag}` | {va} | `{h.role}` | {h.summary} |")
    lines.extend(
        [
            "",
            "## 日志含义（只看响应 + 日志就够）",
            "",
        ]
    )
    for tag, meaning in ROLE_GUIDE.items():
        lines.append(f"- **{tag}**: {meaning}")
    lines.extend(
        [
            "",
            "## 已知明文壳",
            "",
        ]
    )
    for sh in KNOWN_SHELLS:
        lines.append(f"- `{sh.hex_prefix}` — {sh.meaning} (`{sh.name}`)")
    lines.append("")
    return "\n".join(lines)


def knowledge_dict() -> dict[str, Any]:
    return {
        "image_base": f"0x{IMAGE_BASE:08X}",
        "arcfour": {
            "vtable": f"0x{ARCFOUR_VTABLE:08X}",
            "setkey": f"0x{ARCFOUR_SETKEY:08X}",
            "update": f"0x{ARCFOUR_UPDATE:08X}",
            "update_ret": f"0x{ARCFOUR_UPDATE_RET:08X}",
            "layout": {"sbox": ARCFOUR_OFF_SBOX, "i": ARCFOUR_OFF_I, "j": ARCFOUR_OFF_J},
        },
        "send_wrapper": f"0x{SEND_WRAPPER:08X}",
        "hooks": [h.as_dict() for h in CORE_HOOKS],
        "shells": [
            {
                "name": s.name,
                "hex_prefix": s.hex_prefix,
                "meaning": s.meaning,
                "fields": [{"off": a, "hex": b, "note": c} for a, b, c in s.fields],
            }
            for s in KNOWN_SHELLS
        ],
        "pipeline": PIPELINE_TEXT.strip(),
        "role_guide": dict(ROLE_GUIDE),
    }
