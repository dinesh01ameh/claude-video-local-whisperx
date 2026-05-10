---
name: watch
description: Watch a video (URL or local path). Downloads with yt-dlp, extracts auto-scaled frames with ffmpeg, pulls the transcript from captions (or local WhisperX fallback), and hands the result to Claude so it can answer questions about what's in the video.
argument-hint: "<video-url-or-path> [question]"
allowed-tools: Bash, Read, AskUserQuestion
homepage: https://github.com/dinesh01ameh/claude-video-local-whisperx
repository: https://github.com/dinesh01ameh/claude-video-local-whisperx
author: dinesh01ameh
license: MIT
user-invocable: true
---

# /watch — Claude watches a video

You don't have a video input; this skill gives you one. A Python script downloads the video, extracts frames as JPEGs, gets a timestamped transcript (native captions first, then local WhisperX LXC as fallback — no paid APIs), and prints frame paths. You then `Read` each frame path to see the images and combine them with the transcript to answer the user.

## Step 0 — Setup preflight (runs every `/watch` invocation, silent on success)

**Python interpreter:** every `python3 ...` command in this skill is for macOS/Linux. On **Windows**, substitute `python` — the `python3` command on Windows is the Microsoft Store stub and will not run the script.

Before every `/watch` run, verify that dependencies and WhisperX are reachable:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py" --check
```

This is a <100ms lookup. On exit 0, the script emits **nothing** — proceed to Step 1 without comment. **Do NOT announce "setup is complete" to the user** — they don't need a status message on every turn. The only acceptable user-visible output from Step 0 is when remediation is required.

On non-zero exit, follow the table:

| Exit | Meaning | Action |
|------|---------|--------|
| `2` | Missing binaries (`ffmpeg` / `ffprobe` / `yt-dlp`) | Run installer |
| `3` | WhisperX unreachable | Diagnose Tailscale / LXC / ACL using `setup.py --json` for detail |
| `4` | Both missing | Run installer first, then troubleshoot WhisperX |

The installer is idempotent — safe to re-run:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py"
```

On macOS with Homebrew, it auto-installs `ffmpeg` and `yt-dlp`. On Linux/Windows, it prints the exact install commands for the user to run. It scaffolds `~/.config/watch/.env` with commented placeholders at `0600` perms, and writes `SETUP_COMPLETE=true` once deps + a key are in place so the next session knows this user has already been through the wizard.

**If WhisperX is still unreachable after install:** run `python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py" --json` and inspect `whisperx_detail`. Common causes:
- Tailscale not running on this machine (start tailscale, verify with `tailscale status`)
- LXC container down or model not loaded (ssh to host, check the LXC)
- ACL doesn't allow this machine as src to port 8080 (one-line edit in tailnet admin)
- Service moved — set `LP_WHISPERX_URL=http://<new-host>:<port>` in `~/.config/watch/.env`

If the user wants to proceed without WhisperX, run /watch with `--no-whisper` and tell them videos without native captions will come back frames-only.

**Structured mode (optional):** `python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py" --json` emits `{status, first_run, missing_binaries, whisper_backend, whisperx_url, whisperx_reachable, whisperx_detail, config_file, platform}` where `status` is one of `ready | needs_install | needs_whisperx | needs_install_and_whisperx`. Use this when you need to branch on specifics (e.g. "is this the user's very first run?" → `first_run: true`, or "why is WhisperX down?" → `whisperx_detail`).

Within a single session, you can skip Step 0 on follow-up `/watch` calls — once `--check` returned 0, nothing about the environment changes between turns.

## When to use

- User pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, most yt-dlp-supported sites) and asks about it.
- User points at a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, etc.) and asks about it.
- User types `/watch <url-or-path> [question]`.

## Recommended limits

- **Best accuracy: videos under 10 minutes.** Frame coverage scales inversely with duration.
- **Hard caps: 100 frames total and 2 fps.** Token cost grows with frame count, so the script targets a frame budget by duration (and never exceeds 2 fps even when the budget would imply more):
  - ≤30s → ~1-2 fps (up to 30 frames)
  - 30s-1min → ~40 frames
  - 1-3min → ~60 frames
  - 3-10min → ~80 frames
  - \>10min → 100 frames, sparsely spaced (warning printed)
- If the user hands you a long video, consider asking whether they want a specific section before burning tokens on a sparse scan.

