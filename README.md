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

## License

MIT, same as upstream. Original work © Bradley Bonanno; modifications © Dinesh Raj.
