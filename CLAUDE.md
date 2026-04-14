# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Single-script tool (`claude_usage.py`) that scrapes the output of Claude Code's interactive `/usage` slash command and writes the parsed result to `usage.txt`. Intended to expose usage data for an external status page.

## Run

```bash
python claude_usage.py
```

Requires `pyte` and `pywinpty` (Windows-only — uses `winpty.PtyProcess`).

## Architecture

The `/usage` command only renders inside an interactive TTY, so the script:

1. Spawns `claude` in a Windows pseudo-terminal at a fixed 120x40 size (`PtyProcess.spawn`).
2. Sleeps to let the TUI boot, sends `/usage\r`, then sleeps again to let it render. Timings in `get_usage()` are load-bearing — the script has no readiness signal and relies on fixed delays.
3. Feeds the captured raw byte stream through a `pyte` virtual terminal to resolve ANSI escapes into a final screen buffer.
4. `extract_usage()` slices lines between the `Current session` header and the `Esc to cancel` footer.
5. `os._exit(0)` is used because the reader thread + child PTY would otherwise block normal interpreter shutdown.

If `/usage` output format, header, or footer text changes upstream in Claude Code, `extract_usage()` will silently return empty/wrong data — that parser is the fragile seam.
