# -*- coding: utf-8 -*-
"""Client binary fingerprint and exact-build capability profiles."""
from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


PROFILE_PATH = Path(__file__).resolve().parents[1] / "data" / "client_builds.json"


@dataclass(frozen=True)
class ClientBuildFingerprint:
    """Stable identity and basic PE32 metadata for one xajh.exe."""

    sha256: str
    size: int
    machine: int
    timestamp: int
    image_base: int
    image_size: int

    @property
    def is_x86(self) -> bool:
        return self.machine == 0x014C


@dataclass(frozen=True)
class CapabilitySet:
    """Immutable set of capabilities proven for an exact client build."""

    values: frozenset[str] = frozenset()

    def supports(self, capability: str) -> bool:
        return str(capability) in self.values

    def __contains__(self, capability: object) -> bool:
        return str(capability) in self.values

    def __iter__(self) -> Iterable[str]:
        return iter(sorted(self.values))


@dataclass(frozen=True)
class ClientBuildProfile:
    """Version-pinned symbols and capabilities loaded from client_builds.json."""

    build_id: str
    fingerprint: ClientBuildFingerprint
    verified_at: str
    capabilities: CapabilitySet
    symbols: dict[str, dict]


def fingerprint_client(path: str | Path) -> ClientBuildFingerprint:
    """Hash a PE32 file and return the fields used by the profile gate."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    data = p.read_bytes()
    if len(data) < 0x100 or data[:2] != b"MZ":
        raise ValueError(f"not a PE file: {p}")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 + 64 > len(data) or data[pe : pe + 4] != b"PE\x00\x00":
        raise ValueError(f"invalid PE header: {p}")
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    timestamp = struct.unpack_from("<I", data, pe + 8)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic != 0x10B:
        raise ValueError(f"expected PE32 client, magic=0x{magic:X}")
    return ClientBuildFingerprint(
        sha256=h.hexdigest().upper(),
        size=len(data),
        machine=machine,
        timestamp=timestamp,
        image_base=struct.unpack_from("<I", data, opt + 28)[0],
        image_size=struct.unpack_from("<I", data, opt + 56)[0],
    )


@lru_cache(maxsize=1)
def load_client_profiles(path: str | Path | None = None) -> tuple[ClientBuildProfile, ...]:
    """Load checked-in exact-build profiles. Invalid entries fail closed."""
    p = Path(path) if path else PROFILE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ()
    out: list[ClientBuildProfile] = []
    for item in raw.get("builds", []) if isinstance(raw, dict) else []:
        try:
            fp = ClientBuildFingerprint(
                sha256=str(item["sha256"]).upper(),
                size=int(item["size"]),
                machine=int(str(item["machine"]), 0),
                timestamp=int(str(item["timestamp"]), 0),
                image_base=int(str(item["image_base"]), 0),
                image_size=int(str(item["image_size"]), 0),
            )
            out.append(
                ClientBuildProfile(
                    build_id=str(item["id"]),
                    fingerprint=fp,
                    verified_at=str(item.get("verified_at") or ""),
                    capabilities=CapabilitySet(
                        frozenset(str(v) for v in item.get("capabilities", []))
                    ),
                    symbols=dict(item.get("symbols") or {}),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def match_client_profile(
    fingerprint: ClientBuildFingerprint,
    profiles: Iterable[ClientBuildProfile] | None = None,
) -> ClientBuildProfile | None:
    """Return an exact SHA-256 and size match; metadata alone is insufficient."""
    for profile in profiles if profiles is not None else load_client_profiles():
        known = profile.fingerprint
        if known.sha256 == fingerprint.sha256.upper() and known.size == fingerprint.size:
            return profile
    return None


def profile_for_client(path: str) -> ClientBuildProfile | None:
    """Fingerprint a client path and return its exact profile, if known.

    Do not cache by path: launchers may replace xajh.exe in place while the
    helper remains open. Reusing a previous hash would grant stale capabilities.
    """
    try:
        return match_client_profile(fingerprint_client(path))
    except (OSError, ValueError):
        return None


def session_supports(session, capability: str) -> bool:
    """Return True only when a session's exact executable grants capability."""
    path = str(getattr(session, "exe_path", None) or "").strip()
    if not path:
        return False
    profile = profile_for_client(path)
    return bool(profile and profile.capabilities.supports(capability))
