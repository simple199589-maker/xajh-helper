# -*- coding: utf-8 -*-
"""Verify a native bin directory contains the full native artifact inventory.

Catches missing / empty / wrong-architecture binaries BEFORE PyInstaller, so
packaging never fails with an anonymous "Unable to find ..." data-file error.

Usage:
  python tools\\check_native_bin.py                 # ROOT/native/bin
  python tools\\check_native_bin.py build\\native    # explicit directory
  (env XAJH_NATIVE_BIN_DIR overrides the default target directory)

Exit codes: 0 ok, 2 any missing/empty/arch/hash problem.

@author by ak
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.native_inventory import NATIVE_BIN_FILES  # noqa: E402

MACHINE_I386 = 0x014C


def _pe_machine(path: Path) -> int | None:
    """Read PE Machine field (0x14C=i386, 0x8664=AMD64). None if unreadable."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) < 0x40 or raw[:2] != b"MZ":
        return None
    e_lfanew = int.from_bytes(raw[0x3C:0x40], "little")
    if e_lfanew <= 0 or e_lfanew + 6 > len(raw):
        return None
    if raw[e_lfanew : e_lfanew + 4] != b"PE\0\0":
        return None
    return int.from_bytes(raw[e_lfanew + 4 : e_lfanew + 6], "little")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _resolve_target(argv: list[str]) -> Path:
    if len(argv) > 1 and argv[1].strip():
        return (ROOT / argv[1]).resolve()
    env = os.environ.get("XAJH_NATIVE_BIN_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return (ROOT / "native" / "bin").resolve()


def main(argv: list[str]) -> int:
    target = _resolve_target(argv)
    if not target.is_dir():
        print(f"not a directory: {target}", file=sys.stderr)
        return 2

    names = NATIVE_BIN_FILES
    missing, empty, bad_arch = [], [], []
    for name in names:
        path = target / name
        if not path.is_file():
            missing.append(name)
            continue
        if path.stat().st_size == 0:
            empty.append(name)
        machine = _pe_machine(path)
        if machine != MACHINE_I386:
            bad_arch.append(f"{name} (machine={machine})")

    if missing or empty or bad_arch:
        if missing:
            print(f"missing ({len(missing)}): " + ", ".join(missing), file=sys.stderr)
        if empty:
            print(f"empty ({len(empty)}): " + ", ".join(empty), file=sys.stderr)
        if bad_arch:
            print(
                "wrong architecture (expected x86 0x14C): " + ", ".join(bad_arch),
                file=sys.stderr,
            )
        return 2

    print(f"native bin OK: {len(names)} files present in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))