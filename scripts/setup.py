#!/usr/bin/env python3
"""Setup / preflight for /watch (local-WhisperX fork).

Modes:
  setup.py --check      Silent preflight. Exit 0 if ready, 2/3/4 on failure.
  setup.py --json       Machine-readable status for Claude to parse.
  setup.py              Installer. Auto-installs deps, scaffolds .env, marks SETUP_COMPLETE.

Differences from upstream (bradautomates/claude-video):
- No Groq/OpenAI key required. Transcription routes to local WhisperX LXC.
- Health check probes http://lp-whisperx:8080/health (override via LP_WHISPERX_URL)
  during --check so cold service is detected before /watch tries to upload.
- Exit code 3 means "WhisperX unreachable" (not "missing API key").

Design:
- Silent on success: --check exits 0 with no output when ready.
- Idempotent installer: never clobbers existing config.
- Never sudo. macOS: brew. Linux/Windows: print exact commands.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Health probe is the only runtime dep on the new client. Keep import lazy
# so --check doesn't crash if local_whisperx.py is somehow missing.
try:
    from local_whisperx import DEFAULT_URL, health_check  # noqa: E402
except ImportError:  # pragma: no cover
    DEFAULT_URL = "http://lp-whisperx:8080"
    def health_check(url=None, timeout=5):
        return False, "local_whisperx module not installed alongside setup.py"


REQUIRED_BINARIES = ["ffmpeg", "ffprobe", "yt-dlp"]
CONFIG_DIR = Path.home() / ".config" / "watch"
CONFIG_FILE = CONFIG_DIR / ".env"
ENV_TEMPLATE = """# /watch (local-WhisperX fork) configuration
#
# Transcription fallback — used only when yt-dlp cannot get captions
# (or when you point /watch at a local file with no subtitles).
#
# This fork routes ALL non-caption transcription to a local WhisperX LXC
# (whisperx large-v3 on RTX 4090). No paid API keys are needed or used.
#
# Default endpoint: http://lp-whisperx:8080 (Tailscale machine name).
# Override via LP_WHISPERX_URL if you've moved the service.

LP_WHISPERX_URL=

# Optional: future-proofing knob. Only "local-whisperx" is supported in this
# fork. Leave blank for default.
WATCH_BACKEND=
"""


def _which(name: str) -> str | None:
    return shutil.which(name)


def _check_binaries() -> list[str]:
    return [b for b in REQUIRED_BINARIES if not _which(b)]


def _check_file_permissions(path: Path) -> None:
    """Warn to stderr if a config file is world/group readable.

    Skipped on Windows: NTFS uses ACLs, not POSIX mode bits, so st_mode
    always reports 0o666 even on a properly-locked-down file. The warning
    would be both wrong and noisy.
    """
    if platform.system() == "Windows":
        return
    try:
        mode = path.stat().st_mode
        if mode & 0o044:
            sys.stderr.write(
                f"[watch] WARNING: {path} is readable by other users. "
                f"Run: chmod 600 {path}\n"
            )
            sys.stderr.flush()
    except OSError:
        pass


def _read_env_key(name: str) -> str | None:
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    if not CONFIG_FILE.exists():
        return None
    _check_file_permissions(CONFIG_FILE)
    try:
        # utf-8-sig strips the BOM that Windows PowerShell's Set-Content -Encoding utf8 writes.
        for line in CONFIG_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw = line.partition("=")
            if key.strip() != name:
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                raw = raw[1:-1]
            return raw or None
    except OSError:
        return None
    return None


def _whisperx_url() -> str:
    return (_read_env_key("LP_WHISPERX_URL") or DEFAULT_URL).rstrip("/")


def _whisperx_reachable() -> tuple[bool, str]:
    """Probe local WhisperX. Returns (ok, detail)."""
    return health_check(_whisperx_url())


def is_first_run() -> bool:
    """True if the installer hasn't completed successfully yet."""
    return _read_env_key("SETUP_COMPLETE") != "true"


