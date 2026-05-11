# /watch — local-WhisperX fork

Fork of [bradautomates/claude-video](https://github.com/bradautomates/claude-video) that routes Whisper transcription to a self-hosted WhisperX LXC over Tailscale. **Zero paid-API surface.**

## What changed from upstream

| Concern | Upstream | This fork |
|---|---|---|
| Transcription backend | Groq Whisper API or OpenAI Whisper API | Self-hosted WhisperX (`large-v3` on RTX 4090 LXC) |
| Required secrets | `GROQ_API_KEY` or `OPENAI_API_KEY` in `~/.config/watch/.env` | None. Optional `LP_WHISPERX_URL` override only. |
| Default endpoint | `api.groq.com` / `api.openai.com` | `http://lp-whisperx:8080` (Tailscale) |
| Cold start | None | 2–4 min on first call after LXC boot, then seconds |
| Cost per run | ~$1 (per upstream's measurements) | $0 in API spend; GPU power only |
| Setup health probe | None | `setup.py --check` probes `/health` and exits 3 if WhisperX is down |
| `--whisper` choices | `groq`, `openai` | `local-whisperx` (only) |

The yt-dlp + ffmpeg pipeline is unchanged — captions still take the free path; WhisperX only fires on no-caption videos.

## Install

### Claude Code (plugin marketplace)

```bash
/plugin marketplace add dinesh01ameh/claude-video-local-whisperx
/plugin install watch@claude-video-local-whisperx
```

### Cowork desktop / claude.ai web

1. Build the bundle: `bash scripts/build-skill.sh` (produces `watch.skill`)
2. Drop `watch.skill` into Settings → Capabilities → Skills

### Codex / generic

```bash
git clone https://github.com/dinesh01ameh/claude-video-local-whisperx.git ~/.codex/skills/watch
```

## Configuration

`~/.config/watch/.env` (created by `setup.py`):

```ini
# Override the default WhisperX endpoint if your LXC moved.
LP_WHISPERX_URL=

# Reserved knob; only "local-whisperx" is supported in this fork.
WATCH_BACKEND=
```

The default endpoint is `http://lp-whisperx:8080`. If your service is reachable at the default (Tailscale name resolution + ACL allows your machine as src), no config is needed.

## Health probe

`setup.py --check` exit codes:

| Exit | Meaning |
|---|---|
| 0 | Ready |
| 2 | yt-dlp / ffmpeg / ffprobe missing |
| 3 | WhisperX unreachable (LXC down, Tailscale down, ACL block, etc.) |
| 4 | Both |

For diagnostic detail: `setup.py --json` returns `{whisperx_url, whisperx_reachable, whisperx_detail, ...}`.

## Wire protocol

This fork talks to a WhisperX HTTP service that exposes:

```
POST /transcribe-upload    multipart {audio: file, language: "en"}  →  {job_id}
GET  /jobs/{id}/status                                                →  {status, progress, error}
GET  /jobs/{id}/result                                                →  {words[], segments[], language, model_id, diarized}
GET  /health                                                          →  {model_loaded, gpu_memory_used_mb, queue_depth, ...}
```

Async upload + poll (5 s interval, 10 min timeout). The vendored client is at `scripts/local_whisperx.py`.

## Running the dashboard server (optional, recommended)

By default, the dashboard is a static HTML file — you copy commands and paste them into a terminal. For one-click operation, run the bundled server:

```powershell
pip install --user fastapi uvicorn
$env:WATCH_PROJECT_DIR = "D:\Ai-work\Triage\Triage Knowledge System"
cd "D:\Ai-work\Triage\Triage Knowledge System\claude-video-local-whisperx"
python scripts\dashboard_server.py
```

Then open http://localhost:4893 in your browser. Paste a URL in the dashboard input → click Add → server runs the preview for you. Watch job progress live. New rows appear without manual refresh.

Marker workflow: click **Send to Claude** on a preview-ready row → server runs `--focused` extraction → `focused-report.md` saved to the work dir → one-click prompt copy for Cowork or Claude Code (the synthesis step still needs a Claude session; the extraction step is automated).

If the server isn't running, the dashboard falls back to clipboard mode automatically — open `dashboard.html` directly via `file://` for the same UI minus the live job polling.

The server runs on `127.0.0.1:4893` (override with `$env:WATCH_SERVER_PORT=4894`). Localhost-only by design — no auth, no external exposure. Logs rotate at 10 MB into `<project>/.watch-cache/server.log`.

Synthesis defaults to Claude Sonnet at low effort — fast and cheap for the NLM template work. Override via env vars:

```powershell
$env:CLAUDE_SYNTHESIS_MODEL = "opus"      # default: sonnet
$env:CLAUDE_SYNTHESIS_EFFORT = "medium"   # default: low
$env:CLAUDE_SYNTHESIS_TIMEOUT_SEC = "1200"
python scripts\dashboard_server.py
```

## License

MIT, same as upstream. Original work © Bradley Bonanno; modifications © Dinesh Raj.