## How to invoke

**Step 1 — parse the user input.** Separate the video source (URL or path) from any question the user asked. Example: `/watch https://youtu.be/abc what language is this in?` → source = `https://youtu.be/abc`, question = `what language is this in?`.

**Step 2 — run the watch script.** Pass the source verbatim. Do not shell-escape it yourself beyond normal quoting:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" "<source>"
```

Optional flags:
- `--start T` / `--end T` — focus on a section. Accepts `SS`, `MM:SS`, or `HH:MM:SS`. When either is set, fps auto-scales denser (see "Focusing on a section" below).
- `--max-frames N` — lower the cap for tighter token budget (e.g. `--max-frames 40`)
- `--resolution W` — change frame width in px (default 512; bump to 1024 only if the user needs to read on-screen text)
- `--fps F` — override auto-fps (clamped to 2 fps max)
- `--out-dir DIR` — keep working files somewhere specific (default: an auto-generated tmp dir)
- `--whisper local-whisperx` — accepted for backwards-compat; only backend supported in this fork
- `--no-whisper` — disable the Whisper fallback entirely (frames-only if no captions)

### Focusing on a section (higher frame rate)

When the user asks about a specific moment — "what happens at the 2 minute mark?", "zoom into 0:45 to 1:00", "the first 10 seconds" — pass `--start` and/or `--end`. The script switches to focused-mode budgets, which are denser than full-video budgets (still capped at 2 fps):

- ≤5s → 2 fps (up to 10 frames)
- 5-15s → 2 fps (up to 30 frames)
- 15-30s → ~2 fps (up to 60 frames)
- 30-60s → ~1.3 fps (up to 80 frames)
- 60-180s → ~0.6 fps (100 frames, capped)

Focused mode is the right call for:
- Any moment/range the user names explicitly ("around 2:30", "the intro", "the last 30 seconds").
- Any video longer than ~10 minutes where the user's question is about a specific part — running focused on the relevant section is far more useful than a sparse scan of the whole thing.
- Re-runs after a full scan didn't have enough detail in some region.

Transcript is auto-filtered to the same range. Frame timestamps are absolute (real video timeline, not offset-from-start).

Examples:
```bash
# Last 10 seconds of a 1 minute video
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" video.mp4 --start 50 --end 60

# Zoom into 2:15 → 2:45 at 3 fps (90 frames)
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" "$URL" --start 2:15 --end 2:45 --fps 3

# From 1h12m to the end of the video
python3 "${CLAUDE_SKILL_DIR}/scripts/watch.py" "$URL" --start 1:12:00
```

**Step 3 — Read every frame path the script lists.** The Read tool renders JPEGs directly as images for you. Read all frames in a single message (parallel tool calls) so you see them together. The frames are in chronological order with a `t=MM:SS` timestamp so you can align them to the transcript.

**Step 4 — answer the user.** You now have two streams of evidence:
- **Frames** — what's on screen at each timestamp
- **Transcript** — what's said at each timestamp. The report's header shows the source (`captions` = yt-dlp pulled native subs; `whisper (local-whisperx)` = transcribed by the local WhisperX LXC).

If the user asked a specific question, answer it directly citing timestamps. If they didn't ask anything, summarize what happens in the video — structure, key moments, notable visuals, spoken content.

**Step 5 — clean up.** The script prints a working directory at the end. If the user isn't going to ask follow-ups about this video, delete it with `rm -rf <dir>`. If they might, leave it in place.

## Transcription

The script gets a timestamped transcript in one of two ways:

1. **Native captions (free, preferred).** yt-dlp pulls manual or auto-generated subtitles from the source platform if available.
2. **Whisper API fallback.** If no captions came back (or the source is a local file), the script extracts audio (`ffmpeg -vn -ac 1 -ar 16000 -b:a 64k`, ~0.5 MB/min) and uploads it to whichever Whisper API has a key configured:
   - **Local WhisperX** — `whisperx large-v3` on the user's RTX 4090 LXC at `http://lp-whisperx:8080` (override via `LP_WHISPERX_URL`). Word-level timestamps, no diarization, language pinned to `en`. Cold-start adds 2-4 min on first call after LXC boot; subsequent calls are seconds.

