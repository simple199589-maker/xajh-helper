from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.bridge_protocol import BRIDGE_BUILD_ID

PKG_DIR = str(sys.argv[1] if len(sys.argv) > 1 else "").strip() or str(
    os.environ.get("XAJH_PKG_DIR") or "xajh_helper"
).strip()
SOURCE_ROOT = Path(os.environ.get("XAJH_NATIVE_BIN_DIR") or ROOT / "native" / "bin")
SOURCE = SOURCE_ROOT / "xajh_bridge.dll"
STAMPED_SOURCE = SOURCE_ROOT / f"xajh_bridge_{BRIDGE_BUILD_ID}.dll"
PACKAGED_CANDIDATES = (
    ROOT / "dist" / PKG_DIR / "_internal" / "native" / "bin" / "xajh_bridge.dll",
    ROOT / "dist" / PKG_DIR / "native" / "bin" / "xajh_bridge.dll",
)
STAMPED_PACKAGED_CANDIDATES = (
    ROOT / "dist" / PKG_DIR / "_internal" / "native" / "bin" / f"xajh_bridge_{BRIDGE_BUILD_ID}.dll",
    ROOT / "dist" / PKG_DIR / "native" / "bin" / f"xajh_bridge_{BRIDGE_BUILD_ID}.dll",
)
TEAM_TAP_PACKAGED_CANDIDATES = (
    ROOT / "dist" / PKG_DIR / "_internal" / "native" / "bin" / "xajh_team_tap.dll",
    ROOT / "dist" / PKG_DIR / "native" / "bin" / "xajh_team_tap.dll",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    if not SOURCE.is_file():
        print(f"source xajh_bridge.dll missing: {SOURCE}", file=sys.stderr)
        return 2
    if not STAMPED_SOURCE.is_file():
        print(f"source stamped bridge missing: {STAMPED_SOURCE}", file=sys.stderr)
        return 2
    source_hash = _sha256(SOURCE)
    expected_stamped_hash = _sha256(STAMPED_SOURCE)
    if source_hash != expected_stamped_hash:
        print(
            "source bridge hash mismatch between generic and stamped DLL: "
            f"generic={source_hash} stamped={expected_stamped_hash}",
            file=sys.stderr,
        )
        return 3
    packaged = next((path for path in PACKAGED_CANDIDATES if path.is_file()), None)
    if packaged is None:
        print("packaged xajh_bridge.dll missing", file=sys.stderr)
        return 2

    packaged_hash = _sha256(packaged)
    if source_hash != packaged_hash:
        print(
            "packaged bridge hash mismatch: "
            f"source={source_hash} package={packaged_hash}",
            file=sys.stderr,
        )
        return 3
    stamped = next(
        (path for path in STAMPED_PACKAGED_CANDIDATES if path.is_file()), None
    )
    if stamped is None:
        print("packaged stamped bridge missing", file=sys.stderr)
        return 2
    stamped_hash = _sha256(stamped)
    if stamped_hash != expected_stamped_hash:
        print(
            "packaged stamped bridge hash mismatch: "
            f"source={expected_stamped_hash} package={stamped_hash}",
            file=sys.stderr,
        )
        return 3
    if not any(path.is_file() for path in TEAM_TAP_PACKAGED_CANDIDATES):
        print("packaged xajh_team_tap.dll missing", file=sys.stderr)
        return 2
    print(f"packaged bridge hash: OK {source_hash}")
    print(f"packaged stamped bridge hash: OK {stamped_hash}")
    print("packaged team tap: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

