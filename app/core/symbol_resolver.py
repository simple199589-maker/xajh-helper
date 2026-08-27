# -*- coding: utf-8 -*-
"""Resolve client symbols with explicit provenance and fail-closed status."""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.core.client_build import ClientBuildProfile, profile_for_client


class SymbolSource(str, Enum):
    EXPORT = "export"
    PATTERN = "pattern"
    PROFILE = "profile"
    NONE = "none"


class SymbolStatus(str, Enum):
    VERIFIED = "verified"
    EXPERIMENTAL = "experimental"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ResolvedSymbol:
    key: str
    status: SymbolStatus
    source: SymbolSource
    rva: int | None = None
    address: int | None = None
    reason: str = ""

    @property
    def callable(self) -> bool:
        return self.address is not None and self.status in {
            SymbolStatus.VERIFIED,
            SymbolStatus.EXPERIMENTAL,
        }


class PatternResolver:
    """Wildcard byte-pattern resolver scoped to executable PE sections."""

    def __init__(self, pe_path: str | Path):
        self.path = Path(pe_path)
        self.data = self.path.read_bytes()
        self.sections = self._sections(self.data)

    @staticmethod
    def _sections(data: bytes) -> list[tuple[str, int, int, int, int]]:
        if data[:2] != b"MZ":
            return []
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        nsec = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        off = pe + 24 + opt_size
        out = []
        for i in range(nsec):
            p = off + i * 40
            if p + 40 > len(data):
                break
            name = data[p : p + 8].split(b"\0", 1)[0].decode("ascii", "ignore")
            vsize, va, raw_size, raw_off = struct.unpack_from("<IIII", data, p + 8)
            chars = struct.unpack_from("<I", data, p + 36)[0]
            out.append((name, va, raw_off, raw_size, chars))
        return out

    @staticmethod
    def _regex(pattern: str) -> re.Pattern[bytes]:
        compact = "".join(pattern.strip().split())
        if not compact or len(compact) % 2:
            raise ValueError("empty pattern")
        parts = []
        for i in range(0, len(compact), 2):
            token = compact[i : i + 2]
            if "?" in token:
                parts.append(b".")
            else:
                parts.append(re.escape(bytes([int(token, 16)])))
        return re.compile(b"".join(parts), re.DOTALL)

    def find_rvas(self, pattern: str, *, executable_only: bool = True) -> list[int]:
        rx = self._regex(pattern)
        out: list[int] = []
        for _name, va, raw_off, raw_size, chars in self.sections:
            if executable_only and not (chars & 0x20000000):
                continue
            blob = self.data[raw_off : raw_off + raw_size]
            out.extend(va + m.start() for m in rx.finditer(blob))
        return out

    def unique_rva(self, pattern: str, *, executable_only: bool = True) -> int | None:
        hits = self.find_rvas(pattern, executable_only=executable_only)
        return hits[0] if len(hits) == 1 else None

    def rva_to_offset(self, rva: int) -> int | None:
        for _name, va, raw_off, raw_size, _chars in self.sections:
            if va <= int(rva) < va + raw_size:
                return raw_off + (int(rva) - va)
        return None

    def read_at_rva(self, rva: int, size: int) -> bytes:
        off = self.rva_to_offset(rva)
        if off is None:
            return b""
        return self.data[off : off + int(size)]


def _export_rva(pe_path: str | Path, export_name: str) -> int | None:
    # Local import avoids making the compatibility facade a hard dependency.
    from app.core.plg_exports import resolve_export_rva

    return resolve_export_rva(pe_path, export_name)


def resolve_symbol(
    key: str,
    *,
    pe_path: str | Path,
    module_base: int,
    export_name: str | None = None,
    pattern: str | None = None,
    pattern_status: SymbolStatus = SymbolStatus.EXPERIMENTAL,
    profile: ClientBuildProfile | None = None,
) -> ResolvedSymbol:
    """Resolve export -> unique pattern -> exact-build profile, in that order."""
    path = Path(pe_path)
    if export_name:
        rva = _export_rva(path, export_name)
        if rva is not None:
            return ResolvedSymbol(
                key, SymbolStatus.VERIFIED, SymbolSource.EXPORT, rva, module_base + rva
            )
    if pattern:
        try:
            rva = PatternResolver(path).unique_rva(pattern)
        except (OSError, ValueError, struct.error):
            rva = None
        if rva is not None:
            return ResolvedSymbol(
                key, pattern_status, SymbolSource.PATTERN, rva, module_base + rva
            )
    exact = profile if profile is not None else profile_for_client(str(path))
    entry = exact.symbols.get(key) if exact else None
    if isinstance(entry, dict):
        try:
            status = SymbolStatus(str(entry.get("status") or "unavailable"))
        except ValueError:
            status = SymbolStatus.UNAVAILABLE
        reason = str(entry.get("reason") or "")
        if status in {SymbolStatus.VERIFIED, SymbolStatus.EXPERIMENTAL}:
            try:
                rva = int(str(entry["rva"]), 0)
                return ResolvedSymbol(
                    key, status, SymbolSource.PROFILE, rva, module_base + rva, reason
                )
            except (KeyError, TypeError, ValueError):
                pass
        return ResolvedSymbol(key, status, SymbolSource.PROFILE, reason=reason)
    return ResolvedSymbol(
        key,
        SymbolStatus.UNAVAILABLE,
        SymbolSource.NONE,
        reason="no export/pattern match and no exact-build profile symbol",
    )