Configuration lives in `~/.config/watch/.env`. The endpoint defaults to `http://lp-whisperx:8080` and is overridable via `LP_WHISPERX_URL`. Use `--no-whisper` to skip the fallback entirely (frames-only output).

## Failure modes and handling

- **Setup preflight failed** → run `python3 "${CLAUDE_SKILL_DIR}/scripts/setup.py"` (auto-installs ffmpeg/yt-dlp via brew on macOS, scaffolds the `.env`). For WhisperX errors, run `setup.py --json` and inspect `whisperx_detail`.
- **No transcript available** → captions missing AND (no Whisper key OR Whisper API failed). Script prints a hint pointing to setup. Proceed frames-only and tell the user.
- **Long video warning printed** → acknowledge it in your answer. Offer to re-run focused on a specific section via `--start`/`--end` rather than a sparse full-video scan.
- **Download fails** → yt-dlp's error goes to stderr. If it's a login-required or region-locked video, tell the user plainly; do not keep retrying.
- **Whisper request fails** → the error is printed to stderr (likely: WhisperX cold-starting longer than the 10-min poll timeout, or transient network hiccup). The report will say "none available" for transcript. Retry once; cold-start is a one-time cost per LXC boot.

## Token efficiency

This skill burns tokens primarily on frames. Order of magnitude:
- 80 frames at 512px wide is roughly 50-80k image tokens depending on aspect ratio.
- The transcript is cheap (a few thousand tokens at most for a 10-minute video).
- Bumping `--resolution` to 1024 roughly quadruples the image tokens per frame. Only do it when necessary.

If you already watched a video this session and the user asks a follow-up, do **not** re-run the script — you already have the frames and transcript in context. Just answer from what you have.

## Project folder & dashboard

Set the env var `WATCH_PROJECT_DIR` to a folder you keep your knowledge work in (e.g. `D:\Ai-work\Triage\Triage Knowledge System` on Windows). When set:

- Per-run working dirs go under `<project>/.watch-cache/watch-<hash>/` instead of system temp. Files there are visible to the Read tool in both Cowork and Claude Code.
- After every run, `<project>/.watch-cache/index.json` gets a new record and `<project>/.watch-cache/dashboard.html` is regenerated.

Open `dashboard.html` in any browser to see every video you've processed: title, uploader, source URL, duration, frame count, transcript source, work-dir link, status. Sortable, filterable, no external dependencies. Self-contained — opens fine on `file://`.

If `WATCH_PROJECT_DIR` is unset, the skill falls back to system temp (no manifest, no dashboard) — same behavior as upstream.

To set the env var:
- Windows: `[System.Environment]::SetEnvironmentVariable("WATCH_PROJECT_DIR", "D:\Ai-work\Triage\Triage Knowledge System", "User")` (in PowerShell, then restart your shell or Claude Code)
- macOS/Linux: `export WATCH_PROJECT_DIR=~/triage-knowledge` in `~/.zshrc` / `~/.bashrc`

The `--out-dir` flag still works and overrides this — useful for one-off runs you don't want recorded in the dashboard.

## Modes — preview / focused / default

The skill has three invocation modes. Pick based on the video and the user's question.

### Default mode (full pipeline)
`/watch <url-or-path> [--start ... --end ...]` — extracts dense frames + transcript and reads it all. Good for: short clips (<10 min), focused start/end ranges, or when the user wants a full summary right now.

### Preview mode (sparse, dashboard-driven)
`python scripts/watch.py --preview <url-or-path>` — downloads the video, extracts ~8-60 sparse timeline thumbs (1 per 10s), pulls the transcript. **Does NOT invoke Claude.** The user opens the dashboard, marks must-watch and skip segments, then runs focused mode. Use when:
- Video > 10 minutes (default-mode token cost is unbounded)
- User wants to curate before Claude processes
- The dashboard is in the workflow

### Focused mode (process marked segments only)
`python scripts/watch.py --focused --work-dir <dir> --segments-json '[...]' --user-review '...'` — runs against an existing preview. Uses an **audio-only baseline**: every transcript line ships to Claude by default, and frames are only extracted inside `must` ranges. Token cost scales with audio length, not frame count.

The dashboard's "Mark segments" button generates the exact focused-mode command for the user to paste. When you receive a paste like that, run it via Bash, then process per the NLM template above.

### The three marker types in focused mode

