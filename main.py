# -*- coding: utf-8 -*-
"""Launch the XAJH business GUI shell (single instance per scope: source/dev/prod)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ultra-early crash capture: install BEFORE importing the heavy GUI stack.
# All crashes unify into logs/xajh_helper_error_YYYYMMDD.log
try:
    from app.core.crash_capture import install_crash_handlers

    install_crash_handlers()
except Exception:
    try:
        import traceback
        from datetime import datetime

        log_dir = ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        p = log_dir / f"xajh_helper_error_{day}.log"
        with open(p, "a", encoding="utf-8", errors="replace") as f:
            f.write(
                f"\n[{datetime.now().isoformat(timespec='seconds')}] "
                f"BOOTSTRAP_INSTALL_FAILED\n"
            )
            f.write(traceback.format_exc())
            f.flush()
    except Exception:
        pass

BUILD_SMOKE_ARG = "--xajh-build-smoke-test"


def _run_build_smoke_test() -> int:
    """Verify that the frozen runtime and its required payload can load."""
    try:
        if not bool(getattr(sys, "frozen", False)):
            raise RuntimeError("smoke test must run from the frozen executable")

        from common.paths import APP_DATA_DIR, NATIVE_BIN_DIR

        required = [
            APP_DATA_DIR / "build_profile.json",
            NATIVE_BIN_DIR / "xajh_bridge.dll",
            NATIVE_BIN_DIR / "xajh_team_tap.dll",
            NATIVE_BIN_DIR / "xajh_inject.exe",
            NATIVE_BIN_DIR / "dummy_damage_reader.exe",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError("missing packaged payload: " + ", ".join(missing))

        # Import the real GUI stack and optional frozen dependencies without
        # creating a window or touching game processes.
        import PIL.Image  # noqa: F401
        import psutil  # noqa: F401
        import pymem  # noqa: F401
        import pystray  # noqa: F401
        import app.ui.app_shell  # noqa: F401
        from app.core.build_profile import channel
        from app.core.youfeng_chain import YoufengChainRunner

        # Exercise the formal 有凤 observer path. This must not depend on lab
        # modules excluded by the production PyInstaller profile.
        observer = YoufengChainRunner(1, drive_casts=False)
        observer._session = type("SmokeSession", (), {"module_base": 0x400000, "pm": None})()
        snapshot = observer._snapshot()
        if set(snapshot) != {
            "skill",
            "config",
            "busy",
            "perform_type",
            "gate0",
            "gate1",
            "gate2",
        }:
            raise RuntimeError("unexpected youfeng observer snapshot")

        build_channel = channel()
        if build_channel not in {"dev", "prod"}:
            raise RuntimeError(f"unexpected build channel: {build_channel!r}")
        return 0
    except Exception:
        import traceback

        traceback.print_exc()
        return 2


if __name__ == "__main__" and BUILD_SMOKE_ARG in sys.argv[1:]:
    raise SystemExit(_run_build_smoke_test())


from app.ui.app_shell import main


if __name__ == "__main__":
    main()
