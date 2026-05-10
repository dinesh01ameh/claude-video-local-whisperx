#!/usr/bin/env python3
"""/watch entry point.

Three modes:
  default   /watch <source>                    full pipeline: download + dense frames + transcript + Claude reads frames
  preview   /watch --preview <source>          sparse frames + transcript only, no Claude. Dashboard surfaces it for marking.
  focused   /watch --focused --work-dir <dir>  extract dense frames in marked segments only; minimal Claude payload
            --segments-json '[...]' --user-review '...'

The marker workflow (preview → mark in dashboard → focused) is the recommended
path for long videos: human-in-the-loop curation before AI processing. Default
mode preserved for backwards compat and short clips.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from dashboard import render_dashboard, update_manifest  # noqa: E402
from download import download, is_url  # noqa: E402
from frames import MAX_FPS, auto_fps, auto_fps_focus, extract, format_time, get_metadata, parse_time  # noqa: E402
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from whisper import load_api_key, transcribe_video  # noqa: E402


# ─── Project directory resolution ────────────────────────────────────────────

def _resolve_project_dir(explicit_out_dir: str | None) -> tuple[Path | None, Path | None]:
    """Return (project_dir, cache_dir) honoring WATCH_PROJECT_DIR env var."""
    project_env = os.environ.get("WATCH_PROJECT_DIR", "").strip()
    if project_env:
        project = Path(project_env).expanduser().resolve()
        return project, project / ".watch-cache"
    return None, None


# ─── Manifest helpers ────────────────────────────────────────────────────────

def _manifest_paths(project_dir: Path) -> tuple[Path, Path]:
    return project_dir / ".watch-cache" / "index.json", project_dir / ".watch-cache" / "dashboard.html"


def _record_id_from_workdir(work: Path) -> str:
    return work.name


def _update_manifest_record(project_dir: Path, record_id: str, patch: dict) -> None:
    """Find existing record by id, merge patch in. Append if missing.

    Manifest is the source of truth; we never silently lose existing fields.
    """
    manifest_path, dashboard_path = _manifest_paths(project_dir)
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = []
    else:
        existing = []
    if not isinstance(existing, list):
        existing = []

    found = False
    for rec in existing:
        if rec.get("id") == record_id:
            rec.update(patch)
            found = True
            break
    if not found:
        existing.append({"id": record_id, **patch})

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        render_dashboard(manifest_path, dashboard_path)
    except Exception as exc:
        print(f"[watch] dashboard update failed (non-fatal): {exc}", file=sys.stderr)


# ─── Common helpers ──────────────────────────────────────────────────────────

def _resolve_workdir(args) -> tuple[Path, Path | None]:
    """Return (work_dir, project_dir). Creates work_dir if needed."""
    project_dir, cache_dir = _resolve_project_dir(args.out_dir)
    if args.out_dir:
        work = Path(args.out_dir).expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
    elif cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="watch-", dir=str(cache_dir)))
    else:
        work = Path(tempfile.mkdtemp(prefix="watch-"))
    print(f"[watch] working dir: {work}", file=sys.stderr)
    if project_dir is not None:
        print(f"[watch] project dir: {project_dir}", file=sys.stderr)
    return work, project_dir


def _get_transcript(dl: dict, video_path: str, work: Path, no_whisper: bool, whisper_pref: str | None) -> tuple[list[dict], str | None]:
    """Return (segments, source). source is 'captions' / 'local-whisperx' / None."""
    if dl.get("subtitle_path"):
        try:
            return parse_vtt(dl["subtitle_path"]), "captions"
        except Exception as exc:
            print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)

    if not no_whisper:
        backend, api_key = load_api_key(whisper_pref)
        if backend:
            try:
                segs, used = transcribe_video(video_path, work / "audio.mp3", backend=backend, api_key=api_key)
                return segs, used
            except SystemExit as exc:
                print(f"[watch] whisper fallback failed: {exc}", file=sys.stderr)

    return [], None


# ─── PREVIEW MODE ────────────────────────────────────────────────────────────

def cmd_preview(args) -> int:
    """Download + sparse frame extraction + transcript. No Claude. Dashboard surfaces it."""
    work, project_dir = _resolve_workdir(args)

    print("[watch] PREVIEW mode — sparse extraction for dashboard marking", file=sys.stderr)
    print(
        "[watch] downloading via yt-dlp…" if is_url(args.source) else "[watch] using local file…",
        file=sys.stderr,
    )
    dl = download(args.source, work / "download")
    video_path = dl["video_path"]

    meta = get_metadata(video_path)
    full_duration = meta["duration_seconds"]

    # Sparse fps: 1 frame per 10 seconds, capped at 60 frames total.
    # Dashboard timeline doesn't need dense thumbs — user picks segments.
    target_count = min(60, max(8, int(full_duration / 10)))
    sparse_fps = target_count / max(full_duration, 1.0)

    print(f"[watch] extracting ~{target_count} sparse frames at {sparse_fps:.3f} fps over {full_duration:.1f}s…", file=sys.stderr)
    frames_list = extract(
        video_path,
        work / "frames",
        fps=sparse_fps,
        resolution=320,  # smaller — they're timeline thumbs, not analysis frames
        max_frames=target_count,
    )

    transcript_segments, transcript_source = _get_transcript(
        dl, video_path, work, args.no_whisper, args.whisper
    )

    info = dl.get("info") or {}

    preview_data = {
        "video_path": str(video_path),
        "duration_seconds": full_duration,
        "duration_ms": int(full_duration * 1000),
        "sparse_frames": [
            {
                "index": i + 1,
                "pts_ms": int(f["timestamp_seconds"] * 1000),
                "path": str(f["path"]),
            }
            for i, f in enumerate(frames_list)
        ],
        "transcript_segments": [
            {
                "start_ms": int(seg["start"] * 1000),
                "end_ms": int(seg["end"] * 1000),
                "text": seg["text"],
            }
            for seg in transcript_segments
        ],
        "transcript_source": transcript_source or "none",
    }
    (work / "preview.json").write_text(json.dumps(preview_data, indent=2, ensure_ascii=False), encoding="utf-8")

    record = {
        "id": work.name,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": args.source,
        "title": info.get("title") if isinstance(info, dict) else None,
        "uploader": info.get("uploader") if isinstance(info, dict) else None,
        "duration_seconds": full_duration,
        "resolution": (
            f"{meta['width']}x{meta['height']}"
            if meta.get("width") and meta.get("height") else None
        ),
        "codec": meta.get("codec"),
        "frames_count": len(frames_list),
        "fps": round(sparse_fps, 3),
        "transcript_source": transcript_source or "none",
        "transcript_segment_count": len(transcript_segments),
        "work_dir": str(work),
        "first_frame_path": str(frames_list[0]["path"]) if frames_list else None,
        "video_path": str(video_path),
        "state": "preview-ready",
    }

    if project_dir is not None:
        _update_manifest_record(project_dir, work.name, record)
        print(f"[watch] preview ready. Open the dashboard to mark segments.", file=sys.stderr)
        print(f"[watch] dashboard: {project_dir / '.watch-cache' / 'dashboard.html'}", file=sys.stderr)
    else:
        print(f"[watch] preview ready at {work}. Set WATCH_PROJECT_DIR for dashboard integration.", file=sys.stderr)

    print(f"PREVIEW_WORK_DIR={work}")
    return 0


# ─── FOCUSED MODE ────────────────────────────────────────────────────────────

def _filter_transcript_to_segments(
    transcript_segments: list[dict],
    must_segments: list[tuple[float, float]],
    skip_segments: list[tuple[float, float]],
) -> list[dict]:
    """Return transcript segments overlapping must-windows but NOT inside skip-windows."""
    out = []
    for seg in transcript_segments:
        seg_start, seg_end = seg["start"], seg["end"]
        in_must = any(seg_end >= ms_start and seg_start <= ms_end for ms_start, ms_end in must_segments) if must_segments else True
        in_skip = any(seg_end >= sk_start and seg_start <= sk_end for sk_start, sk_end in skip_segments)
        if in_must and not in_skip:
            out.append(seg)
    return out


def cmd_focused(args) -> int:
    """Process only marked segments. Dense frames in must-windows, transcript filtered, user review surfaced."""
    work = Path(args.work_dir).expanduser().resolve()
    if not work.exists():
        raise SystemExit(f"work-dir not found: {work}")

    preview_json_path = work / "preview.json"
    if not preview_json_path.exists():
        raise SystemExit(f"preview.json not found in {work} — run /watch --preview first")

    preview = json.loads(preview_json_path.read_text(encoding="utf-8"))
    video_path = preview["video_path"]
    full_duration = preview["duration_seconds"]

    try:
        segments_in = json.loads(args.segments_json) if args.segments_json else []
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--segments-json invalid JSON: {exc}")

    if not isinstance(segments_in, list):
        raise SystemExit("--segments-json must be a JSON array")

    must = [(s["start_ms"] / 1000.0, s["end_ms"] / 1000.0) for s in segments_in if s.get("type") == "must"]
    skip = [(s["start_ms"] / 1000.0, s["end_ms"] / 1000.0) for s in segments_in if s.get("type") == "skip"]

    if not must and not skip:
        raise SystemExit("at least one segment with type='must' or 'skip' is required")

    # If only skip segments are given, default the must range to full video
    if not must and skip:
        must = [(0.0, full_duration)]

    project_dir, _ = _resolve_project_dir(None)

    # Reconstruct transcript segments in seconds for filtering
    transcript_segments = [
        {"start": s["start_ms"] / 1000.0, "end": s["end_ms"] / 1000.0, "text": s["text"]}
        for s in preview.get("transcript_segments", [])
    ]
    filtered_transcript = _filter_transcript_to_segments(transcript_segments, must, skip)

    # Extract dense frames per must segment
    frames_dir = work / "focused-frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    all_focused_frames = []

    for i, (start_s, end_s) in enumerate(must):
        seg_dur = max(0.0, end_s - start_s)
        if seg_dur <= 0:
            continue
        # Dense fps: aim for ~2 frames/sec, cap at 60 frames per segment
        target = min(60, max(4, int(seg_dur * 2)))
        seg_fps = target / seg_dur if seg_dur > 0 else 1.0
        seg_fps = min(seg_fps, MAX_FPS)

        seg_subdir = frames_dir / f"seg_{i + 1:02d}"
        seg_frames = extract(
            video_path,
            seg_subdir,
            fps=seg_fps,
            resolution=args.resolution,
            max_frames=target,
            start_seconds=start_s,
            end_seconds=end_s,
        )
        for f in seg_frames:
            f["segment_index"] = i + 1
            f["segment_start_ms"] = int(start_s * 1000)
            f["segment_end_ms"] = int(end_s * 1000)
        all_focused_frames.extend(seg_frames)

    focused_data = {
        "segments_must": [{"start_ms": int(s * 1000), "end_ms": int(e * 1000)} for s, e in must],
        "segments_skip": [{"start_ms": int(s * 1000), "end_ms": int(e * 1000)} for s, e in skip],
        "segment_intents": {
            f"{s.get('start_ms')}-{s.get('end_ms')}": s.get("intent", "")
            for s in segments_in if s.get("intent")
        },
        "user_review": args.user_review or "",
        "frames_count": len(all_focused_frames),
        "transcript_segment_count": len(filtered_transcript),
        "tokens_estimated": _estimate_tokens(all_focused_frames, filtered_transcript),
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (work / "focused.json").write_text(json.dumps(focused_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Update manifest with focused state
    if project_dir is not None:
        _update_manifest_record(project_dir, work.name, {
            "state": "focused-ready",
            "focused": focused_data,
        })

    # ── Print Markdown report optimized for Claude consumption ────────────────
    print()
    print("# /watch focused: process this video")
    print()
    if args.user_review:
        print("## User context (read this first)")
        print()
        print(f"> {args.user_review}")
        print()

    print("## What you have")
    print()
    print(f"- Frames: {len(all_focused_frames)} (dense, only inside the user's must-windows)")
    print(f"- Transcript segments: {len(filtered_transcript)} (filtered to must-windows, skip-windows excluded)")
    print(f"- Estimated input tokens: ~{focused_data['tokens_estimated']:,}")
    print(f"- Work dir: `{work}`")
    print()

    # Per-segment frames + intent
    print("## Marked segments")
    print()
    for i, (start_s, end_s) in enumerate(must):
        intent = focused_data["segment_intents"].get(f"{int(start_s*1000)}-{int(end_s*1000)}", "")
        print(f"### Segment {i + 1}: {format_time(start_s)} → {format_time(end_s)}")
        if intent:
            print(f"**Intent:** {intent}")
        print()
        seg_frames = [f for f in all_focused_frames if f.get("segment_index") == i + 1]
        for f in seg_frames:
            print(f"- `{f['path']}` (t={format_time(f['timestamp_seconds'])})")
        print()

    if skip:
        print("## Skipped segments (do not process)")
        print()
        for s, e in skip:
            print(f"- {format_time(s)} → {format_time(e)}")
        print()

    print("## Filtered transcript")
    print()
    if filtered_transcript:
        print("```")
        print(format_transcript(filtered_transcript))
        print("```")
    else:
        print("_No transcript content in marked segments._")
    print()

    print("## What to do")
    print()
    print(f"1. **Read each frame path above** with the Read tool (parallel calls within a segment, sequential across segments).")
    print(f"2. **Synthesize** per the SKILL.md NLM-ready output template — use the new structure: `## Spoken content` + `## Visual content` + `## Synthesis`.")
    print(f"3. **Save to two files** in `{work}`:")
    print(f"   - `focused-result.md` — your full analysis (the user reads this in the dashboard)")
    print(f"   - `nlm-summary.md` — the NLM-ready paste version (same content, formatted for the user's NotebookLM topic)")
    print(f"4. **Confirm to the user** in one line: `Focused result + NLM summary saved. Open the dashboard to review and paste.`")
    print()
    print("---")
    print(f"_Work dir: `{work}` — keep until the user has pasted the NLM summary._")

    return 0


def _estimate_tokens(frames: list[dict], transcript: list[dict]) -> int:
    """Rough estimate: ~800 tokens per 512px frame, ~1.3 tokens per word in transcript."""
    frame_tokens = len(frames) * 800
    transcript_words = sum(len(s.get("text", "").split()) for s in transcript)
    transcript_tokens = int(transcript_words * 1.3)
    return frame_tokens + transcript_tokens


# ─── DEFAULT MODE (unchanged from prior behavior) ────────────────────────────

def cmd_default(args) -> int:
    """Original full pipeline. Preserved for backwards compat."""
    max_frames = min(args.max_frames, 100)
    work, project_dir = _resolve_workdir(args)

    print(
        "[watch] downloading via yt-dlp…" if is_url(args.source) else "[watch] using local file…",
        file=sys.stderr,
    )
    dl = download(args.source, work / "download")
    video_path = dl["video_path"]

    meta = get_metadata(video_path)
    full_duration = meta["duration_seconds"]

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)

    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None

    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=max_frames)
    else:
        fps, target = auto_fps(effective_duration, max_frames=max_frames)
    if args.fps is not None:
        fps = min(args.fps, MAX_FPS)
        target = max(1, int(round(fps * effective_duration)))

    scope = (
        f"{format_time(effective_start)}-{format_time(effective_end)} ({effective_duration:.1f}s)"
        if focused else f"full {effective_duration:.1f}s"
    )
    print(f"[watch] extracting ~{target} frames at {fps:.3f} fps over {scope}…", file=sys.stderr)

    frames_list = extract(
        video_path,
        work / "frames",
        fps=fps,
        resolution=args.resolution,
        max_frames=max_frames,
        start_seconds=start_sec,
        end_seconds=end_sec,
    )

    transcript_segments, transcript_source = _get_transcript(
        dl, video_path, work, args.no_whisper, args.whisper
    )
    transcript_text = format_transcript(transcript_segments) if transcript_segments else None
    if focused and transcript_segments:
        transcript_segments = filter_range(transcript_segments, start_sec, end_sec)
        transcript_text = format_transcript(transcript_segments)

    info = dl.get("info") or {}

    print()
    print("# watch: video report")
    print()
    print(f"- **Source:** {args.source}")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        print(f"- **Uploader:** {info['uploader']}")
    print(f"- **Duration:** {format_time(full_duration)} ({full_duration:.1f}s)")
    if focused:
        print(
            f"- **Focus range:** {format_time(effective_start)} → {format_time(effective_end)} "
            f"({effective_duration:.1f}s)"
        )
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown codec'})")
    mode = "focused" if focused else "full"
    print(f"- **Frames:** {len(frames_list)} @ {fps:.3f} fps, {mode} mode (budget {target}, max {max_frames})")
    print(f"- **Frame size:** {args.resolution}px wide")
    if transcript_segments:
        in_range = " in range" if focused else ""
        print(
            f"- **Transcript:** {len(transcript_segments)} segments{in_range} "
            f"(via {transcript_source or 'captions'})"
        )
    else:
        print("- **Transcript:** none available")

    if not focused and full_duration > 600:
        mins = int(full_duration // 60)
        print()
        print(
            f"> **Warning:** This is a {mins}-minute video. Consider using `/watch --preview` "
            "and the dashboard's marker UI for cheaper, more focused processing."
        )

    print()
    print("## Frames")
    print()
    print(f"Frames live at: `{work / 'frames'}`")
    print()
    print(
        "**Read each frame path below with the Read tool to view the image.** "
        "Frames are in chronological order; `t=MM:SS` is the absolute timestamp in the source video."
    )
    print()
    for frame in frames_list:
        print(f"- `{frame['path']}` (t={format_time(frame['timestamp_seconds'])})")

    print()
    print("## Transcript")
    print()
    if transcript_text:
        label = transcript_source or "captions"
        if focused:
            print(f"_Source: {label}. Filtered to {format_time(effective_start)} → {format_time(effective_end)}:_")
        else:
            print(f"_Source: {label}._")
        print()
        print("```")
        print(transcript_text)
        print("```")
    else:
        print(
            "_No transcript available — proceed with frames only. "
            "Captions were missing and the Whisper fallback was unavailable._"
        )

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")

    # Manifest update + dashboard regen
    if project_dir is not None:
        try:
            first_frame_path = str(frames_list[0]["path"]) if frames_list else None
            transcript_preview = ""
            if transcript_segments:
                transcript_preview = " ".join(
                    seg.get("text", "") for seg in transcript_segments[:3]
                )[:240]

            record = {
                "id": work.name,
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": args.source,
                "title": info.get("title") if isinstance(info, dict) else None,
                "uploader": info.get("uploader") if isinstance(info, dict) else None,
                "duration_seconds": full_duration,
                "resolution": (
                    f"{meta['width']}x{meta['height']}"
                    if meta.get("width") and meta.get("height") else None
                ),
                "codec": meta.get("codec"),
                "frames_count": len(frames_list),
                "fps": round(fps, 3),
                "transcript_source": transcript_source or "none",
                "transcript_segment_count": len(transcript_segments) if transcript_segments else 0,
                "transcript_preview": transcript_preview,
                "work_dir": str(work),
                "first_frame_path": first_frame_path,
                "state": "complete",
            }
            _update_manifest_record(project_dir, work.name, record)
        except Exception as exc:
            print(f"[watch] dashboard update failed (non-fatal): {exc}", file=sys.stderr)

    return 0


# ─── Argparse + dispatch ─────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="watch",
        description="Download a video, extract frames, surface transcript. Three modes: default / preview / focused.",
    )
    ap.add_argument("source", nargs="?", default=None, help="Video URL or local file path (default + preview modes)")
    ap.add_argument("--preview", action="store_true", help="Preview mode: sparse frames + transcript only, no Claude")
    ap.add_argument("--focused", action="store_true", help="Focused mode: process only marked segments from a prior preview")
    ap.add_argument("--work-dir", type=str, default=None, help="Existing work directory (focused mode)")
    ap.add_argument("--segments-json", type=str, default=None, help="JSON array of segments (focused mode)")
    ap.add_argument("--user-review", type=str, default=None, help="User context to prepend for Claude (focused mode)")
    ap.add_argument("--max-frames", type=int, default=80, help="Cap on frame count (default 80, hard max 100)")
    ap.add_argument("--resolution", type=int, default=512, help="Frame width in pixels (default 512)")
    ap.add_argument("--fps", type=float, default=None, help="Override auto-fps")
    ap.add_argument("--start", type=str, default=None, help="Range start (default mode)")
    ap.add_argument("--end", type=str, default=None, help="Range end (default mode)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory")
    ap.add_argument("--no-whisper", action="store_true", help="Disable Whisper fallback")
    ap.add_argument("--whisper", choices=["local-whisperx"], default=None, help="Backend selector (compat)")
    args = ap.parse_args()

    if args.focused:
        if not args.work_dir:
            raise SystemExit("--focused requires --work-dir")
        return cmd_focused(args)

    if args.preview:
        if not args.source:
            raise SystemExit("--preview requires a source URL or path")
        return cmd_preview(args)

    if not args.source:
        raise SystemExit("source is required (URL or local path), unless --focused")
    return cmd_default(args)


if __name__ == "__main__":
    raise SystemExit(main())
