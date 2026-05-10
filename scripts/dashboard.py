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

    records = sorted(records, key=lambda r: r.get("started_at") or "", reverse=True)
    summaries = _load_summaries(records)

    data_json = json.dumps(records, ensure_ascii=False)
    summaries_json = json.dumps(summaries, ensure_ascii=False)
    tags_json = json.dumps(PROJECT_TAGS, ensure_ascii=False)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    html = (
        HTML_TEMPLATE
        .replace("{{DATA}}", data_json)
        .replace("{{SUMMARIES}}", summaries_json)
        .replace("{{PROJECT_TAGS}}", tags_json)
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
    .badge.pasted { color: var(--green); border-color: rgba(95, 184, 120, 0.3); background: rgba(95, 184, 120, 0.08); }
    .badge.pending { color: var(--amber); border-color: rgba(213, 159, 79, 0.3); }

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
  </style>
</head>
<body>
  <div class="topbar">
    <h1>/watch dashboard</h1>
    <span class="meta">regenerated {{GENERATED_AT}}</span>
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

  <table id="runs">
    <thead>
      <tr>
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

  <div class="toast" id="toast"></div>

  <script>
    const RECORDS = {{DATA}};
    const SUMMARIES = {{SUMMARIES}};
    const PROJECT_TAGS = {{PROJECT_TAGS}};
    const STATE_KEY = "watch_dashboard_state";
    const UI_KEY = "watch_dashboard_ui";

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

    let state = loadState();
    const ui = loadUI();

    function getAnnot(id) {
      return state[id] || { nlm_pasted: false, project_tag: "None", note: "" };
    }
    function setAnnot(id, patch) {
      state[id] = { ...getAnnot(id), ...patch, marked_at: new Date().toISOString() };
      saveState(state);
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
      const norm = p.replace(/\\/g, "/").replace(/^([A-Za-z]):/, "/$1:");
      return "file:///" + norm.replace(/^\/+/, "");
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
      document.getElementById("stats").innerHTML = `
        <div class="stat"><span class="num">${total}</span><span class="lbl">Total</span></div>
        <div class="stat"><span class="num green">${pasted}</span><span class="lbl">Pasted to NLM</span></div>
        <div class="stat"><span class="num amber">${pending}</span><span class="lbl">Pending NLM</span></div>
        <div class="stat"><span class="num muted">${failed}</span><span class="lbl">Failed</span></div>
        <div class="stat" style="margin-left:auto;"><span class="num" style="font-size:13px;font-weight:400;color:var(--muted);">${topTags}</span><span class="lbl">Top projects</span></div>
      `;
    }

    function render() {
      const tbody = document.getElementById("rows");
      const empty = document.getElementById("empty");
      const ql = q.toLowerCase().trim();

      let rows = RECORDS.slice();
      if (filterTranscript) rows = rows.filter(r => (r.transcript_source || "none") === filterTranscript);
      if (filterStatus) rows = rows.filter(r => (r.status || "complete") === filterStatus);
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
        const status = r.status || "complete";
        const hasNlm = !!SUMMARIES[r.id];

        const projectOptions = PROJECT_TAGS.map(t =>
          `<option value="${escapeHtml(t)}" ${t === a.project_tag ? "selected" : ""}>${t === "None" ? "(none)" : escapeHtml(t)}</option>`
        ).join("");

        return `<tr data-id="${escapeHtml(r.id)}">
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
            <select data-action="tag" class="${a.project_tag && a.project_tag !== 'None' ? 'tagged' : ''}">${projectOptions}</select>
          </td>
          <td class="nlm-cell">
            <input type="checkbox" data-action="nlm" ${a.nlm_pasted ? "checked" : ""} title="Mark pasted to NLM">
          </td>
          <td class="note-cell">
            <textarea data-action="note" placeholder="note…">${escapeHtml(a.note || "")}</textarea>
          </td>
          <td class="actions">
            ${workUri ? `<a href="${workUri}" title="Open work directory">work</a>` : ""}
            <button data-action="preview-nlm" ${hasNlm ? "" : "disabled"} title="${hasNlm ? "Preview NLM summary" : "No NLM summary saved for this video"}">${hasNlm ? "NLM" : "no NLM"}</button>
            <button data-action="copy-rerun" title="Copy re-run command to clipboard">re-run</button>
          </td>
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
      if (action === "preview-nlm") {
        const summary = SUMMARIES[id];
        const rec = RECORDS.find(r => r.id === id);
        document.getElementById("nlm-modal-title").textContent = (rec && rec.title) || "NLM Summary";
        const body = document.getElementById("nlm-modal-body");
        if (summary) body.textContent = summary;
        else body.innerHTML = '<span class="empty-msg">No NLM summary file found at &lt;work_dir&gt;/nlm-summary.md.</span>';
        body.dataset.id = id;
        document.getElementById("nlm-modal-bg").classList.add("show");
      } else if (action === "copy-rerun") {
        const rec = RECORDS.find(r => r.id === id);
        if (!rec) return;
        const cmd = `python "$env:USERPROFILE\\..\\Ai-work\\Triage\\Triage Knowledge System\\claude-video-local-whisperx\\scripts\\watch.py" "${rec.source}"`;
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
    document.getElementById("img-modal-bg").addEventListener("click", () => {
      document.getElementById("img-modal-bg").classList.remove("show");
    });

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
      if (e.key === "/") {
        e.preventDefault();
        document.getElementById("q").focus();
        document.getElementById("q").select();
      } else if (e.key === "Escape") {
        document.getElementById("nlm-modal-bg").classList.remove("show");
        document.getElementById("img-modal-bg").classList.remove("show");
      }
    });

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
