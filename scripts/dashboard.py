#!/usr/bin/env python3
"""Manifest + dashboard generation for /watch (daily-use grade).

Each /watch run appends a record to <project_dir>/.watch-cache/index.json
and triggers a regen of dashboard.html. The HTML embeds:
  - the manifest (sortable, searchable, filterable)
  - each video's nlm-summary.md content (if Claude saved one)

User annotations (NLM-paste status, project tag, notes) live in browser
localStorage — the dashboard merges them with the manifest at render time.
Single-machine usage, persistent across browser restarts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_TAGS = [
    "None",
    "MMS",
    "PKC",
    "Amrocky-Migration-Tool",
    "Amrocky-Wedding",
    "Amrocky-Reselling",
    "Sheeltron",
    "ServerSupply",
    "Byrefab",
    "Adhoc",
]


def _rewrite_path(p: str, project_root: Path) -> str:
    """Rewrite sandbox paths so they resolve on the host.

    /watch runs from inside the Cowork sandbox record paths like
    `/sessions/<sid>/mnt/<basename>/.watch-cache/...`. Those paths don't
    exist on the host filesystem (Windows / Claude Code), so the dashboard's
    `<img src="file://…">` tags 404 and `_load_previews` can't find preview.json.

    Strip everything before `.watch-cache/` and prepend the host project root.
    Already-host paths are left alone (the marker is still found, and
    `project_root / .watch-cache / <tail>` is identical to the input).
    """
    if not p:
        return p
    for marker in ("/.watch-cache/", "\\.watch-cache\\"):
        idx = p.find(marker)
        if idx != -1:
            tail = p[idx + len(marker):]
            return str(project_root / ".watch-cache" / tail)
    return p


def _normalize_paths(records: list[dict], project_root: Path) -> list[dict]:
    """Return records with sandbox paths rewritten to host paths."""
    out = []
    for rec in records:
        new_rec = dict(rec)
        for key in ("work_dir", "first_frame_path", "video_path"):
            val = new_rec.get(key)
            if val:
                new_rec[key] = _rewrite_path(str(val), project_root)
        out.append(new_rec)
    return out


def update_manifest(manifest_path: Path, record: dict[str, Any]) -> None:
    """Append `record` to the JSON list at `manifest_path`. Creates if missing."""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except (OSError, json.JSONDecodeError):
            existing = []
    else:
        existing = []
    existing.append(record)
    manifest_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_summaries(records: list[dict]) -> dict[str, str]:
    """For each record, look for <work_dir>/nlm-summary.md and load its content.

    Returns {record_id: markdown_text}. Missing files are silently skipped.
    Cap each summary at ~80 KB to avoid pathological dashboard sizes.
    """
    out: dict[str, str] = {}
    for rec in records:
        rid = rec.get("id")
        work = rec.get("work_dir")
        if not rid or not work:
            continue
        summary_path = Path(work) / "nlm-summary.md"
        if not summary_path.exists():
            continue
        try:
            text = summary_path.read_text(encoding="utf-8", errors="replace")
            if len(text) > 80_000:
                text = text[:80_000] + "\n\n…[truncated]"
            out[rid] = text
        except OSError:
            continue
    return out


def _load_focused_results(records: list[dict]) -> dict[str, str]:
    """Load <work_dir>/focused-result.md for each record. Cap at 80 KB.

    Mirror of _load_summaries; the marker workflow saves both nlm-summary.md
    (NLM-paste version) and focused-result.md (long-form analysis), and the
    dashboard surfaces them via separate modals.
    """
    out: dict[str, str] = {}
    for rec in records:
        rid = rec.get("id")
        work = rec.get("work_dir")
        if not rid or not work:
            continue
        result_path = Path(work) / "focused-result.md"
        if not result_path.exists():
            continue
        try:
            text = result_path.read_text(encoding="utf-8", errors="replace")
            if len(text) > 80_000:
                text = text[:80_000] + "\n\n…[truncated]"
            out[rid] = text
        except OSError:
            continue
    return out


def _load_variant_summaries(records: list[dict]) -> dict[str, dict[str, str]]:
    """For each record, load nlm-summary-variant-a/b.md if present.

    Mirrors _load_summaries but returns {record_id: {slot: text}} so the
    Compare modal can show multiple summaries side-by-side. Same 80 KB cap.
    """
    out: dict[str, dict[str, str]] = {}
    for rec in records:
        rid = rec.get("id")
        work = rec.get("work_dir")
        if not rid or not work:
            continue
        for slot in ("variant-a", "variant-b"):
            p = Path(work) / f"nlm-summary-{slot}.md"
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text) > 80_000:
                    text = text[:80_000] + "\n\n…[truncated]"
                out.setdefault(rid, {})[slot] = text
            except OSError:
                continue
    return out


def _load_variant_focused_results(records: list[dict]) -> dict[str, dict[str, str]]:
    """Slot-suffixed focused-result.md companion to _load_variant_summaries."""
    out: dict[str, dict[str, str]] = {}
    for rec in records:
        rid = rec.get("id")
        work = rec.get("work_dir")
        if not rid or not work:
            continue
        for slot in ("variant-a", "variant-b"):
            p = Path(work) / f"focused-result-{slot}.md"
            if not p.exists():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                if len(text) > 80_000:
                    text = text[:80_000] + "\n\n…[truncated]"
                out.setdefault(rid, {})[slot] = text
            except OSError:
                continue
    return out


def _load_previews(records: list[dict]) -> dict[str, dict]:
    """For preview-ready records, load <work_dir>/preview.json so the marker UI
    can render the timeline strip and transcript without a network round-trip
    (the dashboard is a self-contained file:// page).

    Returns {record_id: preview_dict}. Only loads for records with
    state == "preview-ready" — focused-ready / complete records don't need it.
    """
    out: dict[str, dict] = {}
    for rec in records:
        if rec.get("state") != "preview-ready":
            continue
        rid = rec.get("id")
        work = rec.get("work_dir")
        if not rid or not work:
            continue
        preview_path = Path(work) / "preview.json"
        if not preview_path.exists():
            continue
        try:
            data = json.loads(preview_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out[rid] = data
        except (OSError, json.JSONDecodeError):
            continue
    return out


def render_dashboard(manifest_path: Path, dashboard_path: Path) -> None:
    """Regenerate dashboard.html from the manifest + sibling nlm-summary.md files."""
    if manifest_path.exists():
        try:
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records = []
    else:
        records = []

    if not isinstance(records, list):
        records = []

    project_root = manifest_path.parent.parent  # <project>/.watch-cache/index.json → <project>
    records = _normalize_paths(records, project_root)
    records = sorted(records, key=lambda r: r.get("started_at") or "", reverse=True)
    summaries = _load_summaries(records)
    focused_results = _load_focused_results(records)
    variant_summaries = _load_variant_summaries(records)
    variant_focused_results = _load_variant_focused_results(records)
    previews = _load_previews(records)
    # preview.json content carries its own sandbox paths inside sparse_frames[*].path;
    # rewrite them so the marker timeline's <img> tags resolve on the host.
    previews = {
        rid: {
            **p,
            "video_path": _rewrite_path(str(p.get("video_path", "")), project_root) if p.get("video_path") else p.get("video_path"),
            "sparse_frames": [
                {**f, "path": _rewrite_path(str(f.get("path", "")), project_root)} if f.get("path") else f
                for f in p.get("sparse_frames", [])
            ] if isinstance(p.get("sparse_frames"), list) else p.get("sparse_frames"),
        }
        for rid, p in previews.items()
    }

    # The marker modal needs to know where watch.py lives so it can build the
    # `python <script> --focused ...` command users copy to clipboard.
    script_path = (Path(__file__).parent.resolve() / "watch.py")

    # Forward-slash project root — JS uses this in server mode to strip the
    # absolute prefix from manifest paths and re-root them under /files/*.
    project_root_forward_slash = json.dumps(
        str(project_root).replace("\\", "/"), ensure_ascii=False
    )

    data_json = json.dumps(records, ensure_ascii=False)
    summaries_json = json.dumps(summaries, ensure_ascii=False)
    focused_results_json = json.dumps(focused_results, ensure_ascii=False)
    variant_summaries_json = json.dumps(variant_summaries, ensure_ascii=False)
    variant_focused_results_json = json.dumps(variant_focused_results, ensure_ascii=False)
    previews_json = json.dumps(previews, ensure_ascii=False)
    tags_json = json.dumps(PROJECT_TAGS, ensure_ascii=False)
    script_path_json = json.dumps(str(script_path), ensure_ascii=False)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    html = (
        HTML_TEMPLATE
        .replace("{{DATA}}", data_json)
        .replace("{{SUMMARIES}}", summaries_json)
        .replace("{{FOCUSED_RESULTS}}", focused_results_json)
        .replace("{{VARIANT_SUMMARIES}}", variant_summaries_json)
        .replace("{{VARIANT_FOCUSED_RESULTS}}", variant_focused_results_json)
        .replace("{{PREVIEWS}}", previews_json)
        .replace("{{PROJECT_TAGS}}", tags_json)
        .replace("{{SCRIPT_PATH}}", script_path_json)
        .replace("{{PROJECT_DIR_FORWARD_SLASH}}", project_root_forward_slash)
        .replace("{{GENERATED_AT}}", generated_at)
        .replace("{{COUNT}}", str(len(records)))
    )
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard_path.write_text(html, encoding="utf-8")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>/watch dashboard — Triage Knowledge System</title>
  <style>
    :root {
      --bg: #0f1115; --panel: #161922; --border: #232734;
      --text: #e6e7ea; --muted: #8b8f9b; --accent: #7aa6ff;
      --accent-soft: #2a3552;
      --green: #5fb878; --red: #d56b6b; --amber: #d59f4f;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; height: 100%; }
    body {
      background: var(--bg); color: var(--text);
      font: 13.5px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    }
    .topbar { padding: 14px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: baseline; gap: 16px; }
    .topbar h1 { margin: 0; font-size: 16px; font-weight: 600; }
    .topbar .meta { color: var(--muted); font-size: 11px; }
    .topbar .topbar-btn {
      background: var(--bg); color: var(--muted);
      border: 1px solid var(--border); padding: 5px 12px; border-radius: 4px;
      font: 11px inherit; cursor: pointer;
    }
    .topbar .topbar-btn:hover { color: var(--accent); border-color: var(--accent); }
    .topbar .conn-indicator {
      display: none; margin-left: auto;
      width: 9px; height: 9px; border-radius: 50%; background: var(--muted);
      align-self: center;
    }
    .topbar .conn-indicator.show { display: inline-block; }
    .topbar .conn-indicator.ok { background: var(--green); box-shadow: 0 0 5px rgba(95, 184, 120, 0.6); }
    .topbar .conn-indicator.fail { background: var(--red); box-shadow: 0 0 5px rgba(213, 107, 107, 0.6); }

    .urlbar {
      display: flex; gap: 8px; align-items: center;
      padding: 10px 20px; border-bottom: 0.5px solid var(--border); background: var(--panel);
    }
    .urlbar input[type=url] {
      flex: 1; min-width: 280px;
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 8px 12px; border-radius: 5px; font: 13px inherit;
    }
    .urlbar input[type=url]:focus { outline: 1px solid var(--accent); border-color: var(--accent); }
    .urlbar button {
      background: var(--accent-soft); color: var(--text); border: 1px solid var(--accent);
      padding: 8px 16px; border-radius: 5px; font: 13px inherit; cursor: pointer; font-weight: 500;
    }
    .urlbar button:hover { background: var(--accent); color: var(--bg); }

    .stats { display: flex; gap: 24px; padding: 12px 20px; background: var(--panel); border-bottom: 1px solid var(--border); }
    .stats .stat { display: flex; flex-direction: column; gap: 2px; }
    .stats .num { font-size: 18px; font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }
    .stats .num.green { color: var(--green); }
    .stats .num.amber { color: var(--amber); }
    .stats .num.muted { color: var(--muted); }
    .stats .lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }

    .controls {
      padding: 10px 20px; display: flex; gap: 10px; align-items: center;
      flex-wrap: wrap; border-bottom: 1px solid var(--border);
    }
    .controls input[type=search] {
      flex: 1 1 280px; min-width: 220px; max-width: 420px;
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 7px 10px; border-radius: 5px; font: inherit;
    }
    .controls select, .controls button {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 7px 10px; border-radius: 5px; font: inherit; cursor: pointer;
    }
    .controls button.clear { color: var(--muted); border-style: dashed; }
    .controls button.clear:hover { color: var(--text); border-style: solid; }

    table { width: 100%; border-collapse: collapse; }
    thead th {
      position: sticky; top: 0; z-index: 2;
      background: var(--panel); text-align: left;
      padding: 9px 12px; font-size: 11px; font-weight: 500;
      color: var(--muted); border-bottom: 1px solid var(--border);
      cursor: pointer; user-select: none; white-space: nowrap;
    }
    thead th .arrow { color: var(--accent); margin-left: 4px; }
    tbody tr { border-bottom: 1px solid var(--border); transition: background 80ms; }
    tbody tr:hover { background: rgba(122, 166, 255, 0.04); }
    tbody tr.kbd-active { background: rgba(122, 166, 255, 0.08); }
    td { padding: 10px 12px; vertical-align: top; }

    td.thumb { width: 72px; padding: 8px; }
    td.thumb img {
      width: 56px; height: 56px; object-fit: cover; border-radius: 4px;
      background: var(--panel); border: 1px solid var(--border); display: block; cursor: pointer;
    }
    td.thumb .placeholder {
      width: 56px; height: 56px; border-radius: 4px; background: var(--panel);
      border: 1px dashed var(--border); display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 10px;
    }

    .title { font-weight: 500; color: var(--text); line-height: 1.3; }
    .source { font-size: 11px; color: var(--muted); margin-top: 3px; word-break: break-all; }
    .source a { color: var(--accent); text-decoration: none; }
    .source a:hover { text-decoration: underline; }

    .badge {
      display: inline-block; padding: 1px 7px; border-radius: 3px;
      font-size: 10px; font-weight: 500; border: 1px solid var(--border);
      color: var(--muted); white-space: nowrap;
    }
    .badge.captions { color: var(--green); border-color: rgba(95, 184, 120, 0.3); }
    .badge.local-whisperx { color: var(--accent); border-color: rgba(122, 166, 255, 0.3); }
    .badge.none { color: var(--muted); }
    .badge.complete { color: var(--green); border-color: rgba(95, 184, 120, 0.3); }
    .badge.failed { color: var(--red); border-color: rgba(213, 107, 107, 0.3); }
    .badge.partial { color: var(--amber); border-color: rgba(213, 159, 79, 0.3); }
    .badge.preview-ready { color: var(--amber); border-color: rgba(213, 159, 79, 0.5); background: rgba(213, 159, 79, 0.08); }
    .badge.focused-ready { color: var(--accent); border-color: rgba(122, 166, 255, 0.4); background: rgba(122, 166, 255, 0.08); }
    .badge.pasted { color: var(--green); border-color: rgba(95, 184, 120, 0.3); background: rgba(95, 184, 120, 0.08); }
    .badge.pending { color: var(--amber); border-color: rgba(213, 159, 79, 0.3); }

    .actions button.mark-cta {
      color: var(--amber); font-weight: 600;
    }
    .actions button.mark-cta:hover { color: var(--text); text-decoration: underline; }

    .nlm-cell { text-align: center; }
    .nlm-cell input[type=checkbox] {
      cursor: pointer; width: 16px; height: 16px; accent-color: var(--green);
    }

    .tag-cell select {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 4px 6px; border-radius: 4px; font: 12px inherit; min-width: 110px;
    }
    .tag-cell select.tagged { border-color: var(--accent); color: var(--accent); }

    .note-cell textarea {
      width: 200px; min-height: 32px; max-height: 100px;
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 5px 7px; border-radius: 4px; font: 12px inherit; resize: vertical;
    }
    .note-cell textarea:focus { outline: 1px solid var(--accent); border-color: var(--accent); }

    .actions a, .actions button {
      display: inline-block; color: var(--accent); text-decoration: none;
      font-size: 11px; margin: 0 8px 4px 0; background: none; border: none;
      padding: 0; cursor: pointer; font-family: inherit;
    }
    .actions a:hover, .actions button:hover { text-decoration: underline; }
    .actions button:disabled { color: var(--muted); cursor: default; text-decoration: none; }

    .empty { text-align: center; padding: 64px 24px; color: var(--muted); }
    .empty code { background: var(--panel); border: 1px solid var(--border); padding: 2px 6px; border-radius: 4px; color: var(--text); }

    .duration, .frames, .age { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 11px; }
    .age { white-space: nowrap; }

    /* Modal */
    .modal-bg {
      display: none; position: fixed; inset: 0;
      background: rgba(0,0,0,0.7); z-index: 100;
      align-items: center; justify-content: center; padding: 24px;
    }
    .modal-bg.show { display: flex; }
    .modal {
      background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
      max-width: 880px; max-height: 90vh; width: 100%;
      display: flex; flex-direction: column; overflow: hidden;
    }
    .modal-head { padding: 14px 18px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }
    .modal-head h2 { margin: 0; font-size: 15px; flex: 1; color: var(--text); }
    .modal-head button { background: none; border: 1px solid var(--border); color: var(--text); padding: 5px 10px; border-radius: 4px; cursor: pointer; font: inherit; }
    .modal-head button:hover { border-color: var(--accent); color: var(--accent); }
    .modal-body { flex: 1; overflow: auto; padding: 18px; }
    .modal-body pre {
      background: var(--bg); border: 1px solid var(--border); border-radius: 5px;
      padding: 14px; margin: 0; white-space: pre-wrap; word-break: break-word;
      font: 12.5px/1.55 ui-monospace, "SF Mono", Menlo, Consolas, monospace; color: var(--text);
    }
    .modal-body .empty-msg { color: var(--muted); font-style: italic; }

    .toast {
      position: fixed; bottom: 24px; right: 24px;
      background: var(--panel); border: 1px solid var(--accent); color: var(--text);
      padding: 10px 16px; border-radius: 5px; font-size: 12px;
      opacity: 0; transition: opacity 200ms; pointer-events: none; z-index: 200;
    }
    .toast.show { opacity: 1; }

    .imgmodal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); z-index: 150; cursor: pointer; align-items: center; justify-content: center; }
    .imgmodal-bg.show { display: flex; }
    .imgmodal-bg img { max-width: 90vw; max-height: 90vh; border-radius: 4px; }

    /* Marker modal — segment-marking UI for preview-ready records */
    .marker-modal { max-width: 1280px; }
    .marker-modal .modal-body { padding: 0; }

    /* M/S toolbar between header and body grid */
    .marker-toolbar {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 18px; border-bottom: 1px solid var(--border); background: var(--bg);
    }
    .marker-toolbar-label {
      font-size: 10px; font-weight: 600; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px;
    }
    .marker-mtype {
      width: 28px; height: 28px; padding: 0; border-radius: 4px;
      font: 600 13px ui-monospace, monospace; cursor: pointer;
      display: inline-flex; align-items: center; justify-content: center;
      transition: background 80ms, color 80ms;
    }
    .marker-mtype.must {
      background: transparent; color: var(--amber); border: 1.5px solid var(--amber);
    }
    .marker-mtype.must.active {
      background: rgba(213, 159, 79, 0.2); box-shadow: 0 0 0 2px rgba(213, 159, 79, 0.25);
    }
    .marker-mtype.audio {
      background: transparent; color: var(--accent); border: 1.5px solid var(--accent);
    }
    .marker-mtype.audio.active {
      background: rgba(122, 166, 255, 0.2); box-shadow: 0 0 0 2px rgba(122, 166, 255, 0.25);
    }
    .marker-mtype.exclude {
      background: transparent; color: var(--red); border: 1.5px solid var(--red);
    }
    .marker-mtype.exclude.active {
      background: rgba(213, 107, 107, 0.2); box-shadow: 0 0 0 2px rgba(213, 107, 107, 0.25);
    }
    .marker-mtype:hover { filter: brightness(1.15); }
    .marker-recording {
      flex: 1; font-size: 12px; color: var(--text); font-variant-numeric: tabular-nums;
    }
    .marker-recording.active.must { color: var(--amber); }
    .marker-recording.active.audio { color: var(--accent); }
    .marker-recording.active.exclude { color: var(--red); }
    .marker-toolbar-help {
      padding: 6px 18px 10px; font-size: 11px; color: var(--muted);
      background: var(--bg); border-bottom: 1px solid var(--border);
    }
    .marker-cancel-active {
      width: 22px; height: 22px; padding: 0; border-radius: 11px;
      background: var(--bg); color: var(--muted); border: 1px solid var(--border);
      cursor: pointer; font: 14px inherit; line-height: 1;
    }
    .marker-cancel-active:hover { color: var(--red); border-color: var(--red); }
    .marker-counts {
      font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;
    }

    /* Two-column body grid */
    .marker-body {
      display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
      gap: 0; min-height: 0;
    }
    .marker-left, .marker-right {
      display: flex; flex-direction: column; min-width: 0;
      max-height: calc(90vh - 180px); overflow-y: auto;
    }
    .marker-right { border-left: 1px solid var(--border); }
    @media (max-width: 900px) {
      .marker-body { grid-template-columns: 1fr; }
      .marker-right { border-left: none; border-top: 1px solid var(--border); }
    }

    /* Video player + custom timeline strip with region overlays */
    .marker-video-wrap {
      padding: 14px 18px 6px; border-bottom: 1px solid var(--border);
      display: flex; flex-direction: column; gap: 6px;
    }
    .marker-video-wrap video {
      width: 100%; max-height: 360px; background: #000; border-radius: 4px; outline: none;
    }
    .marker-timeline {
      position: relative; height: 18px; border: 1px solid var(--border);
      border-radius: 3px; background: var(--bg); cursor: pointer; overflow: hidden;
    }
    .marker-timeline-regions { position: absolute; inset: 0; pointer-events: none; }
    .marker-timeline-region {
      position: absolute; top: 0; bottom: 0;
      border-left: 1px solid currentColor; border-right: 1px solid currentColor;
    }
    .marker-timeline-region.must { background: rgba(213, 159, 79, 0.4); color: var(--amber); }
    .marker-timeline-region.audio { background: rgba(122, 166, 255, 0.4); color: var(--accent); }
    .marker-timeline-region.exclude { background: rgba(213, 107, 107, 0.4); color: var(--red); }
    .marker-timeline-region.active {
      border-style: dashed; border-width: 1.5px;
    }
    .marker-timeline-playhead {
      position: absolute; top: -2px; bottom: -2px; width: 2px;
      background: var(--accent); pointer-events: none; left: 0;
      box-shadow: 0 0 4px rgba(122, 166, 255, 0.6);
    }

    /* Sparse frame thumbs — tint backgrounds based on segment containment */
    .marker-frame { background: var(--bg); }
    .marker-frame.in-must { background: rgba(213, 159, 79, 0.15); border-color: rgba(213, 159, 79, 0.4); }
    .marker-frame.in-audio { background: rgba(122, 166, 255, 0.15); border-color: rgba(122, 166, 255, 0.4); }
    .marker-frame.in-exclude { background: rgba(213, 107, 107, 0.15); border-color: rgba(213, 107, 107, 0.4); }
    .marker-frame.in-active { border-style: dashed; }
    .marker-frame.at-playhead { outline: 2px solid var(--accent); outline-offset: 1px; }

    /* Manual entry drawer */
    .marker-manual summary, .marker-transcript-drawer summary {
      cursor: pointer; padding: 6px 0; font-size: 12px; color: var(--muted); user-select: none;
    }
    .marker-manual summary:hover, .marker-transcript-drawer summary:hover { color: var(--text); }
    .marker-manual[open] .marker-controls { margin-top: 6px; }
    .marker-section { padding: 14px 18px; border-bottom: 1px solid var(--border); }
    .marker-section:last-child { border-bottom: none; }
    .marker-section h3 {
      margin: 0 0 8px; font-size: 12px; font-weight: 500; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px;
    }
    .marker-section label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
    .marker-context textarea {
      width: 100%; min-height: 56px; max-height: 200px;
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 8px 10px; border-radius: 5px; font: 13px inherit; resize: vertical;
    }
    .marker-context textarea:focus { outline: 1px solid var(--accent); border-color: var(--accent); }

    .marker-frames {
      display: flex; gap: 4px; overflow-x: auto; padding: 4px 0 8px;
      scroll-snap-type: x proximity;
    }
    .marker-frame {
      flex: 0 0 auto; width: 88px; cursor: pointer; text-align: center;
      border: 2px solid transparent; border-radius: 4px; padding: 2px;
      scroll-snap-align: start; transition: border-color 80ms;
    }
    .marker-frame img {
      width: 84px; height: 56px; object-fit: cover; border-radius: 3px;
      background: var(--panel); display: block;
    }
    .marker-frame .ts { font-size: 10px; color: var(--muted); margin-top: 3px; font-variant-numeric: tabular-nums; }
    .marker-frame:hover { border-color: var(--accent-soft); }
    .marker-frame.start { border-color: var(--green); }
    .marker-frame.end { border-color: var(--red); }
    .marker-frame.in-range { background: rgba(122, 166, 255, 0.06); }

    .marker-controls {
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    }
    .marker-controls input[type=text] {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 6px 9px; border-radius: 4px; font: 12px inherit;
    }
    .marker-controls input.time { width: 80px; font-variant-numeric: tabular-nums; }
    .marker-controls input.intent { flex: 1 1 240px; min-width: 200px; }
    .marker-controls select {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 6px 9px; border-radius: 4px; font: 12px inherit;
    }
    .marker-controls button.add {
      background: var(--accent-soft); color: var(--text); border: 1px solid var(--accent);
      padding: 6px 14px; border-radius: 4px; font: 12px inherit; cursor: pointer;
    }
    .marker-controls button.add:hover { background: var(--accent); color: var(--bg); }

    .marker-segments-section h3 { display: flex; align-items: baseline; gap: 8px; }
    .marker-coverage { margin-left: auto; font-size: 10px; color: var(--muted); font-weight: 400; text-transform: none; letter-spacing: normal; }
    .marker-segments-list { list-style: none; padding: 0; margin: 0; }
    .marker-segments-list li {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr) auto;
      grid-template-areas: "badge range actions" "badge intent intent";
      column-gap: 8px; row-gap: 2px;
      padding: 6px 8px; border: 1px solid var(--border); border-radius: 4px;
      margin-bottom: 4px; font-size: 12px; background: var(--bg);
    }
    .marker-segments-list li.active { border-style: dashed; border-color: var(--accent); }
    .marker-segments-list .seg-badge {
      grid-area: badge;
      width: 22px; height: 22px; border-radius: 4px;
      display: inline-flex; align-items: center; justify-content: center;
      font: 600 11px ui-monospace, monospace;
    }
    .marker-segments-list .seg-badge.must {
      background: rgba(213, 159, 79, 0.15); color: var(--amber); border: 1px solid var(--amber);
    }
    .marker-segments-list .seg-badge.audio {
      background: rgba(122, 166, 255, 0.15); color: var(--accent); border: 1px solid var(--accent);
    }
    .marker-segments-list .seg-badge.exclude {
      background: rgba(213, 107, 107, 0.15); color: var(--red); border: 1px solid var(--red);
    }
    .marker-segments-list li.active .seg-badge { border-style: dashed; }
    .marker-segments-list .seg-range {
      grid-area: range; display: flex; align-items: center; gap: 8px;
      color: var(--text); font-variant-numeric: tabular-nums;
    }
    .marker-segments-list .seg-duration { color: var(--muted); font-size: 10px; }
    .marker-segments-list .seg-intent {
      grid-area: intent; color: var(--muted); word-break: break-word; font-size: 11px;
    }
    .marker-segments-list .seg-actions { grid-area: actions; display: flex; gap: 4px; }
    .marker-segments-list button.icon {
      background: none; border: 1px solid var(--border); color: var(--muted);
      padding: 2px 6px; border-radius: 3px; cursor: pointer; font: 11px inherit; line-height: 1.2;
    }
    .marker-segments-list button.icon:hover { color: var(--accent); border-color: var(--accent); }
    .marker-segments-list button.icon.del:hover { color: var(--red); border-color: var(--red); }
    .marker-segments-list .seg-active-hint { color: var(--accent); font-size: 11px; font-style: italic; }
    .marker-segments-list .seg-edit {
      grid-column: 1 / -1; padding-top: 6px; display: flex; gap: 6px; flex-wrap: wrap;
    }
    .marker-segments-list .seg-edit input {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 4px 7px; border-radius: 3px; font: 11px inherit;
    }
    .marker-segments-list .seg-edit input.time { width: 70px; font-variant-numeric: tabular-nums; }
    .marker-segments-list .seg-edit input.intent { flex: 1; min-width: 120px; }
    .marker-segments-list .seg-edit button { font: 11px inherit; padding: 4px 10px; border-radius: 3px; cursor: pointer; }
    .marker-segments-list .seg-edit button.save { background: var(--accent); color: var(--bg); border: 1px solid var(--accent); }
    .marker-segments-list .seg-edit button.cancel { background: var(--bg); color: var(--muted); border: 1px solid var(--border); }
    .marker-segments-list .seg-active-intent {
      grid-area: intent; padding-top: 2px;
    }
    .marker-segments-list .seg-active-intent input {
      width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 4px 7px; border-radius: 3px; font: 11px inherit;
    }
    .marker-segments-empty { color: var(--muted); font-style: italic; font-size: 12px; padding: 8px 0; }

    /* Token estimator card — visible savings even at zero markers */
    .marker-token-card .token-big {
      font-size: 28px; font-weight: 600; color: var(--text);
      font-variant-numeric: tabular-nums; line-height: 1.1;
    }
    .marker-token-card .token-sub { font-size: 11px; color: var(--green); margin-top: 2px; }
    .marker-token-card .token-sub.full { color: var(--muted); }
    .marker-token-card .token-bar {
      margin-top: 10px; height: 6px; background: var(--bg);
      border: 1px solid var(--border); border-radius: 3px; overflow: hidden;
    }
    .marker-token-card .token-bar-fill {
      height: 100%; background: linear-gradient(90deg, var(--green), var(--accent));
      width: 0%; transition: width 200ms;
    }

    /* Job progress modal — surfaced when the dashboard runs in server mode */
    .job-modal { max-width: 760px; }
    .job-modal .modal-head { gap: 10px; }
    .job-status {
      font-size: 11px; padding: 2px 9px; border-radius: 3px;
      border: 1px solid var(--border); color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500;
    }
    .job-status.running { color: var(--amber); border-color: var(--amber); }
    .job-status.done { color: var(--green); border-color: var(--green); background: rgba(95, 184, 120, 0.08); }
    .job-status.failed { color: var(--red); border-color: var(--red); }
    .job-status.queued { color: var(--muted); }
    .job-cancel {
      background: var(--bg); color: var(--muted); border: 1px solid var(--border);
      padding: 4px 10px; border-radius: 3px; cursor: pointer; font: 11px inherit;
    }
    .job-cancel:hover { color: var(--red); border-color: var(--red); }
    .job-detail { color: var(--muted); margin: 0 0 10px; font-size: 12px; }
    .job-log {
      max-height: 320px; overflow-y: auto;
      background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
      padding: 10px; margin: 0;
      font: 11.5px/1.5 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      color: var(--text); white-space: pre-wrap; word-break: break-word;
    }
    /* Indeterminate progress bar — runs while extracting / synthesizing */
    .job-progress {
      height: 4px; background: var(--bg); border-radius: 2px;
      margin: 0 0 10px; overflow: hidden; position: relative;
      border: 1px solid var(--border);
    }
    .job-progress-fill {
      position: absolute; top: 0; bottom: 0; width: 30%;
      background: linear-gradient(90deg, transparent, var(--accent), transparent);
      animation: job-progress-slide 1.6s linear infinite;
    }
    @keyframes job-progress-slide {
      0% { left: -30%; }
      100% { left: 100%; }
    }

    .job-log-tabs {
      display: none; gap: 0; margin-bottom: 0;
      border-bottom: 1px solid var(--border);
    }
    .job-log-tabs.show { display: flex; }
    .job-log-tabs .tab {
      background: none; border: none; color: var(--muted); cursor: pointer;
      padding: 6px 14px; font: 11px inherit; font-weight: 500;
      text-transform: uppercase; letter-spacing: 0.5px;
      border-bottom: 2px solid transparent; margin-bottom: -1px;
    }
    .job-log-tabs .tab:hover { color: var(--text); }
    .job-log-tabs .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

    .job-actions { display: none; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
    .job-actions.show { display: flex; }

    /* Auto-synthesize toggle in marker modal foot */
    .auto-synth-toggle {
      display: none; align-items: center; gap: 6px;
      font-size: 11px; color: var(--muted); cursor: pointer; user-select: none;
      margin-right: 6px;
    }
    .auto-synth-toggle.show { display: inline-flex; }
    .auto-synth-toggle input[type=checkbox] {
      width: 14px; height: 14px; cursor: pointer; accent-color: var(--accent);
    }
    .auto-synth-toggle input[type=checkbox]:disabled { cursor: not-allowed; opacity: 0.5; }
    .auto-synth-toggle.disabled { color: var(--border); cursor: not-allowed; }
    .job-actions button {
      background: var(--accent-soft); color: var(--text); border: 1px solid var(--accent);
      padding: 7px 14px; border-radius: 4px; font: 12px inherit; cursor: pointer;
    }
    .job-actions button.primary { background: var(--accent); color: var(--bg); font-weight: 600; }
    .job-actions button:hover { filter: brightness(1.1); }

    .marker-transcript-drawer summary {
      cursor: pointer; padding: 6px 0; font-size: 12px; color: var(--muted); user-select: none;
    }
    .marker-transcript-drawer summary:hover { color: var(--text); }
    .marker-transcript-drawer .seg {
      display: block; padding: 4px 8px; border-radius: 3px; cursor: pointer;
      font-size: 12px; line-height: 1.45; margin-bottom: 2px;
    }
    .marker-transcript-drawer .seg:hover { background: var(--accent-soft); }
    .marker-transcript-drawer .seg .ts {
      color: var(--accent); font-family: ui-monospace, monospace; font-size: 11px; margin-right: 8px;
    }

    .modal-foot {
      padding: 12px 18px; border-top: 1px solid var(--border);
      display: flex; gap: 10px; justify-content: flex-end; align-items: center;
    }
    .modal-foot .estimate { color: var(--muted); font-size: 11px; margin-right: auto; }
    .modal-foot button.primary {
      background: var(--accent); color: var(--bg); border: none;
      padding: 8px 16px; border-radius: 4px; font: 13px inherit; cursor: pointer; font-weight: 600;
    }
    .modal-foot button.primary:hover { filter: brightness(1.1); }
    .modal-foot button.primary:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; filter: none; }
    .modal-foot button.link {
      background: none; border: none; color: var(--muted); font: 11px inherit;
      cursor: pointer; padding: 0; text-decoration: underline; margin-right: auto;
    }
    .modal-foot button.link:hover { color: var(--red); }

    /* Manage projects modal */
    .projects-modal { max-width: 480px; }
    .projects-help { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
    .projects-list { list-style: none; padding: 0; margin: 0 0 16px; }
    .projects-list li {
      display: flex; align-items: center; gap: 8px; padding: 6px 10px;
      border: 1px solid var(--border); border-radius: 4px; margin-bottom: 4px;
      background: var(--bg); font-size: 13px;
    }
    .projects-list .proj-name { flex: 1; color: var(--text); }
    .projects-list .proj-name.editing { padding: 0; }
    .projects-list .proj-name input {
      width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--accent);
      padding: 4px 7px; border-radius: 3px; font: inherit;
    }
    .projects-list button.icon {
      background: none; border: 1px solid var(--border); color: var(--muted);
      padding: 3px 8px; border-radius: 3px; cursor: pointer; font: 11px inherit;
    }
    .projects-list button.icon:hover { color: var(--accent); border-color: var(--accent); }
    .projects-list button.icon.del:hover { color: var(--red); border-color: var(--red); }
    .projects-list .proj-locked { color: var(--muted); font-style: italic; font-size: 11px; }
    .projects-add { display: flex; gap: 8px; }
    .projects-add input {
      flex: 1; background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 7px 10px; border-radius: 4px; font: 13px inherit;
    }
    .projects-add input:focus { outline: 1px solid var(--accent); border-color: var(--accent); }
    .projects-add button {
      background: var(--accent-soft); color: var(--text); border: 1px solid var(--accent);
      padding: 7px 14px; border-radius: 4px; font: 13px inherit; cursor: pointer;
    }
    .projects-add button:hover { background: var(--accent); color: var(--bg); }

    /* Phase 7: server-only cells (select column) hidden in static mode */
    html.static-mode .server-only-col { display: none !important; }

    /* Phase 7: bulk-select bar (server mode only) */
    .bulk-bar {
      display: none; align-items: center; gap: 12px;
      padding: 10px 20px; background: rgba(122, 166, 255, 0.08);
      border-bottom: 1px solid var(--accent); font-size: 13px;
      position: sticky; top: 0; z-index: 5;
    }
    .bulk-bar.show { display: flex; }
    .bulk-bar .count { color: var(--accent); font-weight: 600; font-variant-numeric: tabular-nums; }
    .bulk-bar button {
      background: var(--bg); color: var(--text); border: 1px solid var(--border);
      padding: 5px 12px; border-radius: 4px; cursor: pointer; font: 12px inherit;
    }
    .bulk-bar button:hover { border-color: var(--accent); }
    .bulk-bar button.danger {
      background: var(--red); color: var(--bg); border-color: var(--red); margin-left: auto;
    }
    .bulk-bar button.danger:hover { filter: brightness(1.15); }

    /* Select column */
    td.select-cell, th.select-cell {
      width: 36px; text-align: center; padding: 8px;
    }
    .select-cell input[type=checkbox] {
      width: 14px; height: 14px; cursor: pointer; accent-color: var(--accent);
    }

    /* Per-row delete button */
    .actions button.del-btn {
      color: var(--muted); border: 1px solid var(--border);
      background: var(--bg); padding: 2px 9px; border-radius: 3px;
      cursor: pointer; font: 11px inherit;
    }
    .actions button.del-btn:hover { color: var(--red); border-color: var(--red); }

    /* Cache stat (5th stat in topbar) */
    .stats .stat.cache-stat { cursor: pointer; }
    .stats .stat.cache-stat:hover .num { color: var(--accent); }
    .stats .stat.cache-stat.disabled { cursor: not-allowed; }
    .stats .stat.cache-stat.disabled:hover .num { color: var(--muted); }

    /* Confirm-delete modal — bumped above other modals so it stacks correctly
       when opened from inside another modal (e.g., orphan cleanup). */
    #confirm-modal-bg { z-index: 110; }
    .confirm-modal { max-width: 460px; }
    .confirm-modal .modal-body p { margin: 0 0 10px; line-height: 1.5; }
    .confirm-detail {
      background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
      padding: 10px 12px; margin: 10px 0; font-size: 12px;
    }
    .confirm-detail .row { margin-bottom: 4px; }
    .confirm-detail .row:last-child { margin-bottom: 0; }
    .confirm-detail .label {
      color: var(--muted); font-size: 10px; text-transform: uppercase;
      letter-spacing: 0.5px; margin-right: 6px;
    }
    .confirm-detail .value {
      color: var(--text); font-family: ui-monospace, monospace; font-size: 11px;
      word-break: break-all;
    }
    .confirm-warning { color: var(--red); font-size: 12px; }
    .modal-foot button.danger {
      background: var(--red); color: var(--bg); border: 1px solid var(--red);
      padding: 7px 14px; border-radius: 4px; font: 13px inherit;
      cursor: pointer; font-weight: 500;
    }
    .modal-foot button.danger:hover { filter: brightness(1.15); }
    .modal-foot button.secondary {
      background: var(--bg); color: var(--muted); border: 1px solid var(--border);
      padding: 7px 14px; border-radius: 4px; font: 13px inherit; cursor: pointer;
    }
    .modal-foot button.secondary:hover { color: var(--text); border-color: var(--accent); }

    /* Orphans modal */
    .orphans-modal { max-width: 640px; }
    .orphans-help { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
    .orphans-list {
      list-style: none; padding: 0; margin: 0 0 12px;
      max-height: 380px; overflow-y: auto;
    }
    .orphans-list li {
      display: grid;
      grid-template-columns: 24px 1fr auto;
      gap: 10px; padding: 8px 10px;
      border: 1px solid var(--border); border-radius: 4px;
      margin-bottom: 4px; font-size: 12px; background: var(--bg);
      align-items: center;
    }
    .orphans-list .orphan-info .name {
      color: var(--text); font-family: ui-monospace, monospace; font-size: 12px;
    }
    .orphans-list .orphan-info .meta {
      color: var(--muted); font-size: 10px; margin-top: 2px;
    }
    .orphans-list .orphan-size {
      color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums;
    }
    .orphans-summary { color: var(--muted); font-size: 11px; margin: 8px 0; }
    .orphans-empty {
      color: var(--muted); font-style: italic; padding: 24px; text-align: center;
    }

    /* Disk-breakdown modal */
    .disk-modal { max-width: 720px; }
    .disk-summary { color: var(--muted); font-size: 12px; margin: 0 0 14px; line-height: 1.5; }
    .disk-table { width: 100%; border-collapse: collapse; font-size: 12px; }
    .disk-table th {
      text-align: left; padding: 6px 8px; color: var(--muted);
      border-bottom: 1px solid var(--border); font-weight: 500;
    }
    .disk-table td {
      padding: 6px 8px; border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    .disk-table tr { cursor: pointer; }
    .disk-table tr:hover td { background: rgba(122, 166, 255, 0.05); }
    .disk-table td.size {
      text-align: right; font-variant-numeric: tabular-nums;
      white-space: nowrap; color: var(--text);
    }
    .disk-table td.id-col {
      color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px;
    }

    /* Phase 11: variant slots — Send-button popover + Compare modal */
    .marker-send-group {
      position: relative; display: inline-flex; gap: 4px; align-items: center;
    }
    .marker-variant-caret {
      background: var(--accent); color: var(--bg); border: none;
      padding: 8px 10px; border-radius: 4px; font: 13px inherit; cursor: pointer;
      font-weight: 600;
    }
    .marker-variant-caret:hover { filter: brightness(1.1); }
    .marker-variant-caret[hidden] { display: none; }
    .marker-variant-menu {
      position: absolute; bottom: calc(100% + 4px); right: 0; z-index: 105;
      background: var(--panel); border: 1px solid var(--accent);
      border-radius: 5px; padding: 4px; display: flex; flex-direction: column;
      gap: 2px; min-width: 180px; box-shadow: 0 4px 12px rgba(0,0,0,0.5);
    }
    .marker-variant-menu[hidden] { display: none; }
    .marker-variant-menu button {
      background: none; border: none; color: var(--text); padding: 7px 12px;
      text-align: left; cursor: pointer; font: 12px inherit; border-radius: 3px;
    }
    .marker-variant-menu button:hover { background: var(--accent-soft); }
    .marker-variant-menu .variant-menu-hint {
      padding: 4px 12px 2px; font-size: 10px; color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px;
    }

    .actions button.compare-btn { color: var(--accent); font-weight: 500; }
    .actions button.compare-btn:hover { text-decoration: underline; }
    .actions .variant-count {
      font-size: 10px; color: var(--muted); margin-right: 4px;
    }

    .compare-modal { max-width: 95vw; width: 95vw; max-height: 92vh; }
    .compare-modal .modal-head { gap: 14px; flex-wrap: wrap; }
    .compare-modal .modal-body { padding: 14px; }
    .compare-toggle {
      font-size: 11px; color: var(--muted); display: inline-flex;
      align-items: center; gap: 6px; cursor: pointer; user-select: none;
    }
    .compare-toggle input[type=checkbox] {
      width: 14px; height: 14px; accent-color: var(--accent); cursor: pointer;
    }
    .compare-columns {
      display: grid; gap: 12px; height: 100%;
    }
    .compare-columns.cols-1 { grid-template-columns: 1fr; }
    .compare-columns.cols-2 { grid-template-columns: 1fr 1fr; }
    .compare-columns.cols-3 { grid-template-columns: 1fr 1fr 1fr; }
    .compare-col {
      display: flex; flex-direction: column; min-width: 0;
      border: 1px solid var(--border); border-radius: 6px;
      background: var(--panel); max-height: calc(92vh - 140px);
    }
    .compare-col-head {
      padding: 10px 12px; border-bottom: 1px solid var(--border);
      display: flex; flex-direction: column; gap: 3px;
    }
    .compare-col-name { font-weight: 600; color: var(--text); font-size: 13px; }
    .compare-badge {
      font-size: 11px; color: var(--muted); font-family: ui-monospace, monospace;
    }
    .compare-col-body {
      flex: 1; overflow-y: auto; padding: 10px 14px; margin: 0;
      background: var(--bg); border: none;
      font: 12.5px/1.55 ui-monospace, Menlo, Consolas, monospace; color: var(--text);
      white-space: pre-wrap; word-break: break-word;
    }
    .compare-col-body.empty { color: var(--muted); font-style: italic; }
    .compare-col-foot {
      padding: 8px 12px; border-top: 1px solid var(--border);
      display: flex; gap: 8px; justify-content: flex-end;
    }
    .compare-col-foot button {
      background: var(--bg); color: var(--accent); border: 1px solid var(--border);
      padding: 5px 12px; border-radius: 4px; cursor: pointer; font: 11px inherit;
    }
    .compare-col-foot button:hover { border-color: var(--accent); }
    .compare-col-foot button.del { color: var(--muted); }
    .compare-col-foot button.del:hover { color: var(--red); border-color: var(--red); }
    @media (max-width: 1100px) {
      .compare-columns.cols-3 { grid-template-columns: 1fr 1fr; }
      .compare-columns.cols-3 .compare-col:last-child { grid-column: 1 / -1; }
    }
    @media (max-width: 700px) {
      .compare-columns.cols-2, .compare-columns.cols-3 { grid-template-columns: 1fr; }
      .compare-columns.cols-3 .compare-col:last-child { grid-column: auto; }
    }

    /* Phase 14: marker-modal meta strip — creator description + chapters */
    .marker-meta {
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
      padding: 8px 18px; border-bottom: 1px solid var(--border); background: var(--bg);
    }
    .marker-meta[hidden] { display: none; }
    .marker-creator-intent { flex: 1 1 auto; min-width: 0; }
    .marker-creator-intent summary {
      cursor: pointer; padding: 4px 0; font-size: 11px; color: var(--muted);
      user-select: none; text-transform: uppercase; letter-spacing: 0.5px;
      font-weight: 500;
    }
    .marker-creator-intent summary:hover { color: var(--accent); }
    .marker-creator-intent-body {
      margin: 6px 0 0; padding: 8px 10px; max-height: 180px; overflow-y: auto;
      background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
      white-space: pre-wrap; word-break: break-word;
      font: 12px/1.5 -apple-system, sans-serif; color: var(--text);
    }
    .marker-chapter-btn {
      background: var(--accent-soft); color: var(--text); border: 1px solid var(--accent);
      padding: 6px 12px; border-radius: 4px; font: 11px inherit; cursor: pointer;
      font-weight: 500; white-space: nowrap;
    }
    .marker-chapter-btn:hover { background: var(--accent); color: var(--bg); }
    .marker-chapter-btn[hidden] { display: none; }
    .marker-chapter-count {
      font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums;
    }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>/watch dashboard</h1>
    <span class="meta">regenerated {{GENERATED_AT}}</span>
    <span class="conn-indicator" id="conn-indicator" title="Server connection status"></span>
    <button id="manage-projects-btn" class="topbar-btn" title="Add, rename, or delete project tags">Manage projects</button>
  </div>

  <div class="urlbar">
    <input id="url-input" type="url" placeholder="Paste video URL — generates the /watch --preview command for you to paste in your terminal" autocomplete="off">
    <button id="url-add-btn">+ Add video</button>
  </div>

  <div class="stats" id="stats"></div>

  <div class="controls">
    <input id="q" type="search" placeholder="Filter by title, uploader, source, transcript, notes…  ( / to focus )" autofocus>
    <select id="filter-transcript">
      <option value="">All transcripts</option>
      <option value="captions">Captions</option>
      <option value="local-whisperx">WhisperX</option>
      <option value="none">No transcript</option>
    </select>
    <select id="filter-status">
      <option value="">All statuses</option>
      <option value="preview-ready">Preview-ready (needs marking)</option>
      <option value="focused-ready">Focused-ready</option>
      <option value="complete">Complete</option>
      <option value="partial">Partial</option>
      <option value="failed">Failed</option>
    </select>
    <select id="filter-nlm">
      <option value="">All NLM</option>
      <option value="pasted">Pasted</option>
      <option value="pending">Pending</option>
    </select>
    <select id="filter-project">
      <option value="">All projects</option>
    </select>
    <button class="clear" id="clear-filters">Clear filters</button>
  </div>

  <div class="bulk-bar" id="bulk-bar">
    <span class="count" id="bulk-count">0 selected</span>
    <button id="bulk-clear">Clear</button>
    <button id="bulk-delete-btn" class="danger">Delete selected</button>
  </div>

  <table id="runs">
    <thead>
      <tr>
        <th class="select-cell server-only-col"><input type="checkbox" id="select-all-rows" title="Select all (filtered)"></th>
        <th data-sort="started_at" class="active">Watched <span class="arrow">▼</span></th>
        <th data-sort="title">Title</th>
        <th></th>
        <th data-sort="duration_seconds">Dur</th>
        <th data-sort="frames_count">Frames</th>
        <th data-sort="transcript_source">Transcript</th>
        <th data-sort="status">Status</th>
        <th data-sort="project_tag">Project</th>
        <th data-sort="nlm_pasted">NLM</th>
        <th>Notes</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" class="empty" style="display:none;">
    No runs yet. Run <code>/watch &lt;url-or-path&gt;</code> in Claude Code or Cowork to populate this dashboard.
  </div>

  <!-- NLM summary modal -->
  <div class="modal-bg" id="nlm-modal-bg">
    <div class="modal">
      <div class="modal-head">
        <h2 id="nlm-modal-title">NLM Summary</h2>
        <button id="nlm-copy">Copy</button>
        <button id="nlm-close">Close</button>
      </div>
      <div class="modal-body">
        <pre id="nlm-modal-body"></pre>
      </div>
    </div>
  </div>

  <!-- Image preview modal -->
  <div class="imgmodal-bg" id="img-modal-bg"><img id="img-modal" alt=""></div>

  <!-- Marker modal — segment marking for preview-ready records -->
  <div class="modal-bg" id="marker-modal-bg">
    <div class="modal marker-modal">
      <div class="modal-head">
        <h2 id="marker-title">Mark segments</h2>
        <button id="marker-close">Close</button>
      </div>

      <div class="marker-toolbar">
        <span class="marker-toolbar-label">DROP MARKER</span>
        <button id="marker-m-btn" class="marker-mtype must" title="Add visual frames here (Claude will see this part)">M</button>
        <button id="marker-a-btn" class="marker-mtype audio" title="Audio-only here (transcript yes, no frames) — same as default, just explicit">A</button>
        <button id="marker-x-btn" class="marker-mtype exclude" title="Exclude this range entirely (no transcript, no frames)">X</button>
        <span class="marker-recording" id="marker-recording"></span>
        <button id="marker-cancel-active" class="marker-cancel-active" title="Discard the open segment" hidden>×</button>
        <span class="marker-counts" id="marker-counts">No segments yet</span>
      </div>
      <div class="marker-toolbar-help">
        Default: transcript everywhere, no frames. M adds frames where visuals matter. X drops a range entirely.
      </div>

      <!-- Phase 14: creator-supplied framing (description + chapters) -->
      <div class="marker-meta" id="marker-meta" hidden>
        <details class="marker-creator-intent" id="marker-creator-intent-details" hidden>
          <summary>Creator's stated intent (from YouTube description)</summary>
          <div class="marker-creator-intent-body" id="marker-creator-intent-body"></div>
        </details>
        <button id="marker-apply-chapters" class="marker-chapter-btn" hidden>
          Suggested markers from chapters
        </button>
        <span class="marker-chapter-count" id="marker-chapter-count"></span>
      </div>

      <div class="modal-body marker-body">
        <div class="marker-left">
          <div class="marker-video-wrap">
            <video id="marker-video" controls preload="metadata"></video>
            <div class="marker-timeline" id="marker-timeline" title="Click anywhere to seek">
              <div class="marker-timeline-regions" id="marker-timeline-regions"></div>
              <div class="marker-timeline-playhead" id="marker-timeline-playhead"></div>
            </div>
          </div>

          <div class="marker-section">
            <h3>Sparse frames</h3>
            <div id="marker-frames" class="marker-frames"></div>
          </div>

          <div class="marker-section">
            <details class="marker-manual">
              <summary>Manual entry — set range with MM:SS</summary>
              <div class="marker-controls">
                <span>Range:</span>
                <input id="marker-start" class="time" type="text" placeholder="MM:SS">
                <span>→</span>
                <input id="marker-end" class="time" type="text" placeholder="MM:SS">
                <select id="marker-type">
                  <option value="must">must (frames)</option>
                  <option value="audio_only">audio-only</option>
                  <option value="exclude">exclude</option>
                </select>
                <input id="marker-intent" class="intent" type="text" placeholder="intent (optional)">
                <button id="marker-add" class="add">Add segment</button>
              </div>
            </details>
          </div>

          <div class="marker-section">
            <details class="marker-transcript-drawer">
              <summary>Transcript (click a line to seek the video)</summary>
              <div id="marker-transcript"></div>
            </details>
          </div>
        </div>

        <div class="marker-right">
          <div class="marker-section marker-segments-section">
            <h3>
              <span id="marker-segments-header">Segments</span>
              <span class="marker-coverage" id="marker-coverage"></span>
            </h3>
            <ul id="marker-segments-list" class="marker-segments-list"></ul>
          </div>

          <div class="marker-section marker-token-card">
            <h3>Estimated tokens</h3>
            <div class="token-big" id="marker-token-big">—</div>
            <div class="token-sub" id="marker-token-sub">Open the modal to compute</div>
            <div class="token-bar"><div class="token-bar-fill" id="marker-token-bar"></div></div>
          </div>

          <div class="marker-section marker-context">
            <label for="marker-review">User context (passed to Claude as <code>--user-review</code>)</label>
            <textarea id="marker-review" placeholder="What are you trying to learn from this video? What pattern, claim, or moment matters?"></textarea>
          </div>
        </div>
      </div>

      <div class="modal-foot">
        <span class="estimate" id="marker-estimate">No segments yet</span>
        <label class="auto-synth-toggle" id="auto-synth-toggle" title="Auto-synthesize via Claude Code after extraction">
          <input type="checkbox" id="auto-synth-checkbox" checked>
          <span>Auto-synthesize</span>
        </label>
        <div class="marker-send-group">
          <button id="marker-copy" class="primary">Copy /watch --focused command</button>
          <button id="marker-variant-btn" class="marker-variant-caret" title="Save as a variant slot (A/B comparison)" hidden>▾</button>
          <div class="marker-variant-menu" id="marker-variant-menu" hidden>
            <div class="variant-menu-hint">Save to slot</div>
            <button data-variant="variant-a">Save as Variant A</button>
            <button data-variant="variant-b">Save as Variant B</button>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Compare-variants modal (Phase 11) -->
  <div class="modal-bg" id="compare-modal-bg">
    <div class="modal compare-modal">
      <div class="modal-head">
        <h2 id="compare-modal-title">Compare variants</h2>
        <label class="compare-toggle">
          <input type="checkbox" id="compare-show-result">
          Show focused-result instead of NLM summary
        </label>
        <button id="compare-close">Close</button>
      </div>
      <div class="modal-body">
        <div class="compare-columns" id="compare-columns"></div>
      </div>
    </div>
  </div>

  <!-- Result (focused-result.md) modal -->
  <div class="modal-bg" id="result-modal-bg">
    <div class="modal">
      <div class="modal-head">
        <h2 id="result-modal-title">Focused result</h2>
        <button id="result-copy">Copy</button>
        <button id="result-close">Close</button>
      </div>
      <div class="modal-body">
        <pre id="result-modal-body"></pre>
      </div>
    </div>
  </div>

  <!-- Manage projects modal -->
  <div class="modal-bg" id="projects-modal-bg">
    <div class="modal projects-modal">
      <div class="modal-head">
        <h2>Manage projects</h2>
        <button id="projects-close">Close</button>
      </div>
      <div class="modal-body">
        <p class="projects-help">Project tags shown in the <em>Project</em> column. Renaming or deleting updates every row tagged with that project.</p>
        <ul id="projects-list" class="projects-list"></ul>
        <div class="projects-add">
          <input id="projects-add-input" type="text" placeholder="New project name (≤32 chars)" maxlength="32">
          <button id="projects-add-btn">Add project</button>
        </div>
      </div>
      <div class="modal-foot">
        <button id="projects-reset" class="link">Reset to defaults</button>
        <button id="orphans-open-btn" class="secondary" title="Server required">Cleanup orphans…</button>
      </div>
    </div>
  </div>

  <!-- Confirm delete modal (per-row, bulk, and orphan deletes share this) -->
  <div class="modal-bg" id="confirm-modal-bg">
    <div class="modal confirm-modal">
      <div class="modal-head">
        <h2 id="confirm-modal-title">Delete?</h2>
        <button id="confirm-modal-close">Close</button>
      </div>
      <div class="modal-body">
        <p id="confirm-modal-message"></p>
        <div id="confirm-modal-detail" class="confirm-detail" style="display:none"></div>
        <p class="confirm-warning">This will permanently delete the video, frames, transcript, and any saved analysis. Cannot be undone.</p>
      </div>
      <div class="modal-foot">
        <button id="confirm-modal-cancel" class="secondary">Cancel</button>
        <button id="confirm-modal-confirm" class="danger">Delete</button>
      </div>
    </div>
  </div>

  <!-- Orphan-cleanup modal -->
  <div class="modal-bg" id="orphans-modal-bg">
    <div class="modal orphans-modal">
      <div class="modal-head">
        <h2>Cleanup orphans</h2>
        <button id="orphans-close">Close</button>
      </div>
      <div class="modal-body">
        <p class="orphans-help">Work-dirs in <code>.watch-cache/</code> that aren't in the manifest. Usually leftovers from sandbox testing.</p>
        <ul id="orphans-list" class="orphans-list"></ul>
        <div class="orphans-summary" id="orphans-summary"></div>
      </div>
      <div class="modal-foot">
        <button id="orphans-select-all" class="secondary">Select all</button>
        <button id="orphans-delete" class="danger">Delete selected</button>
      </div>
    </div>
  </div>

  <!-- Disk usage breakdown modal -->
  <div class="modal-bg" id="disk-modal-bg">
    <div class="modal disk-modal">
      <div class="modal-head">
        <h2>Disk usage by record</h2>
        <button id="disk-close">Close</button>
      </div>
      <div class="modal-body">
        <p class="disk-summary" id="disk-summary"></p>
        <table class="disk-table">
          <thead><tr><th>Title</th><th style="text-align:right">Size</th><th>ID</th></tr></thead>
          <tbody id="disk-rows"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Job progress modal — server mode only -->
  <div class="modal-bg" id="job-modal-bg">
    <div class="modal job-modal">
      <div class="modal-head">
        <h2 id="job-modal-title">Job</h2>
        <span class="job-status" id="job-status">queued</span>
        <button id="job-cancel" class="job-cancel" title="Terminate this subprocess">Cancel</button>
        <button id="job-close">Close</button>
      </div>
      <div class="modal-body">
        <p class="job-detail" id="job-detail"></p>
        <div class="job-progress" id="job-progress" style="display:none">
          <div class="job-progress-fill"></div>
        </div>
        <div class="job-log-tabs" id="job-log-tabs">
          <button class="tab active" data-channel="extract">Extract</button>
          <button class="tab" data-channel="synthesis">Synthesis</button>
        </div>
        <pre class="job-log" id="job-log"></pre>
        <div class="job-actions" id="job-actions">
          <button id="job-copy-prompt">Copy prompt for Claude Code</button>
          <button id="job-show-report">View focused report</button>
          <button id="job-open-result" class="primary">Open Result</button>
          <button id="job-open-nlm">Open NLM</button>
        </div>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    let RECORDS = {{DATA}};
    let SUMMARIES = {{SUMMARIES}};
    let FOCUSED_RESULTS = {{FOCUSED_RESULTS}};
    let VARIANT_SUMMARIES = {{VARIANT_SUMMARIES}};
    let VARIANT_FOCUSED_RESULTS = {{VARIANT_FOCUSED_RESULTS}};
    let PREVIEWS = {{PREVIEWS}};
    const PROJECT_TAGS_SEED = {{PROJECT_TAGS}};
    const SCRIPT_PATH = {{SCRIPT_PATH}};
    const PROJECT_ROOT = {{PROJECT_DIR_FORWARD_SLASH}};

    /** Server mode is on when the page is served over http(s); off on file://. */
    const SERVER_MODE = (window.location.protocol === "http:" || window.location.protocol === "https:");
    const STATE_KEY = "watch_dashboard_state";
    const UI_KEY = "watch_dashboard_ui";
    const MARKERS_KEY = "watch_dashboard_markers";
    const PROJECTS_KEY = "watch_dashboard_projects";

    // Project tags: seeded from server-side PROJECT_TAGS_SEED on first load.
    // After any edit, localStorage becomes the source of truth.
    function loadProjects() {
      try {
        const raw = localStorage.getItem(PROJECTS_KEY);
        if (!raw) return PROJECT_TAGS_SEED.slice();
        const arr = JSON.parse(raw);
        if (!Array.isArray(arr) || arr.length === 0) return PROJECT_TAGS_SEED.slice();
        return arr;
      } catch { return PROJECT_TAGS_SEED.slice(); }
    }
    function saveProjects(arr) {
      try { localStorage.setItem(PROJECTS_KEY, JSON.stringify(arr)); }
      catch { showToast("Couldn't save projects: localStorage full?"); }
    }
    let PROJECT_TAGS = loadProjects();

    function loadState() {
      try { return JSON.parse(localStorage.getItem(STATE_KEY) || "{}"); }
      catch { return {}; }
    }
    function saveState(state) {
      try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); }
      catch (e) { showToast("Couldn't save: localStorage full?"); }
    }
    function loadUI() {
      try { return JSON.parse(localStorage.getItem(UI_KEY) || "{}"); }
      catch { return {}; }
    }
    function saveUI(ui) {
      try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch {}
    }
    function loadMarkers() {
      try { return JSON.parse(localStorage.getItem(MARKERS_KEY) || "{}"); }
      catch { return {}; }
    }
    function saveMarkers(m) {
      try { localStorage.setItem(MARKERS_KEY, JSON.stringify(m)); }
      catch (e) { showToast("Couldn't save markers: localStorage full?"); }
    }

    let state = loadState();
    const ui = loadUI();
    const markers = loadMarkers();

    // Phase 7: bulk-select state, disk usage, confirm-modal callback.
    const selectedIds = new Set();
    const diskState = { cache_total_bytes: 0, video_count: 0, orphan_count: 0, by_record: [] };
    let confirmCallback = null;

    // Hide server-only columns in static mode via a body class.
    if (!SERVER_MODE) document.documentElement.classList.add("static-mode");

    function fmtBytes(n) {
      if (!n || n < 0) return "0 B";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let i = 0; let v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return v >= 100 ? `${v.toFixed(0)} ${units[i]}` : v >= 10 ? `${v.toFixed(1)} ${units[i]}` : `${v.toFixed(2)} ${units[i]}`;
    }

    function getAnnot(id) {
      return state[id] || { nlm_pasted: false, project_tag: "None", note: "" };
    }

    /** Phase 14: pre-select a project tag in the dropdown when the video's
     *  description mentions one. Visual-only (doesn't persist) until the
     *  user explicitly picks a value — keeps localStorage as the source of
     *  truth and lets the user override silently. Returns null on no match. */
    function suggestProjectTag(description) {
      if (!description) return null;
      const text = String(description).toLowerCase();
      // First pass: full-tag match (so Amrocky-Migration-Tool wins over generic Amrocky).
      for (const tag of PROJECT_TAGS) {
        if (tag === "None") continue;
        if (text.includes(tag.toLowerCase())) return tag;
      }
      // Second pass: tag-prefix match, word-boundary anchored to avoid
      // false positives like 'PKC' in 'pickle'.
      for (const tag of PROJECT_TAGS) {
        if (tag === "None") continue;
        const prefix = tag.split("-")[0].toLowerCase();
        if (prefix.length < 3) continue;  // very short prefixes are unsafe
        try {
          if (new RegExp(`\\b${prefix}\\b`, "i").test(text)) return tag;
        } catch { /* skip on regex errors */ }
      }
      return null;
    }
    function setAnnot(id, patch) {
      state[id] = { ...getAnnot(id), ...patch, marked_at: new Date().toISOString() };
      saveState(state);
    }

    /** Unified record state — falls back to legacy `status` for older records. */
    function effectiveState(r) {
      return r.state || r.status || "complete";
    }

    /** Marker draft — segments + review + active in-progress segment. */
    function newSegId() {
      try { if (crypto && crypto.randomUUID) return crypto.randomUUID(); } catch {}
      return "seg-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    }
    /** Phase 4: legacy drafts may have type='skip'; normalize to 'exclude'. */
    function normalizeType(t) { return t === "skip" ? "exclude" : t; }
    /** type → CSS class fragment (audio_only → audio for tidier selectors) */
    function typeClass(t) { return t === "audio_only" ? "audio" : t; }
    /** type → letter for badges and recording status */
    function typeLetter(t) {
      return t === "must" ? "M" : t === "audio_only" ? "A" : t === "exclude" ? "X" : "?";
    }
    function getMarker(id) {
      const raw = markers[id] || { review: "", segments: [], active: null };
      const segments = (raw.segments || []).map(s => ({
        ...s,
        id: s.id || newSegId(),
        type: normalizeType(s.type),
      }));
      let active = raw.active || null;
      if (active && active.type) active = { ...active, type: normalizeType(active.type) };
      return {
        review: raw.review || "",
        segments,
        active,
      };
    }
    function setMarker(id, patch) {
      markers[id] = { ...getMarker(id), ...patch };
      saveMarkers(markers);
    }

    function fmtDuration(seconds) {
      if (!seconds && seconds !== 0) return "—";
      const s = Math.round(seconds);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      if (h > 0) return `${h}:${String(m).padStart(2,"0")}:${String(sec).padStart(2,"0")}`;
      return `${m}:${String(sec).padStart(2,"0")}`;
    }

    /** "MM:SS" or "HH:MM:SS" → milliseconds. Returns null if unparseable. */
    function parseTimeToMs(str) {
      if (!str) return null;
      const parts = String(str).trim().split(":").map(p => p.trim());
      if (parts.some(p => p === "" || isNaN(Number(p)))) return null;
      const nums = parts.map(Number);
      let s;
      if (nums.length === 1) s = nums[0];
      else if (nums.length === 2) s = nums[0] * 60 + nums[1];
      else if (nums.length === 3) s = nums[0] * 3600 + nums[1] * 60 + nums[2];
      else return null;
      if (!isFinite(s) || s < 0) return null;
      return Math.round(s * 1000);
    }
    function fmtMs(ms) {
      if (ms == null || isNaN(ms)) return "—";
      const s = Math.round(ms / 1000);
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      const sec = s % 60;
      if (h > 0) return `${h}:${String(m).padStart(2,"0")}:${String(sec).padStart(2,"0")}`;
      return `${m}:${String(sec).padStart(2,"0")}`;
    }

    function fmtAge(iso) {
      if (!iso) return "—";
      const d = new Date(iso);
      const diffMs = Date.now() - d.getTime();
      const diffMin = Math.floor(diffMs / 60000);
      if (diffMin < 1) return "just now";
      if (diffMin < 60) return `${diffMin}m ago`;
      const diffHr = Math.floor(diffMin / 60);
      if (diffHr < 24) return `${diffHr}h ago`;
      const diffDay = Math.floor(diffHr / 24);
      if (diffDay < 30) return `${diffDay}d ago`;
      return d.toISOString().slice(0, 10);
    }

    function escapeHtml(s) {
      if (s == null) return "";
      return String(s)
        .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
    }

    function fileUri(p) {
      if (!p) return "";
      const fwd = p.replace(/\\/g, "/");
      if (SERVER_MODE) {
        // Strip the project-root prefix (in forward-slash form) and re-root under /files/*.
        // Each path component is encoded so spaces and other reserved chars survive.
        let rel = fwd;
        if (PROJECT_ROOT && rel.startsWith(PROJECT_ROOT)) {
          rel = rel.slice(PROJECT_ROOT.length);
        }
        if (!rel.startsWith("/")) rel = "/" + rel;
        const encoded = rel.split("/").map(encodeURIComponent).join("/");
        return "/files" + encoded;
      }
      const norm = fwd.replace(/^([A-Za-z]):/, "/$1:");
      return "file:///" + encodeURI(norm.replace(/^\/+/, ""));
    }

    function showToast(msg) {
      const t = document.getElementById("toast");
      t.textContent = msg;
      t.classList.add("show");
      clearTimeout(showToast._t);
      showToast._t = setTimeout(() => t.classList.remove("show"), 1800);
    }

    let sortKey = ui.sortKey || "started_at";
    let sortDir = ui.sortDir || -1;
    let q = ui.q || "";
    let filterTranscript = ui.filterTranscript || "";
    let filterStatus = ui.filterStatus || "";
    let filterNlm = ui.filterNlm || "";
    let filterProject = ui.filterProject || "";

    document.getElementById("q").value = q;
    document.getElementById("filter-transcript").value = filterTranscript;
    document.getElementById("filter-status").value = filterStatus;
    document.getElementById("filter-nlm").value = filterNlm;

    // Populate project filter
    const projSel = document.getElementById("filter-project");
    PROJECT_TAGS.forEach(t => {
      const opt = document.createElement("option");
      opt.value = t === "None" ? "" : t;
      opt.textContent = t === "None" ? "(no tag)" : t;
      if (t === filterProject || (t === "None" && filterProject === "(none)")) opt.selected = true;
      if (t === "None") opt.value = "(none)";
      projSel.appendChild(opt);
    });
    projSel.value = filterProject;

    function persistUI() {
      saveUI({ sortKey, sortDir, q, filterTranscript, filterStatus, filterNlm, filterProject });
    }

    function renderStats() {
      const total = RECORDS.length;
      let pasted = 0, pending = 0, failed = 0;
      const byProject = {};
      RECORDS.forEach(r => {
        const a = getAnnot(r.id);
        if ((r.status || "complete") === "failed") failed++;
        if (a.nlm_pasted) pasted++; else pending++;
        const tag = a.project_tag || "None";
        byProject[tag] = (byProject[tag] || 0) + 1;
      });
      const topTags = Object.entries(byProject)
        .filter(([k]) => k !== "None")
        .sort((a, b) => b[1] - a[1])
        .slice(0, 3)
        .map(([k, v]) => `${k}: ${v}`)
        .join(" · ") || "—";
      const cacheNum = SERVER_MODE ? fmtBytes(diskState.cache_total_bytes) : "—";
      const cacheLbl = (SERVER_MODE && diskState.orphan_count > 0)
        ? `Cache · ${diskState.orphan_count} orphan${diskState.orphan_count !== 1 ? "s" : ""}`
        : "Cache size";
      const cacheCls = SERVER_MODE ? "cache-stat" : "cache-stat disabled";
      const cacheTitle = SERVER_MODE
        ? "Click for breakdown by record"
        : "Server required for live disk usage";
      document.getElementById("stats").innerHTML = `
        <div class="stat"><span class="num">${total}</span><span class="lbl">Total</span></div>
        <div class="stat"><span class="num green">${pasted}</span><span class="lbl">Pasted to NLM</span></div>
        <div class="stat"><span class="num amber">${pending}</span><span class="lbl">Pending NLM</span></div>
        <div class="stat"><span class="num muted">${failed}</span><span class="lbl">Failed</span></div>
        <div class="stat ${cacheCls}" id="cache-stat" title="${cacheTitle}"><span class="num">${cacheNum}</span><span class="lbl">${cacheLbl}</span></div>
        <div class="stat" style="margin-left:auto;"><span class="num" style="font-size:13px;font-weight:400;color:var(--muted);">${topTags}</span><span class="lbl">Top projects</span></div>
      `;
    }

    function render() {
      const tbody = document.getElementById("rows");
      const empty = document.getElementById("empty");
      const ql = q.toLowerCase().trim();

      let rows = RECORDS.slice();
      if (filterTranscript) rows = rows.filter(r => (r.transcript_source || "none") === filterTranscript);
      if (filterStatus) rows = rows.filter(r => effectiveState(r) === filterStatus);
      if (filterNlm) {
        rows = rows.filter(r => {
          const a = getAnnot(r.id);
          return (filterNlm === "pasted") === !!a.nlm_pasted;
        });
      }
      if (filterProject) {
        rows = rows.filter(r => {
          const a = getAnnot(r.id);
          if (filterProject === "(none)") return !a.project_tag || a.project_tag === "None";
          return a.project_tag === filterProject;
        });
      }
      if (ql) {
        rows = rows.filter(r => {
          const a = getAnnot(r.id);
          const hay = [
            r.title, r.uploader, r.source, r.work_dir, r.transcript_source, r.status,
            r.transcript_preview, a.note, a.project_tag,
          ].filter(Boolean).join(" ").toLowerCase();
          return hay.includes(ql);
        });
      }

      rows.sort((a, b) => {
        let av, bv;
        if (sortKey === "project_tag") {
          av = (getAnnot(a.id).project_tag || "None");
          bv = (getAnnot(b.id).project_tag || "None");
        } else if (sortKey === "nlm_pasted") {
          av = getAnnot(a.id).nlm_pasted ? 1 : 0;
          bv = getAnnot(b.id).nlm_pasted ? 1 : 0;
        } else {
          av = a[sortKey] ?? "";
          bv = b[sortKey] ?? "";
        }
        if (av < bv) return -1 * sortDir;
        if (av > bv) return 1 * sortDir;
        return 0;
      });

      if (rows.length === 0) {
        tbody.innerHTML = "";
        empty.style.display = "block";
        return;
      }
      empty.style.display = "none";

      tbody.innerHTML = rows.map(r => {
        const a = getAnnot(r.id);
        const tFrame = r.first_frame_path ? fileUri(r.first_frame_path) : "";
        const workUri = r.work_dir ? fileUri(r.work_dir) : "";
        const sourceLink = r.source && r.source.startsWith("http")
          ? `<a href="${escapeHtml(r.source)}" target="_blank" rel="noopener">${escapeHtml(r.source)}</a>`
          : escapeHtml(r.source || "");
        const tBadge = (r.transcript_source || "none");
        const tCount = r.transcript_segment_count != null ? ` · ${r.transcript_segment_count}` : "";
        const status = effectiveState(r);
        const hasNlm = !!SUMMARIES[r.id];
        const hasResult = !!FOCUSED_RESULTS[r.id];
        const hasPreview = !!PREVIEWS[r.id];
        const isMarkable = (status === "preview-ready" || status === "focused-ready") && hasPreview;
        // Phase 11: which slots (main + variants) have any saved content?
        const vSummaries = VARIANT_SUMMARIES[r.id] || {};
        const vFocused = VARIANT_FOCUSED_RESULTS[r.id] || {};
        const slotsPresent = [];
        if (hasNlm || hasResult) slotsPresent.push("main");
        for (const slot of ["variant-a", "variant-b"]) {
          if (vSummaries[slot] || vFocused[slot]) slotsPresent.push(slot);
        }
        const variantCount = slotsPresent.filter(s => s !== "main").length;
        const showCompare = slotsPresent.length >= 2;

        // Project dropdown — projects come from localStorage (with seed fallback).
        // Always include the user's current tag even if it's been removed from the project list,
        // so we don't silently flip their selection.
        const tagsForRow = PROJECT_TAGS.slice();
        if (a.project_tag && !tagsForRow.includes(a.project_tag)) tagsForRow.push(a.project_tag);
        if (!tagsForRow.includes("None")) tagsForRow.unshift("None");
        // Phase 14: if user hasn't tagged this row yet and the description
        // hints at a project, surface the suggestion in the dropdown.
        let effectiveTag = a.project_tag;
        if (!state[r.id] && (!effectiveTag || effectiveTag === "None")) {
          const suggested = suggestProjectTag(r.description_excerpt);
          if (suggested && tagsForRow.includes(suggested)) effectiveTag = suggested;
        }
        const projectOptions = tagsForRow.map(t =>
          `<option value="${escapeHtml(t)}" ${t === effectiveTag ? "selected" : ""}>${t === "None" ? "(none)" : escapeHtml(t)}</option>`
        ).join("");

        // Phase 12: legacy rows (created before --preview existed) have no
        // preview.json, so the Mark button can't render. Surface a Refresh
        // affordance — server re-runs watch.py --preview --out-dir on the
        // existing work_dir without touching focused outputs.
        const showRefreshPreview = !hasPreview && (status === "complete" || status === "focused-ready");

        // Action buttons: only render what's actionable. No more disabled "no NLM" / "no result" labels.
        const actionParts = [];
        if (workUri) actionParts.push(`<a href="${workUri}" title="Open work directory">work</a>`);
        if (showRefreshPreview) actionParts.push(`<button data-action="refresh-preview" title="Re-extract sparse frames + transcript so you can use the marker UI on this row.">Refresh preview</button>`);
        if (isMarkable) {
          const markLabel = status === "focused-ready" ? "Re-mark" : "Mark";
          const markTitle = status === "focused-ready"
            ? "Re-open the marker editor to change segments, switch model, or re-run synthesis"
            : "Mark must/skip segments and generate the focused command";
          actionParts.push(`<button data-action="mark" class="mark-cta" title="${markTitle}">${markLabel}</button>`);
        }
        if (hasResult) actionParts.push(`<button data-action="preview-result" title="Read the long-form focused result">Result</button>`);
        if (hasNlm) actionParts.push(`<button data-action="preview-nlm" title="Preview the NLM-ready summary">NLM</button>`);
        if (showCompare) {
          const vTag = variantCount > 0 ? `<span class="variant-count">(${variantCount}v)</span>` : "";
          actionParts.push(`${vTag}<button data-action="compare" class="compare-btn" title="Side-by-side comparison of main and variants">Compare</button>`);
        }
        actionParts.push(`<button data-action="copy-rerun" title="Copy re-run command to clipboard">re-run</button>`);
        actionParts.push(`<button data-action="delete-record" class="del-btn" title="Delete this record and its files">delete</button>`);

        const selectChecked = selectedIds.has(r.id) ? "checked" : "";
        return `<tr data-id="${escapeHtml(r.id)}">
          <td class="select-cell server-only-col"><input type="checkbox" data-action="select-row" ${selectChecked}></td>
          <td><span class="age" title="${escapeHtml(r.started_at || "")}">${fmtAge(r.started_at)}</span></td>
          <td>
            <div class="title">${escapeHtml(r.title || "(no title)")}</div>
            <div class="source">${r.uploader ? escapeHtml(r.uploader) + " · " : ""}${sourceLink}</div>
          </td>
          <td class="thumb">${tFrame
            ? `<img src="${tFrame}" alt="" data-frame="${tFrame}">`
            : `<div class="placeholder">—</div>`}</td>
          <td class="duration">${fmtDuration(r.duration_seconds)}</td>
          <td class="frames">${r.frames_count != null ? r.frames_count : "—"}</td>
          <td><span class="badge ${tBadge}">${tBadge}${tCount}</span></td>
          <td><span class="badge ${status}">${status}</span></td>
          <td class="tag-cell">
            <select data-action="tag" class="${effectiveTag && effectiveTag !== 'None' ? 'tagged' : ''}">${projectOptions}</select>
          </td>
          <td class="nlm-cell">
            <input type="checkbox" data-action="nlm" ${a.nlm_pasted ? "checked" : ""} title="Mark pasted to NLM">
          </td>
          <td class="note-cell">
            <textarea data-action="note" placeholder="note…">${escapeHtml(a.note || "")}</textarea>
          </td>
          <td class="actions">${actionParts.join("")}</td>
        </tr>`;
      }).join("");
    }

    function fullRender() { renderStats(); render(); }

    // Event delegation
    document.getElementById("rows").addEventListener("change", e => {
      const tr = e.target.closest("tr"); if (!tr) return;
      const id = tr.dataset.id;
      const action = e.target.dataset.action;
      if (action === "nlm") {
        setAnnot(id, { nlm_pasted: e.target.checked });
        renderStats();
      } else if (action === "tag") {
        setAnnot(id, { project_tag: e.target.value });
        renderStats();
        if (e.target.value !== "None") e.target.classList.add("tagged"); else e.target.classList.remove("tagged");
      } else if (action === "note") {
        setAnnot(id, { note: e.target.value });
      }
    });

    document.getElementById("rows").addEventListener("click", e => {
      const tr = e.target.closest("tr"); if (!tr) return;
      const id = tr.dataset.id;
      const action = e.target.dataset.action;

      if (e.target.tagName === "IMG" && e.target.dataset.frame) {
        const m = document.getElementById("img-modal-bg");
        document.getElementById("img-modal").src = e.target.dataset.frame;
        m.classList.add("show");
        return;
      }
      if (action === "mark") {
        openMarker(id);
        return;
      }
      if (action === "compare") {
        openCompareModal(id);
        return;
      }
      if (action === "refresh-preview") {
        if (!SERVER_MODE) {
          showToast("Refresh requires the server (http://localhost:4893)");
          return;
        }
        const rec = RECORDS.find(r => r.id === id);
        fetch(`/api/records/${encodeURIComponent(id)}/refresh-preview`, { method: "POST" })
          .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); }))
          .then(data => {
            openJobModal(
              data.job_id,
              "Refresh preview",
              rec
                ? `Re-extracting sparse frames + transcript for "${rec.title || rec.id}"…`
                : "Re-extracting preview…",
              { rec_id: id }
            );
          })
          .catch(err => showToast("Refresh failed: " + err.message));
        return;
      }
      if (action === "preview-nlm") {
        const summary = SUMMARIES[id];
        const rec = RECORDS.find(r => r.id === id);
        document.getElementById("nlm-modal-title").textContent = (rec && rec.title) || "NLM Summary";
        const body = document.getElementById("nlm-modal-body");
        if (summary) body.textContent = summary;
        else body.innerHTML = '<span class="empty-msg">No NLM summary file found at &lt;work_dir&gt;/nlm-summary.md.</span>';
        body.dataset.id = id;
        document.getElementById("nlm-modal-bg").classList.add("show");
      } else if (action === "preview-result") {
        const result = FOCUSED_RESULTS[id];
        const rec = RECORDS.find(r => r.id === id);
        document.getElementById("result-modal-title").textContent = (rec && rec.title) ? `Result — ${rec.title}` : "Focused result";
        const body = document.getElementById("result-modal-body");
        if (result) body.textContent = result;
        else body.innerHTML = '<span class="empty-msg">No focused-result.md found at &lt;work_dir&gt;.</span>';
        body.dataset.id = id;
        document.getElementById("result-modal-bg").classList.add("show");
      } else if (action === "copy-rerun") {
        const rec = RECORDS.find(r => r.id === id);
        if (!rec) return;
        const cmd = `python "${SCRIPT_PATH}" "${rec.source}"`;
        navigator.clipboard.writeText(cmd).then(
          () => showToast("Re-run command copied"),
          () => showToast("Couldn't copy — check clipboard permissions")
        );
      }
    });

    // Modal close + copy
    document.getElementById("nlm-close").addEventListener("click", () => document.getElementById("nlm-modal-bg").classList.remove("show"));
    document.getElementById("nlm-modal-bg").addEventListener("click", e => {
      if (e.target.id === "nlm-modal-bg") document.getElementById("nlm-modal-bg").classList.remove("show");
    });
    document.getElementById("nlm-copy").addEventListener("click", () => {
      const text = document.getElementById("nlm-modal-body").textContent;
      navigator.clipboard.writeText(text).then(
        () => showToast("NLM summary copied — paste into your topic notebook"),
        () => showToast("Couldn't copy — select manually")
      );
    });

    document.getElementById("result-close").addEventListener("click", () => document.getElementById("result-modal-bg").classList.remove("show"));
    document.getElementById("result-modal-bg").addEventListener("click", e => {
      if (e.target.id === "result-modal-bg") document.getElementById("result-modal-bg").classList.remove("show");
    });
    document.getElementById("result-copy").addEventListener("click", () => {
      const text = document.getElementById("result-modal-body").textContent;
      navigator.clipboard.writeText(text).then(
        () => showToast("Focused result copied"),
        () => showToast("Couldn't copy — select manually")
      );
    });
    document.getElementById("img-modal-bg").addEventListener("click", () => {
      document.getElementById("img-modal-bg").classList.remove("show");
    });

    // ── Marker modal ───────────────────────────────────────────────────────────
    let markerCurrentId = null;       // record id whose preview is open
    let markerVideo = null;           // <video id="marker-video"> ref
    let markerPlayheadRaf = null;     // requestAnimationFrame id

    function openMarker(id) {
      const rec = RECORDS.find(r => r.id === id);
      const preview = PREVIEWS[id];
      if (!rec || !preview) {
        showToast("No preview data for this record");
        return;
      }
      markerCurrentId = id;
      markerVideo = document.getElementById("marker-video");

      document.getElementById("marker-title").textContent = `Mark segments — ${rec.title || rec.id}`;

      const draft = getMarker(id);
      document.getElementById("marker-review").value = draft.review || "";
      document.getElementById("marker-start").value = "";
      document.getElementById("marker-end").value = "";
      document.getElementById("marker-intent").value = "";
      document.getElementById("marker-type").value = "must";

      // Wire video src (file:// — Chrome plays it fine in same-origin file context)
      if (preview.video_path) {
        markerVideo.src = fileUri(preview.video_path);
        markerVideo.style.display = "block";
      } else {
        markerVideo.removeAttribute("src");
        markerVideo.style.display = "none";
      }

      renderMarkerFrames(preview);
      renderMarkerTranscript(preview);
      renderMarkerSegments();
      renderMarkerMeta(rec, preview);
      startPlayheadLoop();

      document.getElementById("marker-modal-bg").classList.add("show");
    }

    /** Phase 14: populate the meta strip — creator's intent + chapter button. */
    function renderMarkerMeta(rec, preview) {
      const meta = document.getElementById("marker-meta");
      const intentDetails = document.getElementById("marker-creator-intent-details");
      const intentBody = document.getElementById("marker-creator-intent-body");
      const chBtn = document.getElementById("marker-apply-chapters");
      const chCount = document.getElementById("marker-chapter-count");

      const desc = (preview && preview.description)
        || (rec && rec.description_excerpt)
        || "";
      const chapters = (preview && Array.isArray(preview.chapters)) ? preview.chapters : [];

      if (desc) {
        intentBody.textContent = desc;
        intentDetails.hidden = false;
        intentDetails.open = false;
      } else {
        intentDetails.hidden = true;
      }
      if (chapters.length > 0) {
        chBtn.hidden = false;
        chCount.textContent = `${chapters.length} chapter${chapters.length === 1 ? "" : "s"} available`;
      } else {
        chBtn.hidden = true;
        chCount.textContent = "";
      }
      meta.hidden = !(desc || chapters.length > 0);
    }

    /** Heuristic chapter classifier — title text drives must / exclude / skip. */
    function classifyChapter(title, idx, total) {
      const t = (title || "").toLowerCase();
      if (idx === 0 && /\b(intro|introduction|welcome|overview|hello)\b/.test(t)) return "exclude";
      if (idx === total - 1 && /\b(outro|conclusion|recap|subscribe|wrap[- ]?up|thanks for watching)\b/.test(t)) return "exclude";
      if (/\b(demo|example|walkthrough|live|code|slide|screen|implementation|tutorial|build)/.test(t)) return "must";
      return null;
    }

    function applyChapterMarkers() {
      if (!markerCurrentId) return;
      const preview = PREVIEWS[markerCurrentId];
      const chapters = (preview && Array.isArray(preview.chapters)) ? preview.chapters : [];
      if (chapters.length === 0) {
        showToast("No chapters available on this video");
        return;
      }
      const draft = getMarker(markerCurrentId);
      if (draft.segments && draft.segments.length > 0) {
        const n = draft.segments.length;
        if (!confirm(`Replace your ${n} existing segment${n === 1 ? "" : "s"} with chapter suggestions?`)) return;
      }
      const dur = durationMs();
      const total = chapters.length;
      const newSegments = [];
      chapters.forEach((ch, idx) => {
        const startMs = Math.round((Number(ch.start_time) || 0) * 1000);
        let endMs;
        if (ch.end_time != null && isFinite(Number(ch.end_time))) {
          endMs = Math.round(Number(ch.end_time) * 1000);
        } else if (idx + 1 < total) {
          endMs = Math.round((Number(chapters[idx + 1].start_time) || 0) * 1000);
        } else {
          endMs = dur || (startMs + 1000);
        }
        if (endMs <= startMs) return;
        const type = classifyChapter(ch.title, idx, total);
        if (!type) return;
        newSegments.push({
          id: newSegId(),
          type,
          start_ms: startMs,
          end_ms: endMs,
          intent: (ch.title || "").trim(),
        });
      });
      if (newSegments.length === 0) {
        showToast("No chapters matched the heuristics — mark manually");
        return;
      }
      setMarker(markerCurrentId, { segments: newSegments, active: null });
      renderMarkerSegments();
      showToast(`Pre-populated ${newSegments.length} segment${newSegments.length === 1 ? "" : "s"} from chapters — review and adjust`);
    }

    document.getElementById("marker-apply-chapters").addEventListener("click", applyChapterMarkers);

    function closeMarker() {
      document.getElementById("marker-modal-bg").classList.remove("show");
      const vMenu = document.getElementById("marker-variant-menu");
      if (vMenu) vMenu.hidden = true;
      if (markerVideo) { try { markerVideo.pause(); } catch {} }
      stopPlayheadLoop();
      markerCurrentId = null;
    }

    function durationMs() {
      const preview = markerCurrentId ? PREVIEWS[markerCurrentId] : null;
      return (preview && preview.duration_ms) || 0;
    }
    function currentMs() {
      if (markerVideo && isFinite(markerVideo.currentTime)) {
        return Math.round(markerVideo.currentTime * 1000);
      }
      return 0;
    }
    function startPlayheadLoop() {
      function tick() {
        if (!markerCurrentId) return;
        renderMarkerPlayhead();
        refreshFramePlayhead();
        markerPlayheadRaf = requestAnimationFrame(tick);
      }
      stopPlayheadLoop();
      markerPlayheadRaf = requestAnimationFrame(tick);
    }
    function stopPlayheadLoop() {
      if (markerPlayheadRaf != null) cancelAnimationFrame(markerPlayheadRaf);
      markerPlayheadRaf = null;
    }

    function renderMarkerFrames(preview) {
      const container = document.getElementById("marker-frames");
      const frames = preview.sparse_frames || [];
      if (frames.length === 0) {
        container.innerHTML = '<span class="marker-segments-empty">No frames in preview.</span>';
        return;
      }
      container.innerHTML = frames.map(f => {
        const uri = fileUri(f.path);
        return `<div class="marker-frame" data-pts="${f.pts_ms}">
          <img src="${uri}" alt="frame at ${fmtMs(f.pts_ms)}">
          <div class="ts">${fmtMs(f.pts_ms)}</div>
        </div>`;
      }).join("");
      refreshFrameTints();
    }

    /** Tint each sparse frame based on which segment contains its pts_ms. */
    function refreshFrameTints() {
      if (!markerCurrentId) return;
      const draft = getMarker(markerCurrentId);
      const segments = draft.segments || [];
      const active = draft.active;
      document.querySelectorAll("#marker-frames .marker-frame").forEach(el => {
        const pts = Number(el.dataset.pts);
        el.classList.remove("in-must", "in-audio", "in-exclude", "in-active");
        for (const s of segments) {
          if (pts >= s.start_ms && pts <= s.end_ms) {
            el.classList.add(`in-${typeClass(s.type)}`);
            break;
          }
        }
        if (active) {
          const endProvisional = Math.max(active.start_ms, currentMs());
          if (pts >= active.start_ms && pts <= endProvisional) {
            el.classList.add(`in-${typeClass(active.type)}`);
            el.classList.add("in-active");
          }
        }
      });
    }

    function refreshFramePlayhead() {
      const cur = currentMs();
      let nearest = null;
      document.querySelectorAll("#marker-frames .marker-frame").forEach(el => {
        el.classList.remove("at-playhead");
        const pts = Number(el.dataset.pts);
        if (pts <= cur && (!nearest || pts > Number(nearest.dataset.pts))) nearest = el;
      });
      if (nearest) nearest.classList.add("at-playhead");
    }

    function renderMarkerTimelineRegions() {
      const cont = document.getElementById("marker-timeline-regions");
      const dur = durationMs();
      if (!dur || !markerCurrentId) { cont.innerHTML = ""; return; }
      const draft = getMarker(markerCurrentId);
      const segs = (draft.segments || []).slice();
      const html = segs.map(s => {
        const left = (s.start_ms / dur) * 100;
        const width = ((s.end_ms - s.start_ms) / dur) * 100;
        const tip = `${typeLetter(s.type)} ${fmtMs(s.start_ms)}–${fmtMs(s.end_ms)}${s.intent ? ' · ' + s.intent : ''}`;
        return `<div class="marker-timeline-region ${typeClass(s.type)}" style="left:${left}%;width:${width}%" title="${escapeHtml(tip)}"></div>`;
      });
      if (draft.active) {
        const left = (draft.active.start_ms / dur) * 100;
        const cur = Math.max(draft.active.start_ms, currentMs());
        const width = ((cur - draft.active.start_ms) / dur) * 100;
        html.push(`<div class="marker-timeline-region ${typeClass(draft.active.type)} active" style="left:${left}%;width:${width}%"></div>`);
      }
      cont.innerHTML = html.join("");
    }

    function renderMarkerPlayhead() {
      const head = document.getElementById("marker-timeline-playhead");
      const dur = durationMs();
      if (!dur) { head.style.display = "none"; return; }
      head.style.display = "block";
      const pct = Math.max(0, Math.min(100, (currentMs() / dur) * 100));
      head.style.left = `calc(${pct}% - 1px)`;
      // While an active segment is open, its visible band must keep growing with the playhead.
      if (markerCurrentId) {
        const draft = getMarker(markerCurrentId);
        if (draft.active) renderMarkerTimelineRegions();
      }
    }

    function renderMarkerTranscript(preview) {
      const container = document.getElementById("marker-transcript");
      const segs = preview.transcript_segments || [];
      if (segs.length === 0) {
        container.innerHTML = '<span class="marker-segments-empty">No transcript in preview.</span>';
        return;
      }
      container.innerHTML = segs.map(s =>
        `<span class="seg" data-start="${s.start_ms}" data-end="${s.end_ms}">
           <span class="ts">${fmtMs(s.start_ms)}</span>${escapeHtml(s.text || "")}
         </span>`
      ).join("");
    }

    function renderClosedSegmentRow(s) {
      const dur = Math.max(0, (s.end_ms - s.start_ms) / 1000);
      return `<li data-seg-id="${escapeHtml(s.id)}">
        <span class="seg-badge ${typeClass(s.type)}">${typeLetter(s.type)}</span>
        <span class="seg-range">
          <span>${fmtMs(s.start_ms)} → ${fmtMs(s.end_ms)}</span>
          <span class="seg-duration">${dur.toFixed(1)}s</span>
        </span>
        <span class="seg-actions">
          <button class="icon" data-action="seg-edit" title="Edit">edit</button>
          <button class="icon del" data-action="seg-del" title="Delete">del</button>
        </span>
        <span class="seg-intent">${escapeHtml(s.intent || "—")}</span>
      </li>`;
    }

    function renderActiveSegmentRow(active) {
      const lbl = typeLetter(active.type);
      return `<li class="active" data-active="1">
        <span class="seg-badge ${typeClass(active.type)}">${lbl}</span>
        <span class="seg-range">
          <span>${fmtMs(active.start_ms)} → …</span>
          <span class="seg-active-hint">click ${lbl} again to close</span>
        </span>
        <span class="seg-actions">
          <button class="icon del" data-action="active-cancel" title="Discard">×</button>
        </span>
        <span class="seg-active-intent">
          <input type="text" data-action="active-intent" placeholder="intent (optional)" value="${escapeHtml(active.intent || '')}">
        </span>
      </li>`;
    }

    function renderMarkerSegments() {
      if (!markerCurrentId) return;
      const draft = getMarker(markerCurrentId);
      const segs = (draft.segments || []).slice().sort((a, b) => a.start_ms - b.start_ms);
      const ul = document.getElementById("marker-segments-list");
      const header = document.getElementById("marker-segments-header");
      const coverage = document.getElementById("marker-coverage");

      header.textContent = `Segments (${segs.length})`;
      const dur = durationMs();
      if (dur > 0 && segs.length > 0) {
        const sumMs = (t) => segs.filter(s => s.type === t).reduce((a, s) => a + (s.end_ms - s.start_ms), 0);
        const mustPct = Math.round((sumMs("must") / dur) * 100);
        const audioPct = Math.round((sumMs("audio_only") / dur) * 100);
        const excludePct = Math.round((sumMs("exclude") / dur) * 100);
        const baselinePct = Math.max(0, 100 - mustPct - audioPct - excludePct);
        coverage.textContent = `${mustPct}% must · ${audioPct}% audio · ${excludePct}% excluded · ${baselinePct}% baseline`;
      } else {
        coverage.textContent = "";
      }

      if (segs.length === 0 && !draft.active) {
        ul.innerHTML = '<li class="marker-segments-empty">No segments yet — press M to start a must segment, or S for skip.</li>';
      } else {
        const closed = segs.map(renderClosedSegmentRow).join("");
        const activeRow = draft.active ? renderActiveSegmentRow(draft.active) : "";
        ul.innerHTML = closed + activeRow;
      }

      updateMarkerToolbar();
      updateMarkerEstimate();
      renderMarkerTimelineRegions();
      refreshFrameTints();
    }

    function updateMarkerToolbar() {
      if (!markerCurrentId) return;
      const draft = getMarker(markerCurrentId);
      const mBtn = document.getElementById("marker-m-btn");
      const aBtn = document.getElementById("marker-a-btn");
      const xBtn = document.getElementById("marker-x-btn");
      const rec = document.getElementById("marker-recording");
      const cancel = document.getElementById("marker-cancel-active");
      const counts = document.getElementById("marker-counts");

      mBtn.classList.remove("active");
      aBtn.classList.remove("active");
      xBtn.classList.remove("active");
      rec.classList.remove("active", "must", "audio", "exclude");

      if (draft.active) {
        const btnByType = { must: mBtn, audio_only: aBtn, exclude: xBtn };
        const btn = btnByType[draft.active.type];
        if (btn) btn.classList.add("active");
        rec.classList.add("active", typeClass(draft.active.type));
        const lbl = typeLetter(draft.active.type);
        rec.textContent = `recording ${lbl} from ${fmtMs(draft.active.start_ms)} — press ${lbl} again to close`;
        cancel.hidden = false;
      } else {
        rec.textContent = "Press M for visuals, A for audio-only annotation, X to exclude";
        cancel.hidden = true;
      }

      const segs = draft.segments || [];
      const n = (t) => segs.filter(s => s.type === t).length;
      counts.textContent = `${n("must")} must · ${n("audio_only")} audio · ${n("exclude")} exclude`;
    }

    /** Format a token count as "12.0k" for big-number display. */
    function fmtTokens(n) {
      if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
      return String(n);
    }

    function updateMarkerEstimate() {
      const btn = document.getElementById("marker-copy");
      const estFoot = document.getElementById("marker-estimate");
      const big = document.getElementById("marker-token-big");
      const sub = document.getElementById("marker-token-sub");
      const bar = document.getElementById("marker-token-bar");

      if (!markerCurrentId) {
        btn.disabled = true;
        estFoot.textContent = "No segments yet";
        big.textContent = "—"; sub.textContent = "Open a preview-ready record"; bar.style.width = "0%";
        return;
      }

      const draft = getMarker(markerCurrentId);
      const segs = draft.segments || [];
      const must = segs.filter(s => s.type === "must");
      const audioCount = segs.filter(s => s.type === "audio_only").length;
      const exclude = segs.filter(s => s.type === "exclude");

      // Phase 4 audio-only baseline math (mirrors watch.py for consistency):
      //   frames    = sum(must dur) × 2 fps × 800 tokens
      //   transcript = (kept_audio / total_audio) × full_word_count × 1.3
      //   full_pipeline_baseline = 100 frames × 800 + full_words × 1.3 = 80k + transcript
      const preview = PREVIEWS[markerCurrentId];
      const dur_s = preview && (preview.duration_seconds || (preview.duration_ms ? preview.duration_ms / 1000 : 0)) || 0;
      const fullWords = (preview && preview.transcript_segments || [])
        .reduce((a, s) => a + (s.text || "").split(/\s+/).filter(Boolean).length, 0);

      const mustSec = must.reduce((a, s) => a + Math.max(0, (s.end_ms - s.start_ms) / 1000), 0);
      const excludeSec = exclude.reduce((a, s) => a + Math.max(0, (s.end_ms - s.start_ms) / 1000), 0);
      const audioKept = Math.max(0, dur_s - excludeSec);

      const frameTokens = Math.round(mustSec * 2 * 800);
      const transcriptTokens = dur_s > 0
        ? Math.round((audioKept / dur_s) * fullWords * 1.3)
        : 0;
      const totalTokens = frameTokens + transcriptTokens;
      const fullPipeline = 80000 + Math.round(fullWords * 1.3);
      const savedPct = fullPipeline > 0
        ? Math.max(0, Math.round((1 - totalTokens / fullPipeline) * 100))
        : 0;
      const ratio = fullPipeline > 0 ? Math.min(100, (totalTokens / fullPipeline) * 100) : 0;

      // Token card
      big.textContent = `~${fmtTokens(totalTokens)}`;
      sub.textContent = savedPct > 0
        ? `${savedPct}% saved vs full pipeline`
        : "Same cost as full pipeline";
      sub.classList.toggle("full", savedPct === 0);
      bar.style.width = `${ratio.toFixed(1)}%`;

      // Footer one-liner — same numbers, compact
      if (segs.length === 0) {
        estFoot.textContent = `Audio-only baseline · ~${fmtTokens(totalTokens)} tokens (${savedPct}% saved)`;
      } else {
        estFoot.textContent = `${must.length} must · ${audioCount} audio · ${exclude.length} exclude · ~${fmtTokens(totalTokens)} tokens (${savedPct}% saved)`;
      }
      // Phase 4: zero markers is valid (audio-only baseline). Always allow copy.
      btn.disabled = false;
    }

    /** Build the focused-mode command for clipboard paste. */
    function buildFocusedCommand(rec) {
      const draft = getMarker(rec.id);
      const review = (draft.review || "").replace(/[\r\n]+/g, " ").replace(/"/g, '""').trim();
      const segments = (draft.segments || []).map(s => ({
        start_ms: s.start_ms,
        end_ms: s.end_ms,
        type: s.type,
        ...(s.intent ? { intent: s.intent } : {}),
      }));
      const segmentsJson = JSON.stringify(segments).replace(/'/g, "'\\''");
      return `python "${SCRIPT_PATH}" --focused --work-dir "${rec.work_dir}" --segments-json '${segmentsJson}' --user-review "${review}"`;
    }

    document.getElementById("marker-close").addEventListener("click", closeMarker);
    document.getElementById("marker-modal-bg").addEventListener("click", e => {
      if (e.target.id === "marker-modal-bg") closeMarker();
    });

    /** Toggle the active segment of the given type (must / audio_only / exclude). */
    function dropMarker(type) {
      if (!markerCurrentId) return;
      const draft = getMarker(markerCurrentId);
      const cur = currentMs();
      if (draft.active) {
        if (draft.active.type !== type) {
          showToast(`Close the open ${typeLetter(draft.active.type)} segment first (or click cancel ×)`);
          return;
        }
        const startMs = draft.active.start_ms;
        const endMs = cur;
        if (endMs <= startMs) {
          showToast("End must be after start — let the video play forward, then drop again");
          return;
        }
        const intent = (draft.active.intent || "").trim();
        const newSeg = { id: newSegId(), type, start_ms: startMs, end_ms: endMs, ...(intent ? { intent } : {}) };
        const segments = (draft.segments || []).concat([newSeg]).sort((a, b) => a.start_ms - b.start_ms);
        setMarker(markerCurrentId, { segments, active: null });
      } else {
        setMarker(markerCurrentId, { active: { type, start_ms: cur, intent: "" } });
      }
      renderMarkerSegments();
    }

    document.getElementById("marker-m-btn").addEventListener("click", () => dropMarker("must"));
    document.getElementById("marker-a-btn").addEventListener("click", () => dropMarker("audio_only"));
    document.getElementById("marker-x-btn").addEventListener("click", () => dropMarker("exclude"));
    document.getElementById("marker-cancel-active").addEventListener("click", () => {
      if (!markerCurrentId) return;
      setMarker(markerCurrentId, { active: null });
      renderMarkerSegments();
    });

    // Active intent typing → persist
    document.getElementById("marker-segments-list").addEventListener("input", e => {
      if (!markerCurrentId) return;
      if (e.target.dataset.action === "active-intent") {
        const draft = getMarker(markerCurrentId);
        if (!draft.active) return;
        setMarker(markerCurrentId, { active: { ...draft.active, intent: e.target.value } });
      }
    });

    // Segment edit / delete / cancel-active
    document.getElementById("marker-segments-list").addEventListener("click", e => {
      if (!markerCurrentId) return;
      const li = e.target.closest("li");
      if (!li) return;
      const action = e.target.dataset.action;
      const segId = li.dataset.segId;

      if (action === "active-cancel") {
        setMarker(markerCurrentId, { active: null });
        renderMarkerSegments();
        return;
      }
      if (action === "seg-del") {
        if (!confirm("Delete this segment?")) return;
        const draft = getMarker(markerCurrentId);
        const segments = (draft.segments || []).filter(s => s.id !== segId);
        setMarker(markerCurrentId, { segments });
        renderMarkerSegments();
        return;
      }
      if (action === "seg-edit") { openSegmentEditor(li, segId); return; }
      if (action === "seg-edit-save") { saveSegmentEditor(li, segId); return; }
      if (action === "seg-edit-cancel") { renderMarkerSegments(); return; }
    });

    function openSegmentEditor(li, segId) {
      const draft = getMarker(markerCurrentId);
      const seg = (draft.segments || []).find(s => s.id === segId);
      if (!seg) return;
      li.innerHTML = `
        <span class="seg-badge ${seg.type}">${seg.type === "must" ? "M" : "S"}</span>
        <div class="seg-edit">
          <input class="time" type="text" data-action="edit-start" value="${escapeHtml(fmtMs(seg.start_ms))}">
          <span>→</span>
          <input class="time" type="text" data-action="edit-end" value="${escapeHtml(fmtMs(seg.end_ms))}">
          <select data-action="edit-type">
            <option value="must" ${seg.type === "must" ? "selected" : ""}>must (frames)</option>
            <option value="audio_only" ${seg.type === "audio_only" ? "selected" : ""}>audio-only</option>
            <option value="exclude" ${seg.type === "exclude" ? "selected" : ""}>exclude</option>
          </select>
          <input class="intent" type="text" data-action="edit-intent" value="${escapeHtml(seg.intent || '')}" placeholder="intent">
          <button class="save" data-action="seg-edit-save">Save</button>
          <button class="cancel" data-action="seg-edit-cancel">Cancel</button>
        </div>
      `;
    }

    function saveSegmentEditor(li, segId) {
      const draft = getMarker(markerCurrentId);
      const startMs = parseTimeToMs(li.querySelector('input[data-action="edit-start"]').value);
      const endMs = parseTimeToMs(li.querySelector('input[data-action="edit-end"]').value);
      const type = li.querySelector('select[data-action="edit-type"]').value;
      const intent = li.querySelector('input[data-action="edit-intent"]').value.trim();
      if (startMs == null || endMs == null) { showToast("Invalid time — use MM:SS or HH:MM:SS"); return; }
      if (endMs <= startMs) { showToast("End must be after start"); return; }
      const segments = (draft.segments || []).map(s => {
        if (s.id !== segId) return s;
        const next = { id: s.id, type, start_ms: startMs, end_ms: endMs };
        if (intent) next.intent = intent;
        return next;
      });
      setMarker(markerCurrentId, { segments });
      renderMarkerSegments();
    }

    // Timeline click — seek
    document.getElementById("marker-timeline").addEventListener("click", e => {
      const dur = durationMs();
      if (!dur || !markerVideo) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const pct = (e.clientX - rect.left) / rect.width;
      markerVideo.currentTime = Math.max(0, Math.min(dur, pct * dur)) / 1000;
      renderMarkerPlayhead();
      refreshFramePlayhead();
    });

    // Frame click — seek
    document.getElementById("marker-frames").addEventListener("click", e => {
      const cell = e.target.closest(".marker-frame");
      if (!cell || !markerVideo) return;
      const pts = Number(cell.dataset.pts);
      if (!isFinite(pts)) return;
      markerVideo.currentTime = pts / 1000;
      renderMarkerPlayhead();
      refreshFramePlayhead();
    });

    // Transcript line click — seek
    document.getElementById("marker-transcript").addEventListener("click", e => {
      const seg = e.target.closest(".seg");
      if (!seg || !markerVideo) return;
      markerVideo.currentTime = Number(seg.dataset.start) / 1000;
      renderMarkerPlayhead();
      refreshFramePlayhead();
    });

    // Manual time inputs (secondary affordance)
    document.getElementById("marker-start").addEventListener("change", e => {
      const ms = parseTimeToMs(e.target.value);
      if (ms == null && e.target.value.trim()) showToast("Invalid time — use MM:SS or HH:MM:SS");
    });
    document.getElementById("marker-end").addEventListener("change", e => {
      const ms = parseTimeToMs(e.target.value);
      if (ms == null && e.target.value.trim()) showToast("Invalid time — use MM:SS or HH:MM:SS");
    });

    // Review textarea — persist on input
    document.getElementById("marker-review").addEventListener("input", e => {
      if (!markerCurrentId) return;
      setMarker(markerCurrentId, { review: e.target.value });
    });

    // Manual add segment (kept as secondary affordance)
    document.getElementById("marker-add").addEventListener("click", () => {
      if (!markerCurrentId) return;
      const startMs = parseTimeToMs(document.getElementById("marker-start").value);
      const endMs = parseTimeToMs(document.getElementById("marker-end").value);
      if (startMs == null || endMs == null) { showToast("Set both start and end times"); return; }
      if (endMs <= startMs) { showToast("End must be after start"); return; }
      const type = document.getElementById("marker-type").value;
      const intent = document.getElementById("marker-intent").value.trim();
      const draft = getMarker(markerCurrentId);
      const segments = (draft.segments || []).concat([
        { id: newSegId(), start_ms: startMs, end_ms: endMs, type, ...(intent ? { intent } : {}) }
      ]).sort((a, b) => a.start_ms - b.start_ms);
      setMarker(markerCurrentId, { segments });
      document.getElementById("marker-start").value = "";
      document.getElementById("marker-end").value = "";
      document.getElementById("marker-intent").value = "";
      renderMarkerSegments();
    });

    // Send focused job — server mode posts to /api/focused; static mode falls
    // back to copying the CLI command. Phase 11: `variant` routes output to a
    // slot (main / variant-a / variant-b) so users can A/B compare results.
    function sendFocused(variant) {
      if (!markerCurrentId) return;
      const rec = RECORDS.find(r => r.id === markerCurrentId);
      if (!rec) return;

      if (SERVER_MODE) {
        const draft = getMarker(markerCurrentId);
        const segments = (draft.segments || []).map(s => {
          const out = { start_ms: s.start_ms, end_ms: s.end_ms, type: s.type };
          if (s.intent) out.intent = s.intent;
          return out;
        });
        const review = (draft.review || "").replace(/[\r\n]+/g, " ").trim();
        const autoSynthCheckbox = document.getElementById("auto-synth-checkbox");
        const autoSynthesize = !!(claudeState.available && autoSynthCheckbox && autoSynthCheckbox.checked);
        const slotLabel = variant === "variant-a" ? " → Variant A"
                        : variant === "variant-b" ? " → Variant B" : "";
        fetch("/api/focused", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            work_dir: rec.work_dir,
            segments,
            user_review: review,
            auto_synthesize: autoSynthesize,
            variant,
          }),
        }).then(r => {
          if (!r.ok) return r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); });
          return r.json();
        }).then(data => {
          closeMarker();
          openJobModal(
            data.job_id,
            (autoSynthesize ? "Focused extraction + synthesis" : "Focused extraction") + slotLabel,
            autoSynthesize
              ? "Phase 1/2 starting — extraction; phase 2/2 chains Claude Code synthesis."
              : "Extracting marked frames and filtering transcript…",
            { focused_report_path: data.focused_report_path, rec_id: rec.id, auto_synthesize: autoSynthesize, variant }
          );
        }).catch(err => {
          showToast("Server rejected focused job: " + err.message);
        });
      } else {
        // Static mode: variant slots aren't supported (server required). Fall
        // back to copying the main command — user can re-run with env tweaks.
        const cmd = buildFocusedCommand(rec);
        navigator.clipboard.writeText(cmd).then(
          () => showToast("Focused command copied — paste into Claude Code to process"),
          () => showToast("Couldn't copy — check clipboard permissions")
        );
      }
    }

    document.getElementById("marker-copy").addEventListener("click", () => sendFocused("main"));

    // Variant menu (popover above the Send button) — server mode only.
    const variantBtn = document.getElementById("marker-variant-btn");
    const variantMenu = document.getElementById("marker-variant-menu");
    if (variantBtn) {
      variantBtn.addEventListener("click", e => {
        e.stopPropagation();
        variantMenu.hidden = !variantMenu.hidden;
      });
      variantMenu.addEventListener("click", e => {
        const slot = e.target.dataset.variant;
        if (!slot) return;
        variantMenu.hidden = true;
        sendFocused(slot);
      });
      // Click-outside closes the popover.
      document.addEventListener("click", e => {
        if (variantMenu.hidden) return;
        if (e.target.closest("#marker-variant-menu") || e.target === variantBtn) return;
        variantMenu.hidden = true;
      });
    }

    // Filter wiring + persistence
    function bindFilter(elId, setter) {
      document.getElementById(elId).addEventListener("input", e => {
        setter(e.target.value);
        persistUI(); fullRender();
      });
      document.getElementById(elId).addEventListener("change", e => {
        setter(e.target.value);
        persistUI(); fullRender();
      });
    }
    bindFilter("q", v => q = v);
    bindFilter("filter-transcript", v => filterTranscript = v);
    bindFilter("filter-status", v => filterStatus = v);
    bindFilter("filter-nlm", v => filterNlm = v);
    bindFilter("filter-project", v => filterProject = v);

    document.getElementById("clear-filters").addEventListener("click", () => {
      q = ""; filterTranscript = ""; filterStatus = ""; filterNlm = ""; filterProject = "";
      ["q","filter-transcript","filter-status","filter-nlm","filter-project"].forEach(id => document.getElementById(id).value = "");
      persistUI(); fullRender();
    });

    // Sort
    document.querySelectorAll("th[data-sort]").forEach(th => {
      th.addEventListener("click", () => {
        const k = th.dataset.sort;
        if (sortKey === k) sortDir *= -1;
        else { sortKey = k; sortDir = -1; }
        document.querySelectorAll("th .arrow").forEach(a => a.remove());
        const arrow = document.createElement("span");
        arrow.className = "arrow";
        arrow.textContent = sortDir === -1 ? "▼" : "▲";
        th.appendChild(arrow);
        persistUI(); fullRender();
      });
    });

    // Keyboard
    document.addEventListener("keydown", e => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA" || e.target.tagName === "SELECT") return;
      const markerOpen = document.getElementById("marker-modal-bg").classList.contains("show");
      if (markerOpen && (e.key === "m" || e.key === "M")) {
        e.preventDefault(); dropMarker("must"); return;
      }
      if (markerOpen && (e.key === "a" || e.key === "A")) {
        e.preventDefault(); dropMarker("audio_only"); return;
      }
      if (markerOpen && (e.key === "x" || e.key === "X")) {
        e.preventDefault(); dropMarker("exclude"); return;
      }
      if (e.key === "/") {
        e.preventDefault();
        document.getElementById("q").focus();
        document.getElementById("q").select();
      } else if (e.key === "Escape") {
        document.getElementById("nlm-modal-bg").classList.remove("show");
        document.getElementById("img-modal-bg").classList.remove("show");
        document.getElementById("result-modal-bg").classList.remove("show");
        document.getElementById("projects-modal-bg").classList.remove("show");
        document.getElementById("confirm-modal-bg").classList.remove("show");
        document.getElementById("orphans-modal-bg").classList.remove("show");
        document.getElementById("disk-modal-bg").classList.remove("show");
        closeCompareModal();
        closeMarker();
      }
    });

    // ── Compare-variants modal (Phase 11) ────────────────────────────────────
    let compareCurrentId = null;
    let compareShowResult = false;

    function compareSlotsFor(id) {
      const vSummaries = VARIANT_SUMMARIES[id] || {};
      const vFocused = VARIANT_FOCUSED_RESULTS[id] || {};
      const slots = [];
      if (SUMMARIES[id] || FOCUSED_RESULTS[id]) slots.push("main");
      for (const slot of ["variant-a", "variant-b"]) {
        if (vSummaries[slot] || vFocused[slot]) slots.push(slot);
      }
      return slots;
    }

    function openCompareModal(id) {
      const rec = RECORDS.find(r => r.id === id);
      if (!rec) return;
      compareCurrentId = id;
      document.getElementById("compare-modal-title").textContent =
        `Compare variants — ${rec.title || rec.id}`;
      document.getElementById("compare-show-result").checked = compareShowResult;
      renderCompareColumns();
      document.getElementById("compare-modal-bg").classList.add("show");
    }

    function closeCompareModal() {
      document.getElementById("compare-modal-bg").classList.remove("show");
      compareCurrentId = null;
    }

    function renderCompareColumns() {
      if (!compareCurrentId) return;
      const id = compareCurrentId;
      const rec = RECORDS.find(r => r.id === id);
      if (!rec) { closeCompareModal(); return; }
      const variants = rec.variants || {};
      const vSummaries = VARIANT_SUMMARIES[id] || {};
      const vFocused = VARIANT_FOCUSED_RESULTS[id] || {};
      const slots = compareSlotsFor(id);
      const cols = document.getElementById("compare-columns");
      cols.classList.remove("cols-1", "cols-2", "cols-3");
      cols.classList.add(`cols-${Math.min(slots.length, 3)}`);

      const labelFor = s => s === "main" ? "Main"
                          : s === "variant-a" ? "Variant A" : "Variant B";

      cols.innerHTML = slots.map(slot => {
        const isMain = slot === "main";
        const meta = variants[slot];
        const ts = meta && meta.saved_at
          ? meta.saved_at.replace("T", " ").replace(/\+.*$/, "")
          : "";
        const badge = meta
          ? `${meta.model || "?"} · ${meta.effort || "?"}${ts ? " · " + ts : ""}`
          : "(no metadata)";
        const content = compareShowResult
          ? (isMain ? (FOCUSED_RESULTS[id] || "") : (vFocused[slot] || ""))
          : (isMain ? (SUMMARIES[id] || "") : (vSummaries[slot] || ""));
        const fileLabel = compareShowResult ? "focused-result" : "nlm-summary";
        const bodyClass = content ? "compare-col-body" : "compare-col-body empty";
        const bodyText = content || `(no ${fileLabel} for this slot)`;
        const delBtn = isMain
          ? ""
          : `<button data-action="compare-delete" data-slot="${slot}" class="del">Delete</button>`;
        return `<div class="compare-col" data-slot="${slot}">
          <div class="compare-col-head">
            <span class="compare-col-name">${labelFor(slot)}</span>
            <span class="compare-badge">${escapeHtml(badge)}</span>
          </div>
          <pre class="${bodyClass}">${escapeHtml(bodyText)}</pre>
          <div class="compare-col-foot">
            <button data-action="compare-copy" data-slot="${slot}">Copy</button>
            ${delBtn}
          </div>
        </div>`;
      }).join("");
    }

    document.getElementById("compare-close").addEventListener("click", closeCompareModal);
    document.getElementById("compare-modal-bg").addEventListener("click", e => {
      if (e.target.id === "compare-modal-bg") closeCompareModal();
    });
    document.getElementById("compare-show-result").addEventListener("change", e => {
      compareShowResult = !!e.target.checked;
      renderCompareColumns();
    });
    document.getElementById("compare-columns").addEventListener("click", e => {
      const action = e.target.dataset.action;
      if (!action) return;
      const slot = e.target.dataset.slot;
      if (action === "compare-copy") {
        const col = e.target.closest(".compare-col");
        if (!col) return;
        const text = col.querySelector(".compare-col-body").textContent;
        navigator.clipboard.writeText(text).then(
          () => showToast(`Copied ${slot === "main" ? "Main" : (slot === "variant-a" ? "Variant A" : "Variant B")}`),
          () => showToast("Couldn't copy — check clipboard permissions")
        );
      } else if (action === "compare-delete") {
        if (!compareCurrentId || slot === "main") return;
        if (!SERVER_MODE) { showToast("Server required to delete variants"); return; }
        const slotLabel = slot === "variant-a" ? "Variant A" : "Variant B";
        if (!confirm(`Delete ${slotLabel}? This removes its output files and metadata. Cannot be undone.`)) return;
        fetch(`/api/records/${encodeURIComponent(compareCurrentId)}/variants/${slot}`, { method: "DELETE" })
          .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); }))
          .then(() => {
            showToast(`Deleted ${slotLabel}`);
            refreshManifest();
            // Re-render after manifest update; close if fewer than 2 slots remain.
            setTimeout(() => {
              if (!compareCurrentId) return;
              if (compareSlotsFor(compareCurrentId).length < 2) closeCompareModal();
              else renderCompareColumns();
            }, 800);
          })
          .catch(err => showToast("Delete failed: " + err.message));
      }
    });

    // ── URL input bar — generate /watch --preview command from a pasted URL ──
    function isPlausibleUrl(s) {
      if (!s) return false;
      const trimmed = String(s).trim();
      if (!trimmed) return false;
      // yt-dlp accepts more than http(s), but for the dashboard's UX a basic
      // sanity check is enough — the user's terminal will reject anything bogus.
      return /^https?:\/\/\S+$/i.test(trimmed);
    }
    document.getElementById("url-add-btn").addEventListener("click", submitUrl);
    document.getElementById("url-input").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); submitUrl(); }
    });
    function submitUrl() {
      const input = document.getElementById("url-input");
      const url = input.value.trim();
      if (!isPlausibleUrl(url)) {
        showToast("That doesn't look like a video URL — paste a full http(s) URL");
        return;
      }
      if (SERVER_MODE) {
        fetch("/api/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url }),
        }).then(r => {
          if (!r.ok) return r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); });
          return r.json();
        }).then(data => {
          input.value = "";
          openJobModal(data.job_id, "Preview", "Downloading and extracting sparse frames…", null);
        }).catch(err => {
          showToast("Server rejected the preview request: " + err.message);
        });
      } else {
        const cmd = `python "${SCRIPT_PATH}" --preview "${url}"`;
        navigator.clipboard.writeText(cmd).then(
          () => { showToast("Command copied — paste in your terminal to start the preview"); input.value = ""; },
          () => showToast("Couldn't copy — check clipboard permissions")
        );
      }
    }

    // ── Manage projects modal ─────────────────────────────────────────────────
    function openProjects() {
      renderProjectsList();
      document.getElementById("projects-add-input").value = "";
      document.getElementById("projects-modal-bg").classList.add("show");
    }
    function closeProjects() {
      document.getElementById("projects-modal-bg").classList.remove("show");
    }
    function renderProjectsList() {
      const ul = document.getElementById("projects-list");
      ul.innerHTML = PROJECT_TAGS.map(name => {
        const isNone = name === "None";
        return `<li data-name="${escapeHtml(name)}">
          <span class="proj-name">${isNone ? "(none) — default, can't edit" : escapeHtml(name)}</span>
          ${isNone
            ? `<span class="proj-locked">locked</span>`
            : `<button class="icon" data-action="proj-rename" title="Rename">rename</button>
               <button class="icon del" data-action="proj-del" title="Delete">delete</button>`}
        </li>`;
      }).join("");
    }
    function commitProjectsChange(newList) {
      PROJECT_TAGS = newList;
      saveProjects(PROJECT_TAGS);
      // Rebuild the row dropdowns + project filter to reflect changes
      const projSel = document.getElementById("filter-project");
      projSel.innerHTML = "";
      const allOpt = document.createElement("option");
      allOpt.value = ""; allOpt.textContent = "All projects";
      projSel.appendChild(allOpt);
      PROJECT_TAGS.forEach(t => {
        const opt = document.createElement("option");
        opt.value = t === "None" ? "(none)" : t;
        opt.textContent = t === "None" ? "(no tag)" : t;
        projSel.appendChild(opt);
      });
      projSel.value = filterProject;
      fullRender();
      renderProjectsList();
    }
    function countTagged(tag) {
      return RECORDS.reduce((n, r) => n + (getAnnot(r.id).project_tag === tag ? 1 : 0), 0);
    }
    function clearTagFromAll(tag) {
      let changed = false;
      RECORDS.forEach(r => {
        const a = getAnnot(r.id);
        if (a.project_tag === tag) { setAnnot(r.id, { project_tag: "None" }); changed = true; }
      });
      return changed;
    }
    function renameTagOnAll(oldName, newName) {
      RECORDS.forEach(r => {
        const a = getAnnot(r.id);
        if (a.project_tag === oldName) setAnnot(r.id, { project_tag: newName });
      });
    }
    function validateProjectName(name, currentList, exclude) {
      const trimmed = name.trim();
      if (!trimmed) return "Name can't be empty";
      if (trimmed.length > 32) return "Max 32 chars";
      if (currentList.some(t => t === trimmed && t !== exclude)) return "That name is already in use";
      return null;
    }

    document.getElementById("manage-projects-btn").addEventListener("click", openProjects);
    document.getElementById("projects-close").addEventListener("click", closeProjects);
    document.getElementById("projects-modal-bg").addEventListener("click", e => {
      if (e.target.id === "projects-modal-bg") closeProjects();
    });

    document.getElementById("projects-add-btn").addEventListener("click", () => {
      const input = document.getElementById("projects-add-input");
      const name = input.value;
      const err = validateProjectName(name, PROJECT_TAGS, null);
      if (err) { showToast(err); return; }
      commitProjectsChange(PROJECT_TAGS.concat([name.trim()]));
      input.value = "";
    });
    document.getElementById("projects-add-input").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); document.getElementById("projects-add-btn").click(); }
    });

    document.getElementById("projects-list").addEventListener("click", e => {
      const li = e.target.closest("li");
      if (!li) return;
      const name = li.dataset.name;
      const action = e.target.dataset.action;
      if (!name) return;

      if (action === "proj-del") {
        const used = countTagged(name);
        const msg = used > 0
          ? `${used} ${used === 1 ? "row is" : "rows are"} tagged "${name}". Delete the project and clear those tags?`
          : `Delete project "${name}"?`;
        if (!confirm(msg)) return;
        if (used > 0) clearTagFromAll(name);
        commitProjectsChange(PROJECT_TAGS.filter(t => t !== name));
        return;
      }
      if (action === "proj-rename") {
        const span = li.querySelector(".proj-name");
        span.classList.add("editing");
        span.innerHTML = `<input type="text" maxlength="32" value="${escapeHtml(name)}">`;
        const inp = span.querySelector("input");
        inp.focus(); inp.select();
        let done = false;
        const finish = (commit) => {
          if (done) return;
          done = true;
          if (!commit) { renderProjectsList(); return; }
          const next = inp.value;
          const err = validateProjectName(next, PROJECT_TAGS, name);
          if (err) { showToast(err); done = false; inp.focus(); return; }
          if (next.trim() !== name) {
            renameTagOnAll(name, next.trim());
            commitProjectsChange(PROJECT_TAGS.map(t => t === name ? next.trim() : t));
          } else {
            renderProjectsList();
          }
        };
        inp.addEventListener("keydown", ev => {
          if (ev.key === "Enter") { ev.preventDefault(); finish(true); }
          else if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
        });
        inp.addEventListener("blur", () => finish(true), { once: true });
        return;
      }
    });

    document.getElementById("projects-reset").addEventListener("click", () => {
      if (!confirm("Reset project tags to the seeded defaults? Existing row tags are preserved (they'll show as 'orphan' tags until you re-add them).")) return;
      commitProjectsChange(PROJECT_TAGS_SEED.slice());
    });

    // ── Server mode: job modal, manifest poll, connection indicator ──────────
    let jobPollHandle = null;
    let currentJobId = null;
    let currentJobMeta = null;
    let activeLogChannel = "extract";        // which tab the job-modal renders
    let autoSwitchedToSynthesis = false;     // one-shot per-job flag
    let jobAutoCloseHandle = null;
    const claudeState = { available: false, path: null };

    function openJobModal(jobId, title, detail, meta) {
      currentJobId = jobId;
      currentJobMeta = meta || null;
      activeLogChannel = "extract";
      autoSwitchedToSynthesis = false;
      if (jobAutoCloseHandle) { clearTimeout(jobAutoCloseHandle); jobAutoCloseHandle = null; }

      document.getElementById("job-modal-title").textContent = title;
      document.getElementById("job-detail").textContent = detail || "";
      const status = document.getElementById("job-status");
      status.textContent = "queued";
      status.className = "job-status queued";
      document.getElementById("job-log").textContent = "";
      document.getElementById("job-log-tabs").classList.remove("show");
      document.querySelectorAll("#job-log-tabs .tab").forEach(t => {
        t.classList.toggle("active", t.dataset.channel === "extract");
      });
      document.getElementById("job-actions").classList.remove("show");
      document.getElementById("job-cancel").style.display = "inline-block";
      document.getElementById("job-progress").style.display = "none";
      document.getElementById("job-modal-bg").classList.add("show");

      stopJobPoll();
      jobPollHandle = setInterval(() => pollJob(jobId), 1500);
      pollJob(jobId);
    }

    function closeJobModal() {
      stopJobPoll();
      if (jobAutoCloseHandle) { clearTimeout(jobAutoCloseHandle); jobAutoCloseHandle = null; }
      currentJobId = null;
      currentJobMeta = null;
      document.getElementById("job-modal-bg").classList.remove("show");
    }

    function stopJobPoll() {
      if (jobPollHandle) { clearInterval(jobPollHandle); jobPollHandle = null; }
    }

    function setActiveLogChannel(channel, skipRender) {
      activeLogChannel = channel;
      document.querySelectorAll("#job-log-tabs .tab").forEach(t => {
        t.classList.toggle("active", t.dataset.channel === channel);
      });
      if (!skipRender && currentJobId) pollJob(currentJobId);
    }

    function jobModalTitle(job) {
      if (job.kind === "preview") {
        if (job.status === "extracting" || job.status === "queued") return "Preview: extracting frames";
        if (job.status === "done") return "Preview ready";
        if (job.status === "failed") return "Preview failed";
        return "Preview";
      }
      if (job.status === "extracting" || job.status === "queued") return "Phase 1/2: Extracting frames + transcript";
      if (job.status === "synthesizing") {
        // Surface elapsed time so the user can see progress even when
        // claude -p is silent during tool use.
        const elapsedTxt = (job.synthesis_elapsed_sec != null)
          ? ` · ${Math.round(job.synthesis_elapsed_sec)}s elapsed`
          : "";
        return `Phase 2/2: Synthesizing via Claude Code${elapsedTxt}`;
      }
      if (job.status === "done") return "Done";
      if (job.status === "failed") return "Failed";
      return "Focused job";
    }

    function pollJob(jobId) {
      fetch(`/api/jobs/${jobId}`)
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then(job => {
          setConnIndicator(true);
          const status = document.getElementById("job-status");
          status.textContent = job.status;
          status.className = "job-status " + job.status;
          document.getElementById("job-modal-title").textContent = jobModalTitle(job);

          // Show Extract/Synthesis tabs once the focused job has any synthesis state
          const tabs = document.getElementById("job-log-tabs");
          const hasSynthesis =
            job.status === "synthesizing" ||
            (job.synthesis_log_tail && job.synthesis_log_tail.length > 0) ||
            (job.kind === "focused" && (job.status === "done" || job.status === "failed") && job.auto_synthesize);
          tabs.classList.toggle("show", !!hasSynthesis);

          // Auto-switch to synthesis tab the first time we see that phase
          if (!autoSwitchedToSynthesis && job.status === "synthesizing") {
            setActiveLogChannel("synthesis", true);
            autoSwitchedToSynthesis = true;
          }

          // Render the active channel. Surface a help line when the
          // synthesis tail is empty during synthesis — claude -p is silent
          // during tool use, so without this the modal looks stuck.
          const log = document.getElementById("job-log");
          const tail = activeLogChannel === "synthesis"
            ? (job.synthesis_log_tail || [])
            : (job.extract_log_tail || []);
          if (activeLogChannel === "synthesis" && tail.length === 0 && job.status === "synthesizing") {
            log.innerHTML = '<span class="empty-msg">claude -p is silent during tool use. File saves appear here when Claude calls Write.</span>';
          } else {
            const wasAtBottom = (log.scrollTop + log.clientHeight) >= (log.scrollHeight - 4);
            log.textContent = tail.join("\n");
            if (wasAtBottom) log.scrollTop = log.scrollHeight;
          }

          // Indeterminate progress bar runs while either phase is active.
          const progress = document.getElementById("job-progress");
          progress.style.display =
            (job.status === "extracting" || job.status === "synthesizing") ? "block" : "none";

          if (job.status === "done") {
            stopJobPoll();
            document.getElementById("job-cancel").style.display = "none";
            renderJobActions(job);
            // Refresh manifest, then auto-close (so the row's own buttons take over)
            setTimeout(refreshManifest, 800);
            if (jobAutoCloseHandle) clearTimeout(jobAutoCloseHandle);
            jobAutoCloseHandle = setTimeout(() => {
              if (currentJobId === jobId) closeJobModal();
            }, 2500);
          } else if (job.status === "failed") {
            stopJobPoll();
            document.getElementById("job-cancel").style.display = "none";
            log.textContent += "\n\n[ERROR] " + (job.error || "job failed");
          }
        })
        .catch(err => {
          setConnIndicator(false);
          const log = document.getElementById("job-log");
          log.textContent += "\n[poll error] " + err.message;
        });
    }

    function renderJobActions(job) {
      const actions = document.getElementById("job-actions");
      const result = job.result || {};
      const recId = currentJobMeta && currentJobMeta.rec_id;
      const synthesisDone = !!result.focused_result_path;

      const copyPrompt = document.getElementById("job-copy-prompt");
      const showReport = document.getElementById("job-show-report");
      const openResult = document.getElementById("job-open-result");
      const openNlm = document.getElementById("job-open-nlm");

      // Extract-only (no auto-synthesis): paste-prompt is the right CTA.
      // Auto-synthesized: Open Result / Open NLM are the right CTAs.
      copyPrompt.style.display = (result.focused_report_path && !synthesisDone) ? "inline-block" : "none";
      showReport.style.display = result.focused_report_path ? "inline-block" : "none";
      openResult.style.display = (synthesisDone && recId) ? "inline-block" : "none";
      openNlm.style.display = (synthesisDone && recId) ? "inline-block" : "none";

      const anyVisible = [copyPrompt, showReport, openResult, openNlm]
        .some(el => el.style.display === "inline-block");
      actions.classList.toggle("show", anyVisible);
    }

    function updateClaudeUI() {
      const toggle = document.getElementById("auto-synth-toggle");
      const checkbox = document.getElementById("auto-synth-checkbox");
      const btn = document.getElementById("marker-copy");
      const vBtn = document.getElementById("marker-variant-btn");
      if (!SERVER_MODE) {
        toggle.classList.remove("show");
        if (btn) btn.textContent = "Copy /watch --focused command";
        if (vBtn) vBtn.hidden = true;
        return;
      }
      toggle.classList.add("show");
      if (vBtn) vBtn.hidden = false;
      if (claudeState.available) {
        checkbox.disabled = false;
        toggle.classList.remove("disabled");
        toggle.title = `Claude CLI: ${claudeState.path}`;
        if (btn) btn.textContent = checkbox.checked ? "Send to Claude (auto)" : "Send to Claude (extract only)";
      } else {
        checkbox.disabled = true;
        checkbox.checked = false;
        toggle.classList.add("disabled");
        toggle.title = "Claude CLI not found on PATH — auto-synthesis disabled";
        if (btn) btn.textContent = "Send to Claude (extract only)";
      }
    }

    if (SERVER_MODE) {
      document.getElementById("job-close").addEventListener("click", closeJobModal);
      document.getElementById("job-modal-bg").addEventListener("click", e => {
        if (e.target.id === "job-modal-bg") closeJobModal();
      });
      document.getElementById("job-cancel").addEventListener("click", () => {
        if (!currentJobId) return;
        fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" })
          .then(() => showToast("Cancel sent"))
          .catch(() => showToast("Couldn't reach server to cancel"));
      });
      document.getElementById("job-copy-prompt").addEventListener("click", () => {
        if (!currentJobMeta || !currentJobMeta.focused_report_path) return;
        const path = currentJobMeta.focused_report_path;
        const prompt = `Read "${path}" and follow the /watch SKILL.md NLM template. Save focused-result.md and nlm-summary.md to the same work_dir.`;
        navigator.clipboard.writeText(prompt).then(
          () => showToast("Prompt copied — paste in Cowork or Claude Code"),
          () => showToast("Couldn't copy")
        );
      });
      document.getElementById("job-show-report").addEventListener("click", () => {
        if (!currentJobMeta || !currentJobMeta.focused_report_path) return;
        const url = fileUri(currentJobMeta.focused_report_path);
        fetch(url)
          .then(r => r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`)))
          .then(text => { document.getElementById("job-log").textContent = text; })
          .catch(err => showToast("Couldn't load focused report: " + err.message));
      });
      document.getElementById("job-open-result").addEventListener("click", () => {
        const recId = currentJobMeta && currentJobMeta.rec_id;
        if (!recId) return;
        closeJobModal();
        refreshManifest();
        setTimeout(() => {
          const result = FOCUSED_RESULTS[recId];
          const rec = RECORDS.find(r => r.id === recId);
          if (!result) { showToast("Result not loaded yet — try the row's Result button"); return; }
          document.getElementById("result-modal-title").textContent =
            (rec && rec.title) ? `Result — ${rec.title}` : "Focused result";
          document.getElementById("result-modal-body").textContent = result;
          document.getElementById("result-modal-bg").classList.add("show");
        }, 600);
      });
      document.getElementById("job-open-nlm").addEventListener("click", () => {
        const recId = currentJobMeta && currentJobMeta.rec_id;
        if (!recId) return;
        closeJobModal();
        refreshManifest();
        setTimeout(() => {
          const summary = SUMMARIES[recId];
          const rec = RECORDS.find(r => r.id === recId);
          if (!summary) { showToast("NLM summary not loaded yet — try the row's NLM button"); return; }
          document.getElementById("nlm-modal-title").textContent = (rec && rec.title) || "NLM Summary";
          document.getElementById("nlm-modal-body").textContent = summary;
          document.getElementById("nlm-modal-bg").classList.add("show");
        }, 600);
      });

      // Log-tab switching
      document.querySelectorAll("#job-log-tabs .tab").forEach(t => {
        t.addEventListener("click", () => setActiveLogChannel(t.dataset.channel));
      });

      // Auto-synth toggle reactivity
      document.getElementById("auto-synth-checkbox").addEventListener("change", updateClaudeUI);

      // Probe /api/health to learn whether the claude CLI is on the server's PATH.
      fetch("/api/health")
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data) {
            claudeState.available = !!data.claude_available;
            claudeState.path = data.claude_path;
          }
          updateClaudeUI();
        })
        .catch(() => updateClaudeUI());
    } else {
      // Static mode: keep the original button label.
      updateClaudeUI();
    }

    // Phase 12: URL input placeholder text varies by mode. Static-mode users
    // need to paste the generated command into their terminal; server-mode
    // users have it submitted for them — same input box, opposite affordance.
    const URL_PLACEHOLDER_SERVER = "Paste video URL to start preview…";
    const URL_PLACEHOLDER_STATIC = "Paste video URL — generates the /watch --preview command for you to paste in your terminal";

    function updateUrlPlaceholder(serverReachable) {
      const input = document.getElementById("url-input");
      if (!input) return;
      // Treat undefined (initial state, before any /api/* response) as
      // "reachable" so server-mode users see the server placeholder without
      // a flicker between page load and first poll.
      const useServer = SERVER_MODE && serverReachable !== false;
      input.placeholder = useServer ? URL_PLACEHOLDER_SERVER : URL_PLACEHOLDER_STATIC;
    }

    function setConnIndicator(ok) {
      const ind = document.getElementById("conn-indicator");
      if (!ind) return;
      ind.classList.add("show");
      ind.classList.toggle("ok", !!ok);
      ind.classList.toggle("fail", !ok);
      ind.title = ok ? "Server connected" : "Server unreachable — falling back to embedded data";
      updateUrlPlaceholder(!!ok);
    }

    // Initial pass — sets the server-mode placeholder right after page load so
    // server-mode users don't see the static command-paste hint before the
    // first /api/* response.
    updateUrlPlaceholder(undefined);

    /** Pull the live manifest in server mode. Replaces RECORDS/SUMMARIES/etc.
     *  and re-renders. No-op in static mode. */
    function refreshManifest() {
      if (!SERVER_MODE) return;
      fetch("/api/manifest")
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then(data => {
          setConnIndicator(true);
          if (Array.isArray(data.records)) RECORDS = data.records;
          if (data.summaries) SUMMARIES = data.summaries;
          if (data.focused_results) FOCUSED_RESULTS = data.focused_results;
          if (data.variant_summaries) VARIANT_SUMMARIES = data.variant_summaries;
          if (data.variant_focused_results) VARIANT_FOCUSED_RESULTS = data.variant_focused_results;
          if (data.previews) PREVIEWS = data.previews;
          fullRender();
        })
        .catch(() => setConnIndicator(false));
    }

    if (SERVER_MODE) {
      // First refresh shortly after load (gives bootstrap data time to render),
      // then poll every 10s. The job modal also triggers a refresh on completion.
      setTimeout(refreshManifest, 500);
      setInterval(refreshManifest, 10000);
    }

    // ── Phase 7: confirm modal, delete, orphans, disk ────────────────────────

    function openConfirm({ title, message, detail, confirmLabel, onConfirm }) {
      document.getElementById("confirm-modal-title").textContent = title || "Delete?";
      document.getElementById("confirm-modal-message").textContent = message || "";
      const detailEl = document.getElementById("confirm-modal-detail");
      if (detail && detail.length) {
        detailEl.style.display = "block";
        detailEl.innerHTML = detail.map(d =>
          `<div class="row"><span class="label">${escapeHtml(d.label)}</span><span class="value">${escapeHtml(d.value)}</span></div>`
        ).join("");
      } else {
        detailEl.style.display = "none";
        detailEl.innerHTML = "";
      }
      const confirmBtn = document.getElementById("confirm-modal-confirm");
      confirmBtn.textContent = confirmLabel || "Delete";
      confirmCallback = onConfirm || null;
      document.getElementById("confirm-modal-bg").classList.add("show");
    }
    function closeConfirm() {
      document.getElementById("confirm-modal-bg").classList.remove("show");
      confirmCallback = null;
    }
    document.getElementById("confirm-modal-close").addEventListener("click", closeConfirm);
    document.getElementById("confirm-modal-cancel").addEventListener("click", closeConfirm);
    document.getElementById("confirm-modal-bg").addEventListener("click", e => {
      if (e.target.id === "confirm-modal-bg") closeConfirm();
    });
    document.getElementById("confirm-modal-confirm").addEventListener("click", () => {
      const cb = confirmCallback;
      closeConfirm();
      if (cb) cb();
    });

    /** Drop annotations + marker drafts for ids that no longer exist. */
    function pruneLocalStateFor(ids) {
      let stateChanged = false;
      let markersChanged = false;
      ids.forEach(id => {
        if (state[id]) { delete state[id]; stateChanged = true; }
        if (markers[id]) { delete markers[id]; markersChanged = true; }
        selectedIds.delete(id);
      });
      if (stateChanged) saveState(state);
      if (markersChanged) saveMarkers(markers);
    }

    /** Look up disk size for a record (from the last /api/disk poll). */
    function diskSizeFor(id) {
      const e = (diskState.by_record || []).find(x => x.id === id);
      return e ? e.bytes : null;
    }

    function deleteRecordFlow(rec) {
      const sizeBytes = diskSizeFor(rec.id);
      const detail = [
        { label: "Title", value: rec.title || "(no title)" },
        { label: "Work dir", value: rec.work_dir || "(none)" },
      ];
      if (sizeBytes != null) detail.push({ label: "Disk size", value: fmtBytes(sizeBytes) });

      openConfirm({
        title: "Delete video?",
        message: "Delete this record and the files in its work-dir.",
        detail,
        confirmLabel: "Delete",
        onConfirm: () => doDeleteRecord(rec),
      });
    }

    function doDeleteRecord(rec) {
      if (SERVER_MODE) {
        fetch(`/api/records/${encodeURIComponent(rec.id)}`, { method: "DELETE" })
          .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); }))
          .then(data => {
            pruneLocalStateFor([rec.id]);
            RECORDS = RECORDS.filter(x => x.id !== rec.id);
            showToast(`Deleted — freed ${fmtBytes(data.freed_bytes || 0)}`);
            fullRender();
            refreshDisk();
          })
          .catch(err => {
            const m = String(err.message || err);
            if (m.includes("409") || m.includes("job in progress")) {
              showToast("Cannot delete — job in progress. Wait or cancel first.");
            } else if (m.includes("rmtree") || m.includes("locked")) {
              showToast("Some files locked. Close any open previews and try again.");
            } else {
              showToast("Delete failed: " + m);
            }
          });
      } else {
        const cmd = `Remove-Item -Recurse -Force "${rec.work_dir}"`;
        navigator.clipboard.writeText(cmd).then(
          () => showToast("Command copied — paste in terminal. Refresh dashboard after."),
          () => showToast("Couldn't copy — check clipboard permissions")
        );
      }
    }

    function bulkDeleteFlow() {
      if (selectedIds.size === 0) return;
      const ids = Array.from(selectedIds);
      const totalBytes = ids.reduce((sum, id) => {
        const v = diskSizeFor(id);
        return sum + (v || 0);
      }, 0);
      const detail = [
        { label: "Count", value: `${ids.length} videos` },
        { label: "Estimated free", value: totalBytes > 0 ? fmtBytes(totalBytes) : "(server will report)" },
      ];
      openConfirm({
        title: `Delete ${ids.length} videos?`,
        message: "Bulk delete all selected records and their files.",
        detail,
        confirmLabel: `Delete ${ids.length}`,
        onConfirm: () => doBulkDelete(ids),
      });
    }

    function doBulkDelete(ids) {
      if (!SERVER_MODE) {
        // Fallback: single multi-line PowerShell command listing each work_dir.
        const lines = ids.map(id => {
          const r = RECORDS.find(x => x.id === id);
          return r ? `Remove-Item -Recurse -Force "${r.work_dir}"` : null;
        }).filter(Boolean);
        navigator.clipboard.writeText(lines.join("\n")).then(
          () => showToast(`${lines.length} commands copied — paste in terminal.`),
          () => showToast("Couldn't copy")
        );
        return;
      }
      fetch("/api/records/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      })
        .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); }))
        .then(data => {
          const okIds = data.results.filter(x => x.deleted).map(x => x.id);
          const failed = data.results.filter(x => !x.deleted);
          pruneLocalStateFor(okIds);
          RECORDS = RECORDS.filter(x => !okIds.includes(x.id));
          selectedIds.clear();
          showToast(`Deleted ${okIds.length}/${data.results.length} — freed ${fmtBytes(data.total_freed_bytes || 0)}`);
          if (failed.length) {
            console.warn("bulk-delete failures:", failed);
          }
          fullRender();
          refreshDisk();
        })
        .catch(err => showToast("Bulk delete failed: " + err.message));
    }

    function updateBulkBar() {
      const bar = document.getElementById("bulk-bar");
      const count = selectedIds.size;
      if (!SERVER_MODE || count === 0) {
        bar.classList.remove("show");
        return;
      }
      bar.classList.add("show");
      document.getElementById("bulk-count").textContent = `${count} selected`;
      document.getElementById("bulk-delete-btn").textContent = `Delete ${count} selected`;
    }

    // Header "select all" — affects only currently filtered rows.
    document.getElementById("select-all-rows").addEventListener("change", e => {
      const checked = e.target.checked;
      const visibleIds = Array.from(document.querySelectorAll("#rows tr"))
        .map(tr => tr.dataset.id)
        .filter(Boolean);
      visibleIds.forEach(id => {
        if (checked) selectedIds.add(id);
        else selectedIds.delete(id);
      });
      // Update each row's checkbox without full re-render
      document.querySelectorAll('#rows input[data-action="select-row"]').forEach(cb => {
        cb.checked = checked;
      });
      updateBulkBar();
    });

    document.getElementById("bulk-clear").addEventListener("click", () => {
      selectedIds.clear();
      document.querySelectorAll('#rows input[data-action="select-row"]').forEach(cb => { cb.checked = false; });
      document.getElementById("select-all-rows").checked = false;
      updateBulkBar();
    });
    document.getElementById("bulk-delete-btn").addEventListener("click", bulkDeleteFlow);

    // Hook into existing row event delegation for select-row + delete-record
    document.getElementById("rows").addEventListener("change", e => {
      if (e.target.dataset.action === "select-row") {
        const tr = e.target.closest("tr");
        if (!tr) return;
        const id = tr.dataset.id;
        if (e.target.checked) selectedIds.add(id);
        else selectedIds.delete(id);
        updateBulkBar();
      }
    });
    document.getElementById("rows").addEventListener("click", e => {
      if (e.target.dataset.action !== "delete-record") return;
      const tr = e.target.closest("tr");
      if (!tr) return;
      const rec = RECORDS.find(r => r.id === tr.dataset.id);
      if (rec) deleteRecordFlow(rec);
    });

    // ── Disk poll + breakdown modal ───────────────────────────────────────────

    function refreshDisk() {
      if (!SERVER_MODE) return;
      fetch("/api/disk")
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then(data => {
          diskState.cache_total_bytes = data.cache_total_bytes || 0;
          diskState.video_count = data.video_count || 0;
          diskState.orphan_count = data.orphan_count || 0;
          diskState.orphan_total_bytes = data.orphan_total_bytes || 0;
          diskState.by_record = data.by_record || [];
          renderStats();
        })
        .catch(() => { /* silent — connection indicator handles visibility */ });
    }

    if (SERVER_MODE) {
      setTimeout(refreshDisk, 600);
      setInterval(refreshDisk, 30000);
    }

    // Stats bar click → disk modal
    document.getElementById("stats").addEventListener("click", e => {
      const stat = e.target.closest("#cache-stat");
      if (!stat || !SERVER_MODE) return;
      openDiskModal();
    });

    function openDiskModal() {
      const summary = document.getElementById("disk-summary");
      const totalText = fmtBytes(diskState.cache_total_bytes);
      const recText = `${diskState.video_count} record${diskState.video_count !== 1 ? "s" : ""}`;
      const orphText = diskState.orphan_count > 0
        ? `, plus ${diskState.orphan_count} orphan${diskState.orphan_count !== 1 ? "s" : ""} using ${fmtBytes(diskState.orphan_total_bytes)}`
        : "";
      summary.textContent = `${totalText} total · ${recText}${orphText}. Click a row to scroll to it in the table.`;
      const tbody = document.getElementById("disk-rows");
      const items = (diskState.by_record || []);
      if (items.length === 0) {
        tbody.innerHTML = `<tr><td colspan="3" style="color:var(--muted);text-align:center;padding:24px;">No records.</td></tr>`;
      } else {
        tbody.innerHTML = items.map(r =>
          `<tr data-id="${escapeHtml(r.id || '')}">
             <td>${escapeHtml(r.title || "(no title)")}</td>
             <td class="size">${fmtBytes(r.bytes || 0)}</td>
             <td class="id-col">${escapeHtml(r.id || '')}</td>
           </tr>`
        ).join("");
      }
      document.getElementById("disk-modal-bg").classList.add("show");
    }
    document.getElementById("disk-close").addEventListener("click", () => {
      document.getElementById("disk-modal-bg").classList.remove("show");
    });
    document.getElementById("disk-modal-bg").addEventListener("click", e => {
      if (e.target.id === "disk-modal-bg") document.getElementById("disk-modal-bg").classList.remove("show");
    });
    document.getElementById("disk-rows").addEventListener("click", e => {
      const tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      const id = tr.dataset.id;
      document.getElementById("disk-modal-bg").classList.remove("show");
      const target = document.querySelector(`#rows tr[data-id="${id}"]`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("kbd-active");
        setTimeout(() => target.classList.remove("kbd-active"), 1200);
      }
    });

    // ── Orphan cleanup modal ──────────────────────────────────────────────────

    const orphansState = { items: [], selected: new Set() };

    function openOrphansModal() {
      if (!SERVER_MODE) {
        showToast("Server required to enumerate orphans");
        return;
      }
      // Close projects modal first if it's open
      document.getElementById("projects-modal-bg").classList.remove("show");
      orphansState.items = [];
      orphansState.selected.clear();
      renderOrphans();
      document.getElementById("orphans-modal-bg").classList.add("show");
      fetch("/api/orphans")
        .then(r => r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)))
        .then(data => {
          orphansState.items = data.orphans || [];
          renderOrphans();
        })
        .catch(err => {
          document.getElementById("orphans-list").innerHTML =
            `<li class="orphans-empty">Couldn't load orphans: ${escapeHtml(err.message)}</li>`;
        });
    }

    function renderOrphans() {
      const list = document.getElementById("orphans-list");
      const summary = document.getElementById("orphans-summary");
      const items = orphansState.items;
      if (items.length === 0) {
        list.innerHTML = `<li class="orphans-empty">No orphans found.</li>`;
        summary.textContent = "";
        return;
      }
      const totalBytes = items.reduce((a, x) => a + (x.size_bytes || 0), 0);
      summary.textContent = `${items.length} orphan${items.length !== 1 ? "s" : ""} · ${fmtBytes(totalBytes)} total`;
      list.innerHTML = items.map(o => {
        const checked = orphansState.selected.has(o.name) ? "checked" : "";
        const date = o.modified_at ? o.modified_at.replace("T", " ").replace(/\+.*$/, "") : "—";
        return `<li data-name="${escapeHtml(o.name)}">
          <input type="checkbox" data-action="orph-select" ${checked}>
          <div class="orphan-info">
            <div class="name">${escapeHtml(o.name)}</div>
            <div class="meta">modified ${escapeHtml(date)}</div>
          </div>
          <div class="orphan-size">${fmtBytes(o.size_bytes || 0)}</div>
        </li>`;
      }).join("");
    }

    document.getElementById("orphans-list").addEventListener("change", e => {
      if (e.target.dataset.action !== "orph-select") return;
      const li = e.target.closest("li");
      if (!li) return;
      const name = li.dataset.name;
      if (e.target.checked) orphansState.selected.add(name);
      else orphansState.selected.delete(name);
    });

    document.getElementById("orphans-select-all").addEventListener("click", () => {
      const allSelected = orphansState.selected.size === orphansState.items.length && orphansState.items.length > 0;
      orphansState.selected.clear();
      if (!allSelected) orphansState.items.forEach(o => orphansState.selected.add(o.name));
      renderOrphans();
    });

    document.getElementById("orphans-delete").addEventListener("click", () => {
      if (orphansState.selected.size === 0) {
        showToast("Nothing selected");
        return;
      }
      const names = Array.from(orphansState.selected);
      const totalBytes = orphansState.items
        .filter(o => orphansState.selected.has(o.name))
        .reduce((a, x) => a + (x.size_bytes || 0), 0);
      openConfirm({
        title: `Delete ${names.length} orphan${names.length !== 1 ? "s" : ""}?`,
        message: "Removes these directories from disk.",
        detail: [
          { label: "Count", value: `${names.length}` },
          { label: "Estimated free", value: fmtBytes(totalBytes) },
        ],
        confirmLabel: `Delete ${names.length}`,
        onConfirm: () => doOrphanDelete(names),
      });
    });

    function doOrphanDelete(names) {
      let succeeded = 0;
      let totalFreed = 0;
      let pending = names.length;
      names.forEach(name => {
        fetch(`/api/orphans/${encodeURIComponent(name)}`, { method: "DELETE" })
          .then(r => r.ok ? r.json() : r.text().then(t => { throw new Error(t || `HTTP ${r.status}`); }))
          .then(data => {
            succeeded++;
            totalFreed += data.freed_bytes || 0;
            orphansState.items = orphansState.items.filter(o => o.name !== name);
            orphansState.selected.delete(name);
          })
          .catch(err => {
            console.warn(`orphan delete ${name} failed:`, err);
          })
          .finally(() => {
            pending--;
            if (pending === 0) {
              showToast(`Deleted ${succeeded}/${names.length} orphans · freed ${fmtBytes(totalFreed)}`);
              renderOrphans();
              refreshDisk();
            }
          });
      });
    }

    document.getElementById("orphans-close").addEventListener("click", () => {
      document.getElementById("orphans-modal-bg").classList.remove("show");
    });
    document.getElementById("orphans-modal-bg").addEventListener("click", e => {
      if (e.target.id === "orphans-modal-bg") document.getElementById("orphans-modal-bg").classList.remove("show");
    });

    // Wire "Cleanup orphans" button in the projects modal
    const orphansBtn = document.getElementById("orphans-open-btn");
    if (orphansBtn) {
      if (!SERVER_MODE) {
        orphansBtn.disabled = true;
        orphansBtn.title = "Server required to enumerate orphans";
      } else {
        orphansBtn.addEventListener("click", openOrphansModal);
      }
    }

    fullRender();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: dashboard.py <project-dir>  # regenerates dashboard.html from index.json", file=sys.stderr)
        raise SystemExit(2)
    project = Path(sys.argv[1]).expanduser().resolve()
    cache = project / ".watch-cache"
    render_dashboard(cache / "index.json", cache / "dashboard.html")
    print(f"wrote {cache / 'dashboard.html'}")
