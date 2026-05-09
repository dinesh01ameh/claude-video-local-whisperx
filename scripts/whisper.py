#!/usr/bin/env python3
"""Transcribe a video via the local WhisperX LXC service.

Fork of bradautomates/claude-video — Groq/OpenAI paid paths removed.
All non-caption transcription routes to dinesh01ameh's WhisperX LXC over
Tailscale (default http://lp-whisperx:8080, override via LP_WHISPERX_URL).

Public API preserved so watch.py is a near drop-in:
  load_api_key(preferred=None) -> (backend, "")  # api_key always empty for local
  transcribe_video(video_path, audio_out, backend=None, api_key=None)
      -> (segments, "local-whisperx")

Returns segments in the same shape as transcribe.parse_vtt:
  [{"start": float, "end": float, "text": str}, ...]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from local_whisperx import DEFAULT_URL, health_check, transcribe  # noqa: E402


BACKEND_NAME = "local-whisperx"


def _resolve_url() -> str:
    return (os.environ.get("LP_WHISPERX_URL") or DEFAULT_URL).rstrip("/")


def load_api_key(preferred: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """Return (backend, "") if local WhisperX is reachable, else (None, None).

    `preferred` is accepted for backwards-compat with the upstream signature
    but only "local-whisperx" or None makes sense here. Other values are
    treated as "not available" so watch.py falls through to frames-only.
    """
    if preferred is not None and preferred != BACKEND_NAME:
        return None, None

    ok, _detail = health_check()
    if ok:
        return BACKEND_NAME, ""
    return None, None


def extract_audio(video_path: str, out_path: Path) -> Path:
    """Extract mono 16kHz 64kbps mp3 — perfect format for WhisperX intake."""
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install via setup.py.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(Path(video_path).resolve()),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        str(out_path.resolve()),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit("ffmpeg produced no audio — video may have no audio track")
    return out_path


def _segments_from_response(data: dict) -> list[dict]:
    """Project WhisperX result into our {start, end, text} segment format.

    WhisperX result shape: {words[], segments[], language, model_id, diarized}.
    segments[] entries already have start/end/text — we just round and filter.
    """
    out: list[dict] = []
    for seg in data.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "start": round(float(seg.get("start") or 0.0), 2),
            "end": round(float(seg.get("end") or 0.0), 2),
            "text": text,
        })

    # Fallback: synthesize one segment from words[] if segments[] was empty
    if not out:
        words = data.get("words") or []
        if words:
            text = " ".join((w.get("word") or w.get("text") or "").strip() for w in words).strip()
            if text:
                start = round(float(words[0].get("start") or 0.0), 2)
                end = round(float(words[-1].get("end") or 0.0), 2)
                out.append({"start": start, "end": end, "text": text})

    return out


def transcribe_video(
    video_path: str,
    audio_out: Path,
    backend: str | None = None,
    api_key: str | None = None,
) -> tuple[list[dict], str]:
    """Run extract_audio → WhisperX upload → poll → result → segments.

    `backend` and `api_key` accepted for upstream API compat. `api_key` is
    ignored (local service has no auth on the trusted Tailnet path).
    """
    if backend not in (None, BACKEND_NAME):
        raise SystemExit(
            f"unsupported backend {backend!r} — this fork only supports {BACKEND_NAME}"
        )

    url = _resolve_url()
    print(f"[watch] extracting audio for WhisperX ({url})…", file=sys.stderr)
    audio_path = extract_audio(video_path, audio_out)
    size_kb = audio_path.stat().st_size / 1024
    print(f"[watch] audio: {size_kb:.0f} kB — uploading to WhisperX…", file=sys.stderr)

    response = transcribe(audio_path, url=url, language="en", poll_timeout_sec=600)

    segments = _segments_from_response(response)
    if not segments:
        raise SystemExit("WhisperX returned no transcript segments")

    model_id = response.get("model_id") or "whisperx-large-v3"
    print(
        f"[watch] transcribed {len(segments)} segments via {BACKEND_NAME} ({model_id})",
        file=sys.stderr,
    )
    return segments, BACKEND_NAME


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: whisper.py <video-path> [<audio-out.mp3>]", file=sys.stderr)
        raise SystemExit(2)

    video = sys.argv[1]
    audio_out = (
        Path(sys.argv[2])
        if len(sys.argv) > 2 and not sys.argv[2].startswith("--")
        else Path("audio.mp3")
    )
    segments, backend = transcribe_video(video, audio_out)
    print(json.dumps({"backend": backend, "segments": segments}, indent=2))
