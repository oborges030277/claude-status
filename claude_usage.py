import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyte
from winpty import PtyProcess

try:
    from zoneinfo import ZoneInfo
    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:
    _BERLIN = None

ROOT = Path(__file__).resolve().parent
COLS, ROWS = 120, 40
INTERVAL_SECONDS = 5 * 60
SLEEP_TICK = 1.0
HISTORY_KEEP_DAYS = 30
HISTORY_FILE = "history.jsonl"
SESSIONS_FILE = "sessions.json"

_stop = threading.Event()


def _handle_sigint(signum, frame):
    _stop.set()


_PCT_RE = re.compile(r"\d+\s*%\s*used")
_SECTIONS_FOR_SCORE = (
    "Current session",
    "Current week (all models)",
    "Current week (Sonnet only)",
)


def _snapshot_score(snap: list[str]) -> int:
    # Strict scoring: every section title must appear exactly once (duplicates
    # signal a mid-reflow frame), and the percent must sit on the very next
    # non-empty line — not inside a later section's rows.
    count = 0
    for title in _SECTIONS_FOR_SCORE:
        positions = [i for i, line in enumerate(snap) if title in line]
        if len(positions) != 1:
            return 0
        i = positions[0]
        for j in range(i + 1, min(i + 3, len(snap))):
            if any(t in snap[j] for t in _SECTIONS_FOR_SCORE):
                break
            if _PCT_RE.search(snap[j]):
                count += 1
                break
    return count


def render(raw: str) -> list[str]:
    # The /usage TUI first renders the three "% used" bars and only afterwards
    # pushes in the "Last 24h" tips, which overwrite the Current session percent.
    # Feed pyte in chunks, snapshot after each, and keep the frame that has the
    # most complete set of section percents visible.
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    best: list[str] = []
    best_score = -1
    prev: tuple[str, ...] | None = None
    for i in range(0, len(raw), 256):
        stream.feed(raw[i : i + 256])
        snap_tuple = tuple(line.rstrip() for line in screen.display)
        if snap_tuple == prev:
            continue
        prev = snap_tuple
        snap = list(snap_tuple)
        score = _snapshot_score(snap)
        # Use >= to prefer the latest snapshot at max score: early frames may
        # still be rendering lines character-by-character, later frames (just
        # before the tips overlay drops the session percent) are fully settled.
        if score >= best_score:
            best_score = score
            best = snap
    return best or [line.rstrip() for line in screen.display]


def extract_usage_block(lines: list[str]) -> list[str]:
    out, capture = [], False
    for line in lines:
        if "Current session" in line:
            capture = True
        if capture:
            if line.strip() == "Esc to cancel":
                break
            if line.strip():
                out.append(line)
    return out


SECTION_TITLES = (
    "Current session",
    "Current week (all models)",
    "Current week (Sonnet only)",
    "Extra usage",
)


