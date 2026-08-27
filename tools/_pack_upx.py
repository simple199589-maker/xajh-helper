# -*- coding: utf-8 -*-
"""Apply a pinned UPX shell to the packaged GUI executable only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = str(os.environ.get("XAJH_PKG_DIR") or "xajh_helper").strip()
DEFAULT_TARGET = ROOT / "dist" / PKG_DIR / "xajh_helper.exe"
REPORT_PATH = ROOT / "build" / "generated" / "upx_pack_report.json"

UPX_VERSION = "5.2.0"
UPX_ARCHIVE_SHA256 = (
    "b471ebf1b7f20f4a89150264ed9a008a2a5bfd247f3c6d1184a75bb59ca08f5d"
)
UPX_ARCHIVE_MEMBER = f"upx-{UPX_VERSION}-win64/upx.exe"
UPX_DOWNLOAD_URLS = (
    f"https://github.com/upx/upx/releases/download/v{UPX_VERSION}/"
    f"upx-{UPX_VERSION}-win64.zip",
    f"https://ghproxy.net/https://github.com/upx/upx/releases/download/"
    f"v{UPX_VERSION}/upx-{UPX_VERSION}-win64.zip",
)
UPX_CACHE_EXE = ROOT / "build" / "tools" / f"upx-{UPX_VERSION}" / "upx.exe"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    print("+", subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(
        command,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.stdout:
        print(result.stdout.rstrip(), flush=True)
    if result.returncode:
        raise RuntimeError(
            f"command failed with exit code {result.returncode}: "
            f"{subprocess.list2cmdline(command)}"
        )
    return result


def _download_upx() -> Path:
    UPX_CACHE_EXE.parent.mkdir(parents=True, exist_ok=True)
    custom_url = str(os.environ.get("XAJH_UPX_DOWNLOAD_URL") or "").strip()
    urls = ([custom_url] if custom_url else []) + list(UPX_DOWNLOAD_URLS)

    with tempfile.TemporaryDirectory(prefix="xajh-upx-") as temp_dir:
        archive = Path(temp_dir) / f"upx-{UPX_VERSION}-win64.zip"
        last_error: Exception | None = None
        for url in urls:
            try:
                print(f"Downloading pinned UPX {UPX_VERSION}: {url}", flush=True)
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "xajh-helper-build/1.0"},
                )
                with urllib.request.urlopen(request, timeout=60) as response:
                    with archive.open("wb") as output:
                        shutil.copyfileobj(response, output)
                actual_hash = _sha256(archive)
                if actual_hash.lower() != UPX_ARCHIVE_SHA256:
                    raise RuntimeError(
                        "UPX archive SHA-256 mismatch: "
                        f"expected {UPX_ARCHIVE_SHA256}, got {actual_hash}"
                    )
                break
            except Exception as exc:
                last_error = exc
                archive.unlink(missing_ok=True)
                print(f"UPX download failed: {exc}", flush=True)
        else:
            raise RuntimeError(
                "unable to download the pinned UPX archive; set "
                "XAJH_UPX_EXE to a verified upx.exe"
            ) from last_error

        with zipfile.ZipFile(archive) as source:
            try:
                payload = source.read(UPX_ARCHIVE_MEMBER)
            except KeyError as exc:
                raise RuntimeError(
                    f"UPX archive is missing {UPX_ARCHIVE_MEMBER}"
                ) from exc
        temp_exe = UPX_CACHE_EXE.with_suffix(".tmp")
        temp_exe.write_bytes(payload)
        os.replace(temp_exe, UPX_CACHE_EXE)

    return UPX_CACHE_EXE


def find_upx(explicit: str = "", *, allow_download: bool = True) -> Path:
    candidates: list[Path] = []
    configured = explicit or str(os.environ.get("XAJH_UPX_EXE") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    path_hit = shutil.which("upx")
    if path_hit:
        candidates.append(Path(path_hit))
    candidates.append(UPX_CACHE_EXE)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    if allow_download:
        return _download_upx().resolve()
    raise FileNotFoundError(
        "UPX not found; set XAJH_UPX_EXE or allow the pinned download"
    )


def pack_executable(target: Path, upx_exe: Path) -> dict[str, object]:
    target = target.resolve()
    upx_exe = upx_exe.resolve()
    if not target.is_file():
        raise FileNotFoundError(f"packaged executable not found: {target}")
    if target.suffix.lower() != ".exe":
        raise ValueError(f"UPX target must be an .exe file: {target}")
    if not upx_exe.is_file():
        raise FileNotFoundError(f"upx.exe not found: {upx_exe}")

    before_size = target.stat().st_size
    before_hash = _sha256(target)
    version = _run([str(upx_exe), "--version"]).stdout.splitlines()[0].strip()

    # PyInstaller's current Windows bootloader has GUARD_CF enabled. UPX 5.2
    # requires --force for that PE format; --overlay=copy preserves the appended
    # PyInstaller archive. Only the top-level launcher is processed here.
    _run(
        [
            str(upx_exe),
            "--force",
            "--best",
            "--lzma",
            "--overlay=copy",
            str(target),
        ]
    )
    _run([str(upx_exe), "-t", str(target)])

    with target.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("packed output is no longer a Windows PE executable")

    report: dict[str, object] = {
        "target": str(target.relative_to(ROOT)),
        "upx_version": version,
        "upx_exe_sha256": _sha256(upx_exe),
        "before_size": before_size,
        "after_size": target.stat().st_size,
        "before_sha256": before_hash,
        "after_sha256": _sha256(target),
        "upx_test": "ok",
        "scope": "top-level launcher only",
        "guard_cf_note": "UPX --force is required by the PyInstaller bootloader",
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "UPX_PACK_OK "
        f"before={before_size} after={report['after_size']} "
        f"report={REPORT_PATH}",
        flush=True,
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            f"Apply the pinned UPX shell to dist/{PKG_DIR}/xajh_helper.exe "
            "(target dir follows XAJH_PKG_DIR)"
        )
    )
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--upx-exe", default="")
    parser.add_argument("--no-download", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = args.target.resolve()
    if target != DEFAULT_TARGET.resolve():
        raise ValueError(
            "refusing to pack an unexpected file; the supported target is "
            f"{DEFAULT_TARGET}"
        )
    upx_exe = find_upx(args.upx_exe, allow_download=not args.no_download)
    pack_executable(target, upx_exe)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"UPX_PACK_FAILED: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