Focused mode uses an **audio-only baseline**: every transcript line goes to Claude by default, no frames. Markers override:

- **M (must)** — extract dense frames in this range. Use for slide decks, code on screen, demos, UI walkthroughs, anything where what's shown matters as much as what's said.
- **A (audio-only)** — explicit annotation that this section is talking-head / audio-driven. Functionally identical to baseline; useful when you want to attach an intent like "listen for the punchline" so it shows up in the segments list.
- **X (exclude)** — drop the transcript here too. Use for sponsor reads, intros, outros, dead air, music interludes — anything that should not reach Claude at all.

Token impact: an 11-minute talking-head video with zero markers costs ~6k tokens (full transcript, no frames). The same video as a "must everything" mark costs ~85k. Mark visual moments as M to selectively add frame tokens where they earn their cost.

Backwards-compat: legacy `type="skip"` in `--segments-json` is silently aliased to `type="exclude"` — old marker drafts and old generated commands keep working.

## NotebookLM-ready output (Triage Knowledge System integration)

When the user asks for output suitable for **pasting into a NotebookLM topic notebook as a text source**, follow this template. NLM cannot see the JPEG frames directly, so the template embeds your frame descriptions inline at their timestamps — that is the value-add this skill provides over feeding NLM a YouTube URL alone.

### When to use this template

- User says "watch this and put it in NLM" / "feed this to my [topic] notebook" / "make it NLM-ready"
- User is in a Triage session and the source is a video — the NLM topic notebook is the persistent home for the source
- Default to this format whenever the video is going into long-lived knowledge storage rather than a one-off question

### Template (emit verbatim, fill in the bracketed fields)

The new NLM template splits the synthesis into three lenses NotebookLM treats as distinct sources: **what was said** (audio-derived), **what was shown** (frame-derived), and **what it means together** (joint understanding). Each section is independent enough that NLM can answer "what did the speaker say about X" using just spoken-content vs "what was shown when discussing X" using just visual-content.

```markdown
# [Video title — match the source title exactly]

**Source:** [URL or local path]
**Uploader / channel:** [if known from yt-dlp metadata]
**Duration:** [HH:MM:SS]
**Watched:** [ISO date, today]
**Transcript source:** [captions | local-whisperx]
**Marked segments:** [list of must / audio-only / exclude ranges if focused mode was used; "audio-only baseline (no markers)" if none were dropped]
**User context:** [verbatim quote of --user-review if provided, or "none"]

## What this video is

[3-5 sentences. Plain language. What it actually covers — strip marketing claims. Name the speaker(s). State the genre: lecture, demo, walkthrough, interview, ad, etc.]

## Spoken content

[Transcript-grounded synthesis. ONLY claims, arguments, and statements that came from the audio/transcript. Per-section timestamps. Skip filler. If the user marked specific segments, scope this to those.]

- [HH:MM-HH:MM] [What was said]
- [HH:MM-HH:MM] [What was said]

### Direct quotes (verbatim from transcript)

> [HH:MM] "[Exact quote]" — [speaker if relevant]

## Visual content

[Frame-grounded synthesis. ONLY observations from frames that add information beyond what's said. Skip pure talking-head frames. Include: code on screen, data tables, slides, charts, UI demos, product screenshots, written-out frameworks, on-screen captions / quotes / overlays.]

- [HH:MM] [What is shown and why it matters]
- [HH:MM] [What is shown and why it matters]

## Synthesis

[The joint understanding — where audio + visual together produce meaning that neither alone delivered. 3-6 bullets. This is where you connect "speaker says X while showing Y" interpretations. If the video is purely talking-head with nothing notable on screen, this section is short or omitted. If the user provided context via --user-review, integrate it here.]

- [HH:MM] [Joint observation: spoken + shown together]

## Open questions / unverified claims

[Bullet list of claims the speaker made that need external verification before being treated as fact. If the video is purely instructional with no contested claims, omit this section.]

## Tags

[Comma-separated tags relevant to the user's project structure. Default tags: video, [topic-name]. Add project tags (mms, pkc, amrocky, sheeltron, serversupply, byrefab) only if the content is directly applicable to that project.]
```

### Section sourcing (where each lens pulls from)

Phase 4's audio-only baseline changes what each NLM section can cite. Honor the boundaries:

