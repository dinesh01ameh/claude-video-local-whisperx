#!/usr/bin/env python3
"""Optional FastAPI server for one-click /watch dashboard operation.

Without this server, the dashboard is static HTML — copy commands, paste in a
terminal. Run this server and the dashboard hits /api/preview and /api/focused
to spawn watch.py subprocesses for you. Jobs track status + log tail in-memory.

Localhost-only by design — no auth, no external bind.

Usage:
    pip install --user fastapi uvicorn
    $env:WATCH_PROJECT_DIR = "D:\\Ai-work\\Triage\\Triage Knowledge System"
    python scripts/dashboard_server.py

Then open http://localhost:4893 in a browser.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
    import uvicorn
except ImportError:
    print(
        "Missing dependencies. Install with:\n"
        "  pip install --user fastapi uvicorn",
        file=sys.stderr,
    )
    raise SystemExit(1)


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

# Reuse the loaders + path normalization from dashboard.py so /api/manifest
# returns the same shape the static template embeds.
from dashboard import (  # noqa: E402
    PROJECT_TAGS,
    _load_focused_results,
    _load_previews,
    _load_summaries,
    _normalize_paths,
    _rewrite_path,
    render_dashboard,
)


# ─── Bootstrap ───────────────────────────────────────────────────────────────

def _resolve_project_dir() -> Path:
    raw = os.environ.get("WATCH_PROJECT_DIR", "").strip()
    if not raw:
        print(
            "WATCH_PROJECT_DIR is not set.\n"
            "Set it to the project directory whose .watch-cache/ holds the manifest.\n"
            'Example (PowerShell): $env:WATCH_PROJECT_DIR = "D:\\Ai-work\\Triage\\Triage Knowledge System"',
            file=sys.stderr,
        )
        raise SystemExit(2)
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        print(f"WATCH_PROJECT_DIR does not exist: {p}", file=sys.stderr)
        raise SystemExit(2)
    return p


PROJECT_DIR = _resolve_project_dir()
CACHE_DIR = PROJECT_DIR / ".watch-cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_PATH = CACHE_DIR / "index.json"
DASHBOARD_PATH = CACHE_DIR / "dashboard.html"
WATCH_SCRIPT = SCRIPT_DIR / "watch.py"

PORT = int(os.environ.get("WATCH_SERVER_PORT", "4893"))
HOST = "127.0.0.1"

# Phase 6: detect the claude CLI so /api/focused can chain auto-synthesis.
# Resolved once at startup; if claude isn't on PATH the server still serves
# extraction-only and the dashboard hides the auto-synthesize toggle.
CLAUDE_BIN = shutil.which("claude")
SYNTHESIS_TIMEOUT_SEC = int(os.environ.get("CLAUDE_SYNTHESIS_TIMEOUT_SEC", "600"))


# ─── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("dashboard_server")
logger.setLevel(logging.INFO)
_log_path = CACHE_DIR / "server.log"
_file_handler = RotatingFileHandler(
    _log_path, maxBytes=10_000_000, backupCount=2, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_file_handler)
_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
logger.addHandler(_stream_handler)


# ─── Job tracking ────────────────────────────────────────────────────────────

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
LOG_TAIL_CAP = 500


def _new_job(kind: str, **extra) -> str:
    """Create a job record. State machine:
        queued → extracting → [synthesizing] → done   (or → failed at any step)
    Phase 6: focused jobs may chain a synthesize phase; preview jobs only
    use the extract phase.
    """
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "queued",
            "phase": None,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "extract_completed_at": None,
            "synthesis_started_at": None,
            "synthesis_completed_at": None,
            "extract_log_tail": [],
            "synthesis_log_tail": [],
            "error": None,
            "result": None,
            "process": None,
            **extra,
        }
    return job_id


def _update_job(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _append_log(job_id: str, line: str, channel: str = "extract") -> None:
    """channel ∈ {"extract", "synthesis"} — selects which log_tail to append to."""
    key = f"{channel}_log_tail"
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        tail = job[key]
        tail.append(line)
        if len(tail) > LOG_TAIL_CAP:
            del tail[: len(tail) - LOG_TAIL_CAP]


def _stream_subprocess(
    job_id: str,
    proc: subprocess.Popen,
    channel: str = "extract",
    on_done=None,
) -> None:
    """Drain stdout line-by-line until the subprocess exits.

    Phase 6: status transitions are owned by `on_done` (or the chained-job
    parent) — this helper only streams output and reports success/failure.
    If on_done is None and rc==0, the helper marks status="done" as a fallback.
    """
    full_lines: list[str] = []
    try:
        if proc.stdout:
            for raw in iter(proc.stdout.readline, ""):
                line = raw.rstrip("\r\n")
                full_lines.append(line)
                _append_log(job_id, line, channel=channel)
        proc.wait()
        rc = proc.returncode
        if rc == 0:
            logger.info("job %s subprocess (%s) done (rc=0, %d lines)", job_id, channel, len(full_lines))
            if on_done:
                try:
                    on_done(full_lines)
                except Exception as exc:
                    _update_job(job_id, status="failed", error=str(exc))
                    logger.exception("on_done for job %s failed", job_id)
            else:
                _update_job(job_id, status="done", returncode=0)
        else:
            _update_job(
                job_id,
                status="failed",
                returncode=rc,
                error=f"subprocess exited with code {rc}",
            )
            logger.warning("job %s subprocess (%s) failed (rc=%d)", job_id, channel, rc)
    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))
        logger.exception("job %s crashed", job_id)


def _spawn(
    job_id: str,
    args: list[str],
    on_done=None,
    channel: str = "extract",
    cwd: str | None = None,
) -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["WATCH_PROJECT_DIR"] = str(PROJECT_DIR)
    logger.info("job %s starting (%s): %s", job_id, channel, " ".join(args))
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
            cwd=cwd or str(SCRIPT_DIR.parent),
        )
    except OSError as exc:
        _update_job(job_id, status="failed", error=str(exc))
        logger.exception("failed to spawn job %s", job_id)
        return
    _update_job(job_id, process=proc, pid=proc.pid)
    threading.Thread(
        target=_stream_subprocess,
        args=(job_id, proc, channel, on_done),
        daemon=True,
    ).start()


# ─── Synthesis (Phase 6: chain `claude -p` after focused extraction) ─────────

def _spawn_synthesis(job_id: str, work_path: Path, focused_report_path: Path) -> None:
    """Spawn `claude -p <prompt>` and wire it into the job's synthesis phase."""
    if not CLAUDE_BIN:
        _update_job(job_id, status="failed", error="claude CLI not on PATH")
        return

    prompt = (
        "Apply the watch skill's focused-mode synthesis to the report at "
        f"{focused_report_path}. Read every frame path it lists, follow SKILL.md "
        "NLM template (Spoken content / Visual content / Synthesis sections), "
        f"and save BOTH focused-result.md (verbose) and nlm-summary.md "
        f"(NLM-paste version) to {work_path}. Confirm in one line when done."
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["WATCH_PROJECT_DIR"] = str(PROJECT_DIR)

    args = [CLAUDE_BIN, "-p", prompt]
    logger.info("job %s synthesizing via claude (timeout %ds)", job_id, SYNTHESIS_TIMEOUT_SEC)
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
            cwd=str(PROJECT_DIR),
        )
    except OSError as exc:
        _update_job(job_id, status="failed", error=f"failed to spawn claude: {exc}")
        return

    synthesis_started_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _update_job(
        job_id,
        status="synthesizing", phase="synthesize",
        process=proc, pid=proc.pid,
        synthesis_started_at=synthesis_started_iso,
    )
    # Surface a hint immediately — claude -p is silent during tool use, so
    # without this the user sees an empty Synthesis tab and assumes hang.
    _append_log(
        job_id,
        "[INFO] claude -p running silently during tool use; logs appear when it writes files or finishes (~30-90s expected)",
        channel="synthesis",
    )
    # Background file-watcher: complements the subprocess streamer by surfacing
    # [WROTE] events as soon as Claude saves each .md file via the Write tool.
    threading.Thread(
        target=_watch_synthesis_files,
        args=(job_id, work_path),
        daemon=True,
    ).start()

    def _verify_outputs(_lines: list[str]) -> None:
        focused_result = work_path / "focused-result.md"
        nlm_summary = work_path / "nlm-summary.md"
        missing = []
        if not focused_result.exists():
            missing.append("focused-result.md")
        if not nlm_summary.exists():
            missing.append("nlm-summary.md")
        if missing:
            _update_job(
                job_id,
                status="failed",
                error=(
                    f"Claude exited cleanly but {' / '.join(missing)} is missing — "
                    "skill may have misfired. Run the prompt manually to debug, "
                    "or ensure the watch plugin is installed at user scope: "
                    "/plugin install watch@claude-video-local-whisperx"
                ),
            )
            return
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return
            job["synthesis_completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            existing_result = job.get("result") or {}
            existing_result.update({
                "focused_result_path": str(focused_result),
                "nlm_summary_path": str(nlm_summary),
            })
            job["result"] = existing_result
        _update_job(job_id, status="done", phase=None)
        logger.info("job %s synthesis verified", job_id)

    threading.Thread(
        target=_run_synthesis_with_timeout,
        args=(job_id, proc, _verify_outputs, SYNTHESIS_TIMEOUT_SEC),
        daemon=True,
    ).start()


def _watch_synthesis_files(job_id: str, work_path: Path) -> None:
    """Poll work_path for the two synthesis output files and surface [WROTE]
    lines into the synthesis log as they appear.

    Stops when both files exist, when the synthesis phase ends (status changes
    away from 'synthesizing'), or after a 15-minute hard cap. Read-only —
    never touches the subprocess; complements _run_synthesis_with_timeout.
    """
    targets = ("focused-result.md", "nlm-summary.md")
    seen: set[str] = set()
    deadline = time.time() + 900  # 15 min hard cap, longer than synthesis timeout
    while time.time() < deadline:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job or job.get("status") != "synthesizing":
                break
            started_iso = job.get("synthesis_started_at")
        try:
            started_dt = datetime.fromisoformat(started_iso) if started_iso else None
        except ValueError:
            started_dt = None
        for name in targets:
            if name in seen:
                continue
            p = work_path / name
            if p.exists():
                try:
                    size_kb = p.stat().st_size / 1024.0
                except OSError:
                    size_kb = 0.0
                elapsed = (
                    (datetime.now(timezone.utc) - started_dt).total_seconds()
                    if started_dt else 0.0
                )
                _append_log(
                    job_id,
                    f"[WROTE] {name} ({size_kb:.1f} KB) at {elapsed:.0f}s",
                    channel="synthesis",
                )
                seen.add(name)
        if seen == set(targets):
            break
        time.sleep(1.0)


def _run_synthesis_with_timeout(
    job_id: str,
    proc: subprocess.Popen,
    on_done,
    timeout_sec: int,
) -> None:
    """Stream the synthesis subprocess output, enforcing a wall-clock timeout."""
    full_lines: list[str] = []

    def reader():
        try:
            if proc.stdout:
                for raw in iter(proc.stdout.readline, ""):
                    line = raw.rstrip("\r\n")
                    full_lines.append(line)
                    _append_log(job_id, line, channel="synthesis")
        except Exception:
            logger.exception("synthesis reader for job %s crashed", job_id)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    try:
        rc = proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _update_job(
            job_id,
            status="failed",
            error=(
                f"synthesis timed out after {timeout_sec}s "
                "— set CLAUDE_SYNTHESIS_TIMEOUT_SEC to extend, or run the prompt manually"
            ),
        )
        logger.warning("job %s synthesis timed out", job_id)
        return
    reader_thread.join(timeout=2)
    if rc == 0 and on_done:
        try:
            on_done(full_lines)
        except Exception as exc:
            _update_job(job_id, status="failed", error=str(exc))
            logger.exception("synthesis on_done for job %s failed", job_id)
    elif rc != 0:
        tail_text = "\n".join(full_lines[-10:]) if full_lines else "(no output)"
        _update_job(
            job_id,
            status="failed",
            error=f"claude exited with code {rc}\n{tail_text}",
        )
        logger.warning("job %s synthesis failed (rc=%d)", job_id, rc)


# ─── Path safety ─────────────────────────────────────────────────────────────

def _safe_join(base: Path, requested: str) -> Path:
    """Resolve `requested` under `base`. Raise on traversal or absolute paths."""
    if not requested:
        raise HTTPException(404, "empty path")
    norm = requested.replace("\\", "/")
    # Reject absolute / drive-rooted paths from clients
    if norm.startswith("/") or (len(norm) >= 2 and norm[1] == ":"):
        raise HTTPException(403, "absolute paths not allowed")
    if any(part == ".." for part in Path(norm).parts):
        raise HTTPException(403, "path traversal not allowed")
    candidate = (base / norm).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(403, "path escapes project root")
    return candidate


# ─── Manifest data ───────────────────────────────────────────────────────────

# ─── Phase 7: delete / orphan / disk helpers ─────────────────────────────────

# Files in .watch-cache/ that are NOT video work-dirs and must never be
# enumerated as orphans or rmtree'd.
PROTECTED_CACHE_NAMES = {
    "index.json", "dashboard.html",
    "server.log", "server.log.1", "server.log.2",
}

ORPHAN_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _dir_size(path: Path) -> int:
    """Recursive sum of file sizes under `path`. Missing/locked files counted as 0."""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _is_active_workdir(work_dir_str: str) -> bool:
    """True if any in-flight job is touching this work_dir.

    Focused jobs carry a `work_dir` field (set in _new_job). Preview jobs don't
    target an existing work_dir — they create a new one — so they can't conflict
    with a delete of an *existing* record.
    """
    target = Path(work_dir_str).resolve()
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("status") not in {"queued", "extracting", "synthesizing"}:
                continue
            wd = job.get("work_dir")
            if not wd:
                continue
            try:
                if Path(wd).resolve() == target:
                    return True
            except OSError:
                continue
    return False


def _validate_workdir_under_cache(work_dir: str) -> Path:
    """Resolve `work_dir` and verify it's a *direct subdir* of .watch-cache/.

    Defends against path-injection — even if a manifest entry were tampered
    with, we never rmtree anything outside the cache.
    """
    if not work_dir:
        raise HTTPException(400, "record has no work_dir")
    p = Path(work_dir).expanduser().resolve()
    cache_resolved = CACHE_DIR.resolve()
    try:
        rel = p.relative_to(cache_resolved)
    except ValueError:
        raise HTTPException(403, f"work_dir not under .watch-cache/: {work_dir}")
    if len(rel.parts) != 1:
        raise HTTPException(403, f"work_dir must be a direct subdir of .watch-cache/: {work_dir}")
    return p


def _read_manifest_records() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_manifest_records(records: list[dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _delete_record_by_id(rid: str, records: list[dict]) -> tuple[dict, int]:
    """Mutates `records` (drops the matching entry) and rmtree's the work_dir.

    Returns (deleted_record, freed_bytes). Raises HTTPException on validation
    or filesystem errors.
    """
    idx = next((i for i, r in enumerate(records) if r.get("id") == rid), None)
    if idx is None:
        raise HTTPException(404, f"record not found: {rid}")
    rec = records[idx]
    # Normalize legacy sandbox paths (/sessions/.../mnt/.../.watch-cache/...) to
    # the host project root so the same record can be deleted from any host.
    work_dir = _rewrite_path(rec.get("work_dir") or "", PROJECT_DIR)
    if _is_active_workdir(work_dir):
        raise HTTPException(409, f"cannot delete — job in progress on {rid}")
    work_path = _validate_workdir_under_cache(work_dir)

    freed = _dir_size(work_path) if work_path.exists() else 0
    if work_path.exists():
        try:
            shutil.rmtree(work_path, ignore_errors=False)
        except OSError as exc:
            raise HTTPException(500, f"rmtree failed: {exc}")

    del records[idx]
    return rec, freed


def _list_orphans(record_ids: set[str]) -> list[dict]:
    out: list[dict] = []
    if not CACHE_DIR.exists():
        return out
    for entry in CACHE_DIR.iterdir():
        if entry.name in PROTECTED_CACHE_NAMES:
            continue
        if not entry.is_dir():
            continue
        if entry.name in record_ids:
            continue
        try:
            stat = entry.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
        except OSError:
            mtime = None
        out.append({
            "name": entry.name,
            "path": str(entry),
            "size_bytes": _dir_size(entry),
            "modified_at": mtime,
        })
    out.sort(key=lambda o: o.get("modified_at") or "", reverse=True)
    return out


def _load_manifest_data() -> dict[str, Any]:
    """Return the same shape dashboard.py embeds at render time."""
    if MANIFEST_PATH.exists():
        try:
            records = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records = []
    else:
        records = []
    if not isinstance(records, list):
        records = []

    records = _normalize_paths(records, PROJECT_DIR)
    records = sorted(records, key=lambda r: r.get("started_at") or "", reverse=True)
    summaries = _load_summaries(records)
    focused_results = _load_focused_results(records)
    previews = _load_previews(records)
    # Mirror dashboard.py's preview-path normalization
    previews = {
        rid: {
            **p,
            "video_path": (
                _rewrite_path(str(p.get("video_path", "")), PROJECT_DIR)
                if p.get("video_path") else p.get("video_path")
            ),
            "sparse_frames": (
                [
                    {**f, "path": _rewrite_path(str(f.get("path", "")), PROJECT_DIR)}
                    if f.get("path") else f
                    for f in p.get("sparse_frames", [])
                ]
                if isinstance(p.get("sparse_frames"), list)
                else p.get("sparse_frames")
            ),
        }
        for rid, p in previews.items()
    }
    return {
        "records": records,
        "summaries": summaries,
        "focused_results": focused_results,
        "previews": previews,
        "project_tags": PROJECT_TAGS,
    }


# ─── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(title="watch dashboard server", docs_url=None, redoc_url=None)


@app.get("/")
def root():
    return RedirectResponse("/dashboard.html")


@app.get("/favicon.ico")
def favicon():
    """Silence Chrome's default favicon probe — we don't ship one."""
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/dashboard.html")
def dashboard_html():
    if not DASHBOARD_PATH.exists():
        render_dashboard(MANIFEST_PATH, DASHBOARD_PATH)
    return FileResponse(DASHBOARD_PATH, media_type="text/html; charset=utf-8")


@app.get("/files/{path:path}")
def files(path: str):
    target = _safe_join(PROJECT_DIR, path)
    if not target.exists():
        raise HTTPException(404, f"not found: {path}")
    if target.is_dir():
        raise HTTPException(403, "directory listing not allowed")
    suffix = target.suffix.lower()
    media_type = None
    if suffix in (".md", ".txt", ".log"):
        media_type = "text/plain; charset=utf-8"
    elif suffix == ".json":
        media_type = "application/json; charset=utf-8"
    return FileResponse(target, media_type=media_type)


@app.get("/api/manifest")
def api_manifest():
    return JSONResponse(_load_manifest_data())


@app.post("/api/preview")
async def api_preview(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "url must start with http(s)://")

    job_id = _new_job("preview")

    def _on_extract_done(_lines: list[str]) -> None:
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["extract_completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _update_job(job_id, status="done", phase=None)

    args = [sys.executable, str(WATCH_SCRIPT), "--preview", url]
    _update_job(job_id, status="extracting", phase="extract")
    _spawn(job_id, args, on_done=_on_extract_done, channel="extract")
    return {"job_id": job_id}


@app.post("/api/focused")
async def api_focused(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    work_dir = (body.get("work_dir") or "").strip()
    segments = body.get("segments") or []
    user_review = (body.get("user_review") or "").strip()

    # Phase 6: auto_synthesize defaults to True iff claude is on PATH.
    raw_auto = body.get("auto_synthesize", None)
    if raw_auto is None:
        auto_synthesize = bool(CLAUDE_BIN)
    else:
        auto_synthesize = bool(raw_auto)
    if auto_synthesize and not CLAUDE_BIN:
        raise HTTPException(400, "claude CLI not on PATH; cannot auto-synthesize")

    if not work_dir:
        raise HTTPException(400, "work_dir required")
    if not isinstance(segments, list):
        raise HTTPException(400, "segments must be an array")

    work_path = Path(work_dir).resolve()
    try:
        work_path.relative_to(PROJECT_DIR.resolve())
    except ValueError:
        raise HTTPException(403, "work_dir must be inside project")
    if not work_path.exists():
        raise HTTPException(404, f"work_dir not found: {work_dir}")

    focused_report_path = work_path / "focused-report.md"

    job_id = _new_job(
        "focused",
        auto_synthesize=auto_synthesize,
        work_dir=str(work_path),
    )

    def _on_extract_done(full_lines: list[str]) -> None:
        # Capture extraction stdout to focused-report.md so synthesis (or the
        # user, in extract-only mode) can read it.
        try:
            focused_report_path.write_text("\n".join(full_lines), encoding="utf-8")
            logger.info("wrote %s (%d lines)", focused_report_path, len(full_lines))
        except OSError as exc:
            _update_job(job_id, status="failed", error=f"failed to write focused-report.md: {exc}")
            return
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["extract_completed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                JOBS[job_id]["result"] = {"focused_report_path": str(focused_report_path)}

        if not auto_synthesize:
            # Extract-only mode: we're done. The dashboard will surface the
            # "Copy prompt for Claude Code" + "View focused report" buttons.
            _update_job(job_id, status="done", phase=None)
            return

        # Chain synthesis
        _spawn_synthesis(job_id, work_path, focused_report_path)

    seg_json = json.dumps(segments, ensure_ascii=False)
    args = [
        sys.executable, str(WATCH_SCRIPT),
        "--focused",
        "--work-dir", str(work_path),
        "--segments-json", seg_json,
        "--user-review", user_review,
    ]
    _update_job(job_id, status="extracting", phase="extract")
    _spawn(job_id, args, on_done=_on_extract_done, channel="extract")
    return {
        "job_id": job_id,
        "focused_report_path": str(focused_report_path),
        "auto_synthesize": auto_synthesize,
    }


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        # Compute synthesis elapsed live so the dashboard can render a ticking
        # "Xs elapsed" header even when stdout is silent.
        started_iso = job.get("synthesis_started_at")
        synthesis_elapsed = None
        if started_iso:
            try:
                started_dt = datetime.fromisoformat(started_iso)
                synthesis_elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds()
            except ValueError:
                pass
        return {
            "id": job["id"],
            "kind": job.get("kind"),
            "status": job["status"],
            "phase": job.get("phase"),
            "started_at": job["started_at"],
            "extract_completed_at": job.get("extract_completed_at"),
            "synthesis_started_at": job.get("synthesis_started_at"),
            "synthesis_completed_at": job.get("synthesis_completed_at"),
            "synthesis_elapsed_sec": synthesis_elapsed,
            "extract_log_tail": list(job["extract_log_tail"][-50:]),
            "synthesis_log_tail": list(job["synthesis_log_tail"][-50:]),
            "error": job.get("error"),
            "returncode": job.get("returncode"),
            "result": job.get("result"),
            "auto_synthesize": job.get("auto_synthesize"),
        }


@app.delete("/api/records/{rid}")
def api_record_delete(rid: str):
    records = _read_manifest_records()
    rec, freed = _delete_record_by_id(rid, records)
    _write_manifest_records(records)
    try:
        render_dashboard(MANIFEST_PATH, DASHBOARD_PATH)
    except Exception:
        logger.exception("dashboard regen after delete failed (non-fatal)")
    logger.info("deleted record %s, freed %d bytes", rid, freed)
    return {"deleted": True, "id": rid, "freed_bytes": freed}


@app.post("/api/records/bulk-delete")
async def api_records_bulk_delete(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, "ids must be an array of strings")

    records = _read_manifest_records()
    results = []
    total_freed = 0
    for rid in ids:
        try:
            _rec, freed = _delete_record_by_id(rid, records)
            total_freed += freed
            results.append({"id": rid, "deleted": True, "error": None, "freed_bytes": freed})
        except HTTPException as exc:
            results.append({"id": rid, "deleted": False, "error": str(exc.detail), "freed_bytes": 0})
        except Exception as exc:
            logger.exception("bulk delete: unexpected failure for %s", rid)
            results.append({"id": rid, "deleted": False, "error": str(exc), "freed_bytes": 0})

    # Atomic write — single manifest update at the end of the batch.
    _write_manifest_records(records)
    try:
        render_dashboard(MANIFEST_PATH, DASHBOARD_PATH)
    except Exception:
        logger.exception("dashboard regen after bulk-delete failed (non-fatal)")
    logger.info("bulk-delete: %d/%d ok, freed %d bytes", sum(1 for r in results if r["deleted"]), len(results), total_freed)
    return {"results": results, "total_freed_bytes": total_freed}


@app.get("/api/orphans")
def api_orphans():
    records = _read_manifest_records()
    record_ids = {str(r.get("id")) for r in records if r.get("id")}
    return {"orphans": _list_orphans(record_ids)}


@app.delete("/api/orphans/{name}")
def api_orphan_delete(name: str):
    if not ORPHAN_NAME_RE.match(name):
        raise HTTPException(400, "invalid orphan name")
    if name in PROTECTED_CACHE_NAMES:
        raise HTTPException(403, "name is protected")
    target = (CACHE_DIR / name).resolve()
    cache_resolved = CACHE_DIR.resolve()
    try:
        rel = target.relative_to(cache_resolved)
    except ValueError:
        raise HTTPException(403, "path escapes cache root")
    if len(rel.parts) != 1:
        raise HTTPException(403, "must be a direct subdir of .watch-cache/")
    # Must not be in the active manifest (would be a real record, not an orphan)
    records = _read_manifest_records()
    if any(r.get("id") == name for r in records):
        raise HTTPException(409, "name corresponds to a manifest record — use DELETE /api/records/{id} instead")
    if not target.exists():
        raise HTTPException(404, f"not found: {name}")
    if not target.is_dir():
        raise HTTPException(403, "not a directory")
    freed = _dir_size(target)
    try:
        shutil.rmtree(target, ignore_errors=False)
    except OSError as exc:
        raise HTTPException(500, f"rmtree failed: {exc}")
    logger.info("deleted orphan %s, freed %d bytes", name, freed)
    return {"deleted": True, "name": name, "freed_bytes": freed}


@app.get("/api/disk")
def api_disk():
    records = _read_manifest_records()
    record_ids = {str(r.get("id")) for r in records if r.get("id")}
    by_record = []
    cache_total = 0
    for r in records:
        wd_raw = r.get("work_dir")
        if not wd_raw:
            continue
        # Same legacy-path normalization as the delete endpoint — sandbox-path
        # records would otherwise silently disappear from the disk total.
        wd = _rewrite_path(wd_raw, PROJECT_DIR)
        try:
            wp = Path(wd).expanduser().resolve()
            wp.relative_to(CACHE_DIR.resolve())
        except (ValueError, OSError):
            continue
        size = _dir_size(wp) if wp.exists() else 0
        cache_total += size
        by_record.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "bytes": size,
            "work_dir": wd,
        })
    by_record.sort(key=lambda x: x.get("bytes", 0), reverse=True)
    orphans = _list_orphans(record_ids)
    orphan_total = sum(o.get("size_bytes", 0) for o in orphans)
    return {
        "cache_total_bytes": cache_total + orphan_total,
        "video_count": len(by_record),
        "orphan_count": len(orphans),
        "orphan_total_bytes": orphan_total,
        "by_record": by_record,
    }


@app.get("/api/health")
def api_health():
    """Surface server capability flags so the dashboard can adapt at load time."""
    return {
        "claude_available": bool(CLAUDE_BIN),
        "claude_path": CLAUDE_BIN,
        "watch_script": str(WATCH_SCRIPT),
        "project_dir": str(PROJECT_DIR),
        "synthesis_timeout_sec": SYNTHESIS_TIMEOUT_SEC,
    }


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        proc = job.get("process")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                logger.info("job %s cancel requested (pid %s)", job_id, proc.pid)
                return {"ok": True, "terminated": True}
            except Exception as exc:
                raise HTTPException(500, f"terminate failed: {exc}")
        return {"ok": True, "terminated": False, "status": job["status"]}


def main():
    # Render once on startup so the embedded PROJECT_ROOT placeholder is fresh.
    try:
        render_dashboard(MANIFEST_PATH, DASHBOARD_PATH)
    except Exception:
        logger.exception("initial dashboard render failed (continuing anyway)")
    logger.info("starting dashboard server on http://%s:%d", HOST, PORT)
    logger.info("project: %s", PROJECT_DIR)
    logger.info("watch script: %s", WATCH_SCRIPT)
    if CLAUDE_BIN:
        logger.info("claude CLI: %s (auto-synthesis enabled)", CLAUDE_BIN)
    else:
        logger.warning("claude CLI not found on PATH — auto-synthesis disabled")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