def parse_usage(block: list[str]) -> dict:
    sections = []
    extra = ""
    current = None
    for line in block:
        stripped = line.strip()
        matched_title = next((t for t in SECTION_TITLES if stripped == t), None)
        if matched_title:
            if matched_title == "Extra usage":
                current = {"title": matched_title, "_extra": True}
            else:
                current = {"title": matched_title, "percent": None, "reset": ""}
            sections.append(current)
            continue
        if current is None:
            continue
        if current.get("_extra"):
            extra = (extra + " " + stripped).strip()
            continue
        m = re.search(r"(\d+)\s*%\s*used", stripped)
        if m and current["percent"] is None:
            current["percent"] = int(m.group(1))
        rm = re.search(r"(Resets[^%]*?)(?:\s{2,}|\s*\d+\s*%|$)", stripped)
        if rm and not current["reset"]:
            reset_text = re.sub(r"[\u2580-\u259F\u2588]+", "", rm.group(1)).strip()
            if reset_text.lower().startswith("resets"):
                current["reset"] = reset_text
    bars = [s for s in sections if not s.get("_extra")]
    return {
        "sections": bars,
        "extra": extra,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


_RESET_RE = re.compile(r"Resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])m", re.IGNORECASE)


def normalize_reset(reset_str: str, now_utc: datetime | None = None) -> str | None:
    # "Resets 11:30am (Europe/Berlin)" -> next future occurrence of 11:30 in
    # Europe/Berlin, returned as UTC ISO. Session resets are always within 5h;
    # week resets can be further out but we only care about the next occurrence
    # of the wall-clock time (day-of-week is not in the string).
    if not reset_str or _BERLIN is None:
        return None
    m = _RESET_RE.search(reset_str)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    is_pm = m.group(3).lower() == "p"
    if is_pm and hour != 12:
        hour += 12
    elif not is_pm and hour == 12:
        hour = 0
    now = now_utc or datetime.now(timezone.utc)
    now_berlin = now.astimezone(_BERLIN)
    candidate = now_berlin.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now_berlin:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat(timespec="seconds")


def _section_by_title(data: dict, title: str) -> dict | None:
    for s in data.get("sections", []):
        if s.get("title") == title:
            return s
    return None


def _read_last_line(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as f:
        try:
            f.seek(-4096, os.SEEK_END)
        except OSError:
            f.seek(0)
        tail = f.read().decode("utf-8", errors="replace")
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def append_sample(data: dict, history_path: Path) -> int:
    sess = _section_by_title(data, "Current session")
    week = _section_by_title(data, "Current week (all models)")
    if sess is None or week is None:
        return 0
    sess_pct = int(sess.get("percent") or 0)
    week_pct = int(week.get("percent") or 0)
    now_utc = datetime.fromisoformat(data["updated_utc"])
    sr = normalize_reset(sess.get("reset") or "", now_utc)

    prev_sid = 0
    prev_sr = None
    last = _read_last_line(history_path)
    if last:
        try:
            prev = json.loads(last)
            prev_sid = int(prev.get("sid", 0))
            prev_sr = prev.get("sr")
        except Exception:
            pass

    sid = prev_sid or 1
    # Strict advance with 60s jitter tolerance.
    if sr and prev_sr:
        try:
            sr_dt = datetime.fromisoformat(sr)
            prev_sr_dt = datetime.fromisoformat(prev_sr)
            if (sr_dt - prev_sr_dt).total_seconds() > 60:
                sid = prev_sid + 1
        except Exception:
            pass
    elif sr and not prev_sr:
        sid = prev_sid + 1 if prev_sid else 1

    record = {
        "t": data["updated_utc"],
        "sess": sess_pct,
        "week": week_pct,
        "sr": sr,
        "sid": sid,
    }
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    return sid


def prune_history(history_path: Path, keep_days: int = HISTORY_KEEP_DAYS) -> None:
    if not history_path.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept: list[str] = []
    changed = False
    with history_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                changed = True
                continue
            try:
                row = json.loads(line)
                t = datetime.fromisoformat(row["t"])
                if t >= cutoff:
                    kept.append(line.rstrip("\n"))
                else:
                    changed = True
            except Exception:
                kept.append(line.rstrip("\n"))
    if changed:
        history_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def detect_sessions(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    groups: dict[int, list[dict]] = {}
    for r in rows:
        groups.setdefault(int(r.get("sid", 0)), []).append(r)
    max_sid = max(groups.keys())
    out: list[dict] = []
    for sid, g in sorted(groups.items()):
        g.sort(key=lambda r: r["t"])
        peak = max(int(r.get("sess") or 0) for r in g)
        week_start = int(g[0].get("week") or 0)
        week_end = int(g[-1].get("week") or 0)
        delta_week = week_end - week_start
        # Normalized "what would this session cost at full utilization" — valid
        # as soon as the user has used >=10% of the session, so partial sessions
        # contribute too (user rarely drains a 5h block to 100%).
        norm_delta_week = (delta_week / peak * 100) if peak >= 10 else None
        ratio = (delta_week / peak) if peak >= 10 else None
        out.append(
            {
                "sid": sid,
                "start_t": g[0]["t"],
                "end_t": g[-1]["t"],
                "peak_sess": peak,
                "week_start": week_start,
                "week_end": week_end,
                "delta_week": delta_week,
                "norm_delta_week": round(norm_delta_week, 2) if norm_delta_week is not None else None,
                "ratio": round(ratio, 4) if ratio is not None else None,
                "complete": sid != max_sid,
            }
        )
    return out


def compute_summary(sessions: list[dict]) -> dict:
    if not sessions:
        return {
            "avg_norm_week": None,
            "avg_delta_week": None,
            "heavy_per_week": None,
            "current_ratio": None,
            "current_norm_week": None,
            "sample_count": 0,
        }
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    # Include the current (incomplete) session too — its ratio is already
    # meaningful once peak_sess >= 10%. Only skip sessions with too little
    # signal (peak < 10%) via the norm_delta_week == None filter.
    recent = [
        s for s in sessions
        if s.get("norm_delta_week") is not None
        and s.get("delta_week", 0) > 0
        and datetime.fromisoformat(s["start_t"]) >= cutoff
    ]
    if recent:
        avg_norm = sum(s["norm_delta_week"] for s in recent) / len(recent)
        avg_raw = sum(s["delta_week"] for s in recent) / len(recent)
        heavy_per_week = round(100 / avg_norm) if avg_norm > 0 else None
    else:
        avg_norm = None
        avg_raw = None
        heavy_per_week = None
    current = next((s for s in sessions if not s.get("complete")), None)
    current_ratio = current.get("ratio") if current else None
    current_norm = current.get("norm_delta_week") if current else None
    return {
        "avg_norm_week": round(avg_norm, 1) if avg_norm is not None else None,
        "avg_delta_week": round(avg_raw, 1) if avg_raw is not None else None,
        "heavy_per_week": heavy_per_week,
        "current_ratio": current_ratio,
        "current_norm_week": current_norm,
        "sample_count": len(recent),
    }


def rebuild_sessions(history_path: Path, sessions_path: Path) -> None:
    rows: list[dict] = []
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    sessions = detect_sessions(rows)
    summary = compute_summary(sessions)
    payload = {"sessions": sessions, "summary": summary}
    sessions_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")


def get_usage_raw() -> str:
    proc = PtyProcess.spawn(["claude"], dimensions=(ROWS, COLS))
    buf: list[str] = []

    def reader():
        try:
            while True:
                chunk = proc.read(1024)
                if not chunk:
                    break
                buf.append(chunk)
        except EOFError:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        time.sleep(8.0)
        proc.write("/usage")
        time.sleep(1.5)
        proc.write("\r")
        time.sleep(10.0)
    finally:
        try:
            proc.terminate(force=True)
        except Exception:
            pass

    return "".join(buf)


def build_and_publish(data: dict) -> None:
    from render_html import write_html

    html_path = ROOT / "index.html"
    write_html(html_path, data)
    ts = data["updated_utc"]
    os.system(
        f'cd /d "{ROOT}" && git add index.html {HISTORY_FILE} {SESSIONS_FILE} '
        f'&& git commit -m "update {ts}" -q && git push -q'
    )


def run_once() -> None:
    raw = get_usage_raw()
    lines = render(raw)
    block = extract_usage_block(lines)
    (ROOT / "usage.txt").write_text("\n".join(block) + "\n", encoding="utf-8")
    data = parse_usage(block)
    history_path = ROOT / HISTORY_FILE
    sessions_path = ROOT / SESSIONS_FILE
    append_sample(data, history_path)
    prune_history(history_path)
    rebuild_sessions(history_path, sessions_path)
    build_and_publish(data)


def sleep_interruptible(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop.is_set():
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(SLEEP_TICK, remaining))


def main() -> int:
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except (AttributeError, ValueError):
        pass

    print(f"[{datetime.now().isoformat(timespec='seconds')}] claude-status started, interval={INTERVAL_SECONDS}s")
    while not _stop.is_set():
        started = time.monotonic()
        try:
            run_once()
            print(f"[{datetime.now().isoformat(timespec='seconds')}] updated ({time.monotonic()-started:.1f}s)")
        except Exception as e:
            print(f"[{datetime.now().isoformat(timespec='seconds')}] ERROR: {e!r}", file=sys.stderr)
        if _stop.is_set():
            break
        sleep_interruptible(INTERVAL_SECONDS)
    print("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
