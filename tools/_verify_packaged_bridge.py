from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PKG_DIR = str(sys.argv[1] if len(sys.argv) > 1 else "").strip() or str(
    os.environ.get("XAJH_PKG_DIR") or "xajh_helper"
).strip()
SOURCE_ROOT = Path(os.environ.get("XAJH_NATIVE_BIN_DIR") or ROOT / "native" / "bin")
# 统一具名规则：桥只有 xajh_bridge.dll 一个名字（build id 在协议头，不在文件名），
# 打包校验不再要求 stamped 副本。
SOURCE = SOURCE_ROOT / "xajh_bridge.dll"
PACKAGED_CANDIDATES = (
    ROOT / "dist" / PKG_DIR / "_internal" / "native" / "bin" / "xajh_bridge.dll",
    ROOT / "dist" / PKG_DIR / "native" / "bin" / "xajh_bridge.dll",
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
    source_hash = _sha256(SOURCE)
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
    if not any(path.is_file() for path in TEAM_TAP_PACKAGED_CANDIDATES):
        print("packaged xajh_team_tap.dll missing", file=sys.stderr)
        return 2
    print(f"packaged bridge hash: OK {source_hash}")
    print("packaged team tap: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