def _scaffold_env() -> bool:
    if CONFIG_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(ENV_TEMPLATE, encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    return True


def _write_setup_complete() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = ""
    if CONFIG_FILE.exists():
        existing = CONFIG_FILE.read_text(encoding="utf-8")
        for line in existing.splitlines():
            if line.strip().startswith("SETUP_COMPLETE="):
                return
        if existing and not existing.endswith("\n"):
            existing += "\n"
        CONFIG_FILE.write_text(existing + "SETUP_COMPLETE=true\n", encoding="utf-8")
    else:
        CONFIG_FILE.write_text(ENV_TEMPLATE + "\nSETUP_COMPLETE=true\n", encoding="utf-8")
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _brew_pkg(missing: list[str]) -> list[str]:
    pkgs: list[str] = []
    for bin_name in missing:
        if bin_name in ("ffmpeg", "ffprobe"):
            if "ffmpeg" not in pkgs:
                pkgs.append("ffmpeg")
        elif bin_name == "yt-dlp":
            if "yt-dlp" not in pkgs:
                pkgs.append("yt-dlp")
        else:
            pkgs.append(bin_name)
    return pkgs


def _install_macos(missing: list[str]) -> tuple[bool, str]:
    if _which("brew") is None:
        return False, (
            "Homebrew is not installed. Install it from https://brew.sh, then re-run setup. "
            "Or install manually: `brew install " + " ".join(_brew_pkg(missing)) + "`"
        )
    pkgs = _brew_pkg(missing)
    if not pkgs:
        return True, "nothing to install"
    cmd = ["brew", "install", *pkgs]
    print(f"[setup] running: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return False, f"brew install failed with exit code {result.returncode}"
    return True, f"installed via brew: {', '.join(pkgs)}"


def _install_hint_linux(missing: list[str]) -> str:
    pkgs = _brew_pkg(missing)
    hints = []
    if "ffmpeg" in pkgs:
        hints.append("apt: `sudo apt install ffmpeg` or dnf: `sudo dnf install ffmpeg`")
    if "yt-dlp" in pkgs:
        hints.append("`pipx install yt-dlp` (recommended) or `pip install --user yt-dlp`")
    return "\n  ".join(hints) if hints else "nothing to install"


def _install_hint_windows(missing: list[str]) -> str:
    pkgs = _brew_pkg(missing)
    hints = []
    if "ffmpeg" in pkgs:
        hints.append("winget: `winget install Gyan.FFmpeg`")
    if "yt-dlp" in pkgs:
        hints.append("winget: `winget install yt-dlp.yt-dlp` or pip: `pip install --user yt-dlp`")
    return "\n  ".join(hints) if hints else "nothing to install"


def _status() -> dict:
    """Structured preflight snapshot."""
    missing = _check_binaries()
    whisperx_ok, whisperx_detail = _whisperx_reachable()

    if not missing and whisperx_ok:
        status = "ready"
    elif missing and not whisperx_ok:
        status = "needs_install_and_whisperx"
    elif missing:
        status = "needs_install"
    else:
        status = "needs_whisperx"

    return {
        "status": status,
        "first_run": is_first_run(),
        "missing_binaries": missing,
        "whisper_backend": "local-whisperx",
        "whisperx_url": _whisperx_url(),
        "whisperx_reachable": whisperx_ok,
        "whisperx_detail": whisperx_detail,
        "config_file": str(CONFIG_FILE),
        "platform": platform.system(),
    }


def cmd_check() -> int:
    """Silent-on-success preflight.

    Exit codes:
      0 → ready
      2 → binaries missing
      3 → WhisperX unreachable (LXC down, Tailscale down, ACL block, etc.)
      4 → both
    """
    s = _status()
    if s["status"] == "ready":
        return 0

    parts = []
    if s["missing_binaries"]:
        parts.append(f"missing binaries: {', '.join(s['missing_binaries'])}")
    if not s["whisperx_reachable"]:
        parts.append(f"WhisperX unreachable ({s['whisperx_detail']})")
    installer = Path(__file__).resolve()
    sys.stderr.write(
        f"[watch] setup incomplete ({'; '.join(parts)}). "
        f"Run: python3 {installer}\n"
    )
    sys.stderr.flush()

    if s["missing_binaries"] and not s["whisperx_reachable"]:
        return 4
    if s["missing_binaries"]:
        return 2
    return 3


def cmd_json() -> int:
    json.dump(_status(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_install() -> int:
    missing = _check_binaries()
    installed_deps = False
    if missing:
        system = platform.system()
        if system == "Darwin":
            ok, msg = _install_macos(missing)
            print(f"[setup] {msg}", file=sys.stderr)
            if not ok:
                return 2
            still_missing = _check_binaries()
            if still_missing:
                print(f"[setup] still missing after install: {', '.join(still_missing)}", file=sys.stderr)
                return 2
            installed_deps = True
        elif system == "Linux":
            print("[setup] dependencies missing on Linux — please install:", file=sys.stderr)
            print("  " + _install_hint_linux(missing), file=sys.stderr)
            return 2
        elif system == "Windows":
            print("[setup] dependencies missing on Windows — please install:", file=sys.stderr)
            print("  " + _install_hint_windows(missing), file=sys.stderr)
            return 2
        else:
            print(f"[setup] unsupported platform ({system}) for auto-install. Install manually:", file=sys.stderr)
            print(f"  missing: {', '.join(missing)}", file=sys.stderr)
            return 2

    created = _scaffold_env()
    if created:
        print(f"[setup] created config: {CONFIG_FILE}")
    else:
        print(f"[setup] config exists: {CONFIG_FILE}")

    whisperx_ok, whisperx_detail = _whisperx_reachable()
    if whisperx_ok:
        _write_setup_complete()
        print(f"[setup] ready. WhisperX: {whisperx_detail}")
        if installed_deps:
            print("[setup] installed dependencies; /watch is fully set up.")
        return 0

    print("")
    print("[setup] one step left: WhisperX is not reachable.")
    print(f"  Detail: {whisperx_detail}")
    print(f"  URL:    {_whisperx_url()}")
    print("")
    print("  Verify:")
    print(f"    1. Tailscale is up on this machine ({platform.node()}).")
    print( "    2. WhisperX LXC is running:  ssh into the host, check the LXC.")
    print(f"    3. ACL allows this machine as src to {_whisperx_url()}.")
    print( "    4. Override URL via LP_WHISPERX_URL=http://... in ~/.config/watch/.env")
    print("")
    print("  Without WhisperX, /watch still works but videos without native captions")
    print("  will come back frames-only.")
    return 3


def main() -> int:
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--check":
            return cmd_check()
        if arg == "--json":
            return cmd_json()
    return cmd_install()


if __name__ == "__main__":
    raise SystemExit(main())
