#!/usr/bin/env python3
"""Client for the local WhisperX LXC service (Tailscale: lp-whisperx:8080).

Implements the documented contract:
  POST /transcribe-upload    multipart {audio, language="en"} -> {job_id}
  GET  /jobs/{id}/status     -> {status, progress, error}
  GET  /jobs/{id}/result     -> {words[], segments[], language, model_id, diarized}
  GET  /health               -> {model_loaded, gpu_memory_used_mb, queue_depth, ...}

Pure stdlib — no requests dependency, matches whisper.py's stdlib-only style.
This file is vendored, not imported from learning_pipeline.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import sys
import time
import urllib.error
import uuid
from pathlib import Path
from urllib.request import Request, urlopen


DEFAULT_URL = "http://lp-whisperx:8080"
POLL_INTERVAL_SEC = 5.0
COLD_START_NOTICE_SEC = 30.0
_DOTENV_PATH = Path.home() / ".config" / "watch" / ".env"


def _bootstrap_env_from_dotenv() -> None:
    """Load LP_WHISPERX_URL from ~/.config/watch/.env into os.environ.

    setup.py owns the full config schema; this module only needs the URL,
    so we duplicate just enough .env parsing to stay decoupled. utf-8-sig
    strips the BOM that PowerShell's Set-Content -Encoding utf8 writes.
    """
    if "LP_WHISPERX_URL" in os.environ and os.environ["LP_WHISPERX_URL"].strip():
        return
    if not _DOTENV_PATH.exists():
        return
    try:
        for line in _DOTENV_PATH.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != "LP_WHISPERX_URL":
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            if value:
                os.environ["LP_WHISPERX_URL"] = value
            return
    except OSError:
        return


_bootstrap_env_from_dotenv()


def _build_multipart(audio_path: Path, language: str) -> tuple[bytes, str]:
    """Build multipart/form-data with two fields: audio (file), language (str)."""
    boundary = f"----WhisperXBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buf = io.BytesIO()

    # language field
    buf.write(f"--{boundary}".encode()); buf.write(eol)
    buf.write(b'Content-Disposition: form-data; name="language"'); buf.write(eol)
    buf.write(eol)
    buf.write(language.encode()); buf.write(eol)

    # audio field
    mimetype = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    buf.write(f"--{boundary}".encode()); buf.write(eol)
    buf.write(
        f'Content-Disposition: form-data; name="audio"; filename="{audio_path.name}"'.encode()
    )
    buf.write(eol)
    buf.write(f"Content-Type: {mimetype}".encode()); buf.write(eol)
    buf.write(eol)
    buf.write(audio_path.read_bytes())
    buf.write(eol)
    buf.write(f"--{boundary}--".encode()); buf.write(eol)

    return buf.getvalue(), boundary


def _post_upload(url: str, audio_path: Path, language: str, timeout: int = 300) -> str:
    body, boundary = _build_multipart(audio_path, language)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "claude-video-local-whisperx/0.2.0 (+claude-code)",
    }
    request = Request(f"{url}/transcribe-upload", data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = f" — {exc.read().decode('utf-8', errors='replace')[:400]}"
        except Exception:
            pass
        raise SystemExit(f"WhisperX upload failed: HTTP {exc.code}{detail}")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(
            f"WhisperX upload failed: {type(exc).__name__}: {exc}. "
            f"Is the LXC reachable at {url}? Check Tailscale + ACL."
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"WhisperX returned non-JSON on upload: {exc}: {payload[:200]}")

    job_id = data.get("job_id")
    if not job_id:
        raise SystemExit(f"WhisperX upload returned no job_id: {payload[:200]}")
    return job_id


def _poll_status(url: str, job_id: str, poll_timeout_sec: int) -> None:
    """Block until status=done. Raise on failed/timeout."""
    started = time.monotonic()
    cold_start_notified = False
    last_progress = -1

    while True:
        elapsed = time.monotonic() - started
        if elapsed > poll_timeout_sec:
            raise SystemExit(
                f"WhisperX job {job_id} did not finish within {poll_timeout_sec}s. "
                "Cold start typically takes 2-4 min on first call after LXC boot."
            )

        if not cold_start_notified and elapsed > COLD_START_NOTICE_SEC:
            print(
                "[watch] WhisperX cold-starting (model loading on RTX 4090). "
                "First call takes 2-4 min; subsequent calls are seconds.",
                file=sys.stderr,
            )
            cold_start_notified = True

        try:
            with urlopen(f"{url}/jobs/{job_id}/status", timeout=30) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError) as exc:
            print(f"[watch] poll error ({type(exc).__name__}: {exc}) — retrying", file=sys.stderr)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        status = data.get("status")
        progress = data.get("progress")
        if status == "done":
            return
        if status == "failed":
            raise SystemExit(f"WhisperX job failed: {data.get('error') or 'no detail'}")
        if status not in ("queued", "running"):
            raise SystemExit(f"WhisperX returned unexpected status: {status!r}")

        if isinstance(progress, (int, float)) and int(progress) != last_progress:
            last_progress = int(progress)
            if last_progress in (25, 50, 75):
                print(f"[watch] WhisperX progress: {last_progress}%", file=sys.stderr)

        time.sleep(POLL_INTERVAL_SEC)


def _fetch_result(url: str, job_id: str) -> dict:
    try:
        with urlopen(f"{url}/jobs/{job_id}/result", timeout=60) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = f" — {exc.read().decode('utf-8', errors='replace')[:400]}"
        except Exception:
            pass
        raise SystemExit(f"WhisperX result fetch failed: HTTP {exc.code}{detail}")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"WhisperX result fetch failed: {type(exc).__name__}: {exc}")


def transcribe(
    audio_path: str | Path,
    *,
    url: str | None = None,
    language: str = "en",
    poll_timeout_sec: int = 600,
) -> dict:
    """Submit audio, poll, return raw WhisperX result.

    Result shape: {words[], segments[], language, model_id, diarized}.
    Segments are {start, end, text, ...} — drop-in for transcribe.parse_vtt output
    after a thin field projection.
    """
    target_url = (url or os.environ.get("LP_WHISPERX_URL") or DEFAULT_URL).rstrip("/")
    audio = Path(audio_path).resolve()
    if not audio.exists():
        raise SystemExit(f"audio file not found: {audio}")

    job_id = _post_upload(target_url, audio, language)
    _poll_status(target_url, job_id, poll_timeout_sec)
    return _fetch_result(target_url, job_id)


def health_check(url: str | None = None, timeout: int = 5) -> tuple[bool, str]:
    """Return (ok, detail). detail is human-readable."""
    target_url = (url or os.environ.get("LP_WHISPERX_URL") or DEFAULT_URL).rstrip("/")
    try:
        with urlopen(f"{target_url}/health", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} from {target_url}/health"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"unreachable at {target_url}: {type(exc).__name__}: {exc}"
    except json.JSONDecodeError:
        return False, f"non-JSON response from {target_url}/health"

    if not data.get("whisperx_available"):
        return False, f"whisperx_available=false at {target_url}"
    model = data.get("model_id") or "unknown"
    queue = data.get("queue_depth", 0)
    loaded = "loaded" if data.get("model_loaded") else "cold"
    return True, f"ok ({model}, {loaded}, queue={queue})"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: local_whisperx.py <audio-path> [--health]", file=sys.stderr)
        raise SystemExit(2)
    if sys.argv[1] == "--health":
        ok, detail = health_check()
        print(f"{'OK' if ok else 'FAIL'}: {detail}")
        raise SystemExit(0 if ok else 1)
    print(json.dumps(transcribe(sys.argv[1]), indent=2))