- **`## Spoken content`** — sources from the **full audio timeline** the focused-mode pipeline gave you (full transcript minus any `exclude` ranges, which are already removed). This is everything Claude was told audibly. If you didn't see a quote here, you can't cite it.
- **`## Visual content`** — sources **only from frames inside `must` segments**. Frames were not extracted outside must ranges, so anything visual you mention here must be backed by a specific frame path. If the user dropped zero `must` markers, this section is short or omitted.
- **`## Synthesis`** — the joint understanding. Where audio + visuals together produce meaning that neither alone delivered. If there are no `must` frames, this section degrades to "audio-only synthesis" and may also be brief.

Audio-only segments (`A` markers) do **not** get their own NLM section. They're workflow flags the user dropped to annotate intent ("listen for the punchline"), not their own content lens. Fold any insight from those ranges into `## Spoken content` where relevant.

### Style rules for NLM-ready output

- **No hedging.** Don't write "the speaker seems to suggest" — either they said it (quote it) or they didn't.
- **No transcript dump.** Never paste the full transcript into the NLM source — NLM already pulls the YouTube transcript on its own. The value here is your synthesis + frame-grounded observations.
- **Cite timestamps for every claim.** NLM grounds responses on timestamps; bare claims with no timestamps are demoted by NLM's retrieval.
- **Frame observations only when frames add signal.** A frame of a person talking is not a frame observation. A frame of their slide deck or their screen is.
- **No emojis. No "key takeaways" header.** NLM users get noise from those; treat this as a research note, not a blog post.
- **One template, one paste.** Output the entire template as one Markdown block the user can copy in a single action.

### After producing the template

Three things, in this order:

1. **Save your full analysis to `<work_dir>/focused-result.md`** using the Write tool. This is the long-form analysis the user reads in the dashboard's "Result" preview button. Include everything you reasoned through — quotes, frame observations, why the video matters, etc. Verbose is fine here.

2. **Save the NLM-ready Markdown block to `<work_dir>/nlm-summary.md`** using the Write tool. This is the structured paste-ready version with `## Spoken content` / `## Visual content` / `## Synthesis` sections (per the template above). Concise. NLM-optimized.

3. **Tell the user, in one line:** `Focused result + NLM summary saved to <work_dir>. Open the dashboard, click 'Result' to review, then click 'NLM' to copy and paste into your "[topic name]" notebook.`

Both files are required — the dashboard's "Result" button reads `focused-result.md`, the "NLM" button reads `nlm-summary.md`. Don't skip either.

Do not summarize the summary in chat.

## Security & Permissions

**What this skill does:**
- Runs `yt-dlp` locally to download the video and pull native captions when the source supports them (public data; the request goes directly to whatever host the URL points at)
- Runs `ffmpeg` / `ffprobe` locally to extract frames as JPEGs and, when Whisper is needed, a mono 16 kHz audio clip
- Sends the extracted audio clip to a self-hosted WhisperX LXC over Tailscale (default `http://lp-whisperx:8080`, override via `LP_WHISPERX_URL`). No third-party API is contacted for transcription.
- Writes the downloaded video, frames, audio, and an intermediate transcript to a working directory under the system temp dir (or `--out-dir` if specified) so Claude can `Read` them
- Reads / creates `~/.config/watch/.env` (mode `0600`) to store `LP_WHISPERX_URL` (optional) and a `SETUP_COMPLETE` marker. As a fallback, also reads `.env` in the current working directory

**What this skill does NOT do:**
- Does not upload the video itself to any API — only the extracted audio goes out, and only when native captions are missing AND Whisper is not disabled with `--no-whisper`
- Does not access any platform account (no login, no session cookies, no posting)
- No paid API keys are read, stored, or transmitted. The audio clip leaves the local machine only to reach the user's own WhisperX LXC over their tailnet.
- Does not persist anything outside the working directory and `~/.config/watch/.env` — clean up the working directory when you're done (Step 5)

**Bundled scripts:** `scripts/watch.py` (entry point), `scripts/download.py` (yt-dlp wrapper), `scripts/frames.py` (ffmpeg frame extraction), `scripts/transcribe.py` (VTT caption parser), `scripts/whisper.py` (orchestrator wrapping local_whisperx), `scripts/local_whisperx.py` (vendored client for the WhisperX LXC), `scripts/setup.py` (preflight + installer with health probe)

Review scripts before first use to verify behavior.
