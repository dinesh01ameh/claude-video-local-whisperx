#!/usr/bin/env python3
"""Manifest + dashboard generation for /watch.

Each /watch run appends a record to <project_dir>/.watch-cache/index.json
and triggers a regen of dashboard.html. The HTML is self-contained
(no external assets, embeds the manifest as inline JS) so it works on
file:// without CORS shenanigans.

Usage:
  from dashboard import update_manifest, render_dashboard

  update_manifest(manifest_path, record)
  render_dashboard(manifest_path, dashboard_path)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


def render_dashboard(manifest_path: Path, dashboard_path: Path) -> None:
    """Regenerate dashboard.html from the manifest."""
    if manifest_path.exists():
        try:
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records = []
    else:
        records = []

    if not isinstance(records, list):
        records = []

    # Sort newest first
    records = sorted(
        records,
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )

    data_json = json.dumps(records, ensure_ascii=False)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    html = HTML_TEMPLATE.replace("{{DATA}}", data_json).replace(
        "{{GENERATED_AT}}", generated_at
    ).replace("{{COUNT}}", str(len(records)))
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
      --bg: #0f1115;
      --panel: #161922;
      --border: #232734;
      --text: #e6e7ea;
      --muted: #8b8f9b;
      --accent: #7aa6ff;
      --accent-soft: #2a3552;
      --green: #5fb878;
      --red: #d56b6b;
      --amber: #d59f4f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    }
    .topbar {
      padding: 18px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: baseline;
      gap: 18px;
    }
    .topbar h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 600;
    }
    .topbar .meta { color: var(--muted); font-size: 12px; }
    .controls {
      padding: 14px 24px;
      display: flex;
      gap: 12px;
      align-items: center;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }
    .controls input[type=search] {
      flex: 1;
      max-width: 480px;
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 8px 12px;
      border-radius: 6px;
      font: inherit;
    }
    .controls select {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      padding: 8px 10px;
      border-radius: 6px;
      font: inherit;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    thead th {
      position: sticky;
      top: 0;
      background: var(--panel);
      text-align: left;
      padding: 10px 14px;
      font-size: 12px;
      font-weight: 500;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
    }
    thead th .arrow { color: var(--accent); margin-left: 4px; }
    tbody tr {
      border-bottom: 1px solid var(--border);
      transition: background 80ms;
    }
    tbody tr:hover { background: rgba(122, 166, 255, 0.04); }
    td {
      padding: 12px 14px;
      vertical-align: middle;
    }
    td.thumb { width: 80px; }
    td.thumb img {
      width: 64px;
      height: 64px;
      object-fit: cover;
      border-radius: 4px;
      background: var(--panel);
      border: 1px solid var(--border);
      display: block;
    }
    td.thumb .placeholder {
      width: 64px; height: 64px;
      border-radius: 4px;
      background: var(--panel);
      border: 1px dashed var(--border);
      display: flex; align-items: center; justify-content: center;
      color: var(--muted); font-size: 11px;
    }
    .title { font-weight: 500; color: var(--text); }
    .source { font-size: 12px; color: var(--muted); margin-top: 2px; word-break: break-all; }
    .source a { color: var(--accent); text-decoration: none; }
    .source a:hover { text-decoration: underline; }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 500;
      border: 1px solid var(--border);
      color: var(--muted);
      white-space: nowrap;
    }
    .badge.captions { color: var(--green); border-color: rgba(95, 184, 120, 0.3); }
    .badge.whisperx { color: var(--accent); border-color: rgba(122, 166, 255, 0.3); }
    .badge.none { color: var(--muted); border-color: var(--border); }
    .badge.complete { color: var(--green); border-color: rgba(95, 184, 120, 0.3); }
    .badge.failed { color: var(--red); border-color: rgba(213, 107, 107, 0.3); }
    .badge.partial { color: var(--amber); border-color: rgba(213, 159, 79, 0.3); }
    .actions a {
      display: inline-block;
      color: var(--accent);
      text-decoration: none;
      font-size: 12px;
      margin-right: 12px;
    }
    .actions a:hover { text-decoration: underline; }
    .empty {
      text-align: center;
      padding: 64px 24px;
      color: var(--muted);
    }
    .empty code {
      background: var(--panel);
      border: 1px solid var(--border);
      padding: 2px 6px;
      border-radius: 4px;
      color: var(--text);
    }
    .duration, .frames, .age { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12px; }
    .age { white-space: nowrap; }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>/watch dashboard</h1>
    <span class="meta"><span id="count">{{COUNT}}</span> runs · regenerated {{GENERATED_AT}}</span>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder="Filter by title, uploader, source URL, transcript content…" autofocus>
    <select id="filter-transcript">
      <option value="">All transcript sources</option>
      <option value="captions">Captions only</option>
      <option value="local-whisperx">WhisperX only</option>
      <option value="none">No transcript</option>
    </select>
    <select id="filter-status">
      <option value="">All statuses</option>
      <option value="complete">Complete</option>
      <option value="partial">Partial</option>
      <option value="failed">Failed</option>
    </select>
  </div>
  <table id="runs">
    <thead>
      <tr>
        <th data-sort="started_at" class="active">Watched <span class="arrow">▼</span></th>
        <th data-sort="title">Title</th>
        <th></th>
        <th data-sort="duration_seconds">Duration</th>
        <th data-sort="frames_count">Frames</th>
        <th data-sort="transcript_source">Transcript</th>
        <th data-sort="status">Status</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" class="empty" style="display:none;">
    No runs yet. Run <code>/watch &lt;url-or-path&gt;</code> in Claude Code or Cowork to populate this dashboard.
  </div>

  <script>
    const RECORDS = {{DATA}};

    function fmtDuration(seconds) {
      if (!seconds && seconds !== 0) return "—";
      const s = Math.round(seconds);
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
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function fileUri(p) {
      if (!p) return "";
      // Convert Windows paths to file:/// URI
      const norm = p.replace(/\\/g, "/").replace(/^([A-Za-z]):/, "/$1:");
      return "file:///" + norm.replace(/^\/+/, "");
    }

    let sortKey = "started_at";
    let sortDir = -1; // -1 = desc, 1 = asc
    let q = "";
    let filterTranscript = "";
    let filterStatus = "";

    function render() {
      const tbody = document.getElementById("rows");
      const empty = document.getElementById("empty");
      const ql = q.toLowerCase().trim();

      let rows = RECORDS.slice();
      if (filterTranscript) rows = rows.filter(r => (r.transcript_source || "none") === filterTranscript);
      if (filterStatus) rows = rows.filter(r => (r.status || "") === filterStatus);
      if (ql) {
        rows = rows.filter(r => {
          const hay = [
            r.title, r.uploader, r.source, r.work_dir,
            r.transcript_source, r.status,
            (r.transcript_preview || "")
          ].filter(Boolean).join(" ").toLowerCase();
          return hay.includes(ql);
        });
      }

      rows.sort((a, b) => {
        const av = a[sortKey] ?? "";
        const bv = b[sortKey] ?? "";
        if (av < bv) return -1 * sortDir;
        if (av > bv) return 1 * sortDir;
        return 0;
      });

      document.getElementById("count").textContent =
        rows.length === RECORDS.length ? RECORDS.length : `${rows.length} of ${RECORDS.length}`;

      if (rows.length === 0) {
        tbody.innerHTML = "";
        empty.style.display = "block";
        return;
      }
      empty.style.display = "none";

      tbody.innerHTML = rows.map(r => {
        const tFrame = r.first_frame_path ? fileUri(r.first_frame_path) : "";
        const workUri = r.work_dir ? fileUri(r.work_dir) : "";
        const sourceLink = r.source && r.source.startsWith("http")
          ? `<a href="${escapeHtml(r.source)}" target="_blank" rel="noopener">${escapeHtml(r.source)}</a>`
          : escapeHtml(r.source || "");
        const tBadge = (r.transcript_source || "none");
        const tCount = r.transcript_segment_count != null ? ` · ${r.transcript_segment_count} seg` : "";
        const status = r.status || "complete";

        return `<tr>
          <td><span class="age" title="${escapeHtml(r.started_at || "")}">${fmtAge(r.started_at)}</span></td>
          <td>
            <div class="title">${escapeHtml(r.title || "(no title)")}</div>
            <div class="source">${r.uploader ? escapeHtml(r.uploader) + " · " : ""}${sourceLink}</div>
          </td>
          <td class="thumb">${tFrame ? `<img src="${tFrame}" alt="">` : `<div class="placeholder">no frame</div>`}</td>
          <td class="duration">${fmtDuration(r.duration_seconds)}</td>
          <td class="frames">${r.frames_count != null ? r.frames_count : "—"}</td>
          <td><span class="badge ${tBadge}">${tBadge}${tCount}</span></td>
          <td><span class="badge ${status}">${status}</span></td>
          <td class="actions">
            ${workUri ? `<a href="${workUri}" title="Open work directory">work dir</a>` : ""}
            ${r.transcript_path ? `<a href="${fileUri(r.transcript_path)}">transcript</a>` : ""}
            ${r.report_path ? `<a href="${fileUri(r.report_path)}">report</a>` : ""}
          </td>
        </tr>`;
      }).join("");
    }

    document.getElementById("q").addEventListener("input", e => { q = e.target.value; render(); });
    document.getElementById("filter-transcript").addEventListener("change", e => { filterTranscript = e.target.value; render(); });
    document.getElementById("filter-status").addEventListener("change", e => { filterStatus = e.target.value; render(); });
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
        render();
      });
    });

    render();
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
