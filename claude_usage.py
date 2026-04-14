import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pyte
from winpty import PtyProcess

ROOT = Path(__file__).resolve().parent
COLS, ROWS = 120, 40
INTERVAL_SECONDS = 10 * 60
SLEEP_TICK = 1.0

_stop = threading.Event()


def _handle_sigint(signum, frame):
    _stop.set()


def render(raw: str) -> list[str]:
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    stream.feed(raw)
    return [line.rstrip() for line in screen.display]


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
            continue
        if stripped.lower().startswith("resets"):
            current["reset"] = stripped
    bars = [s for s in sections if not s.get("_extra")]
    return {
        "sections": bars,
        "extra": extra,
        "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


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
    os.system(f'cd /d "{ROOT}" && git add index.html && git commit -m "update {ts}" -q && git push -q')


def run_once() -> None:
    raw = get_usage_raw()
    lines = render(raw)
    block = extract_usage_block(lines)
    (ROOT / "usage.txt").write_text("\n".join(block) + "\n", encoding="utf-8")
    data = parse_usage(block)
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
