# Changelog

## 0.2.0-local-whisperx — 2026-05-09

Fork of bradautomates/claude-video v0.1.3.

### Changed
- **Transcription backend swapped:** all non-caption transcription now routes to a self-hosted WhisperX LXC at `http://lp-whisperx:8080` (override via `LP_WHISPERX_URL`). Async upload + poll protocol.
- `scripts/whisper.py` rewritten to wrap `scripts/local_whisperx.py` (vendored client, ~200 LOC, stdlib only).
- `scripts/setup.py` rewritten: `_have_api_key()` → `_whisperx_reachable()`, env template now contains `LP_WHISPERX_URL` instead of `GROQ_API_KEY` / `OPENAI_API_KEY`.
- `hooks/scripts/check-setup.sh` updated to probe `/health` instead of checking for API keys.
- `SKILL.md` updated: removed Groq/OpenAI provider section; documented local backend, cold-start behavior, and `whisperx_detail` diagnostic.
- `--whisper` choices reduced to `[local-whisperx]` (kept for upstream API compat).

### Removed
- Groq and OpenAI Whisper paths. No paid-API code remains in the fork. If WhisperX is unreachable, behavior is frames-only (same as upstream's `--no-whisper`).
- `GROQ_API_KEY` and `OPENAI_API_KEY` env / dotenv handling.

### Added
- `scripts/local_whisperx.py` — vendored client with cold-start notice, progress polling, and health probe.
- `setup.py --check` health probe (exit 3 → WhisperX unreachable, exit 4 → both binaries and WhisperX missing).
- `setup.py --json` returns `whisperx_url`, `whisperx_reachable`, `whisperx_detail` for diagnosis.

---

## Upstream history

# Changelog

All notable changes to `/watch` are documented here.

## [0.1.3] — 2026-05-09

### Fixed
- Windows: `video.info.json` is read as UTF-8 (#4). Previously `Path.read_text()` defaulted to cp1252 on Windows and crashed on yt-dlp's UTF-8 output, silently dropping Title/Uploader from the report. Same fix applied to `.env` reads/writes in `whisper.py` and `setup.py`.
- `download.py` now logs info.json parse failures to stderr instead of swallowing them.

### Security
- Hardened subprocess argv against option injection (#2): inserted `--` before the URL in the yt-dlp argv, and tightened `is_url` to reject `-`-prefixed sources and require a non-empty netloc. Resolved video/audio paths to absolute via `Path.resolve()` before passing to `ffmpeg`/`ffprobe`, so a relative path starting with `-` can't be misinterpreted as a flag.

## [0.1.2] — 2026-04-24

### Fixed
- Windows console crash: removed the emoji from the long-video warning in `watch.py`; cp1252 consoles couldn't encode it.
- `setup.py` now prints `winget` / `pip` install commands on Windows instead of "unsupported platform" — matches what the README already promised.

### Changed
- `SKILL.md` notes that on Windows the scripts must be invoked with `python`, not `python3` (the latter is the Microsoft Store stub on Windows).

## [0.1.1] — 2026-04-24

### Fixed
- Added `commands/watch.md` shim so `/watch` is callable when installed as a Claude Code plugin. Without it, the plugin loaded but the skill wasn't exposed as a slash command.
- `scripts/build-skill.sh` now strips `commands/` from the claude.ai `.skill` bundle alongside `hooks/` and `.claude-plugin/`.

## [0.1.0] — 2026-04-24

Initial marketplace release.

### Added
- `/watch <url-or-path> [question]` slash command.
- yt-dlp download with native caption extraction (manual + auto-subs).
- ffmpeg frame extraction with auto-scaled fps (≤2 fps, ≤100 frames, duration-aware budget).
- `--start` / `--end` focused mode with denser frame budget and transcript range filtering.
- Whisper fallback (Groq preferred, OpenAI secondary) for videos without captions.
- `setup.py` preflight: silent `--check`, structured `--json`, and installer that auto-runs `brew install` on macOS.
- Session-start hook that prints a one-line status on first run / partial config.
- `.skill` bundle packaging for claude.ai upload via `scripts/build-skill.sh`.
