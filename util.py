import csv
import os
import time
from pathlib import Path

LOG_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
RUN_ID = time.strftime("%Y%m%d_%H%M%S")
COLUMNS = ["timestamp", "run_id", "volume", "deed", "step", "message"]

_context = {"volume": "", "deed": ""}
_log_path = LOG_DIR / "run_log.csv"


def set_context(volume=None, deed=None):
    """Tag every following log line. Pass '' to clear a value, None to leave it unchanged."""
    if volume is not None:
        _context["volume"] = volume
    if deed is not None:
        _context["deed"] = str(deed)


def _step(msg):
    head = msg.split(":", 1)[0].strip()
    if ":" in msg and 0 < len(head) <= 40:
        return head
    return " ".join(msg.split()[:3])


def _write_csv(row):
    global _log_path
    for _ in range(2):
        try:
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            new = not _log_path.exists() or _log_path.stat().st_size == 0
            with _log_path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                writer = csv.writer(f)
                if new:
                    writer.writerow(COLUMNS)
                writer.writerow(row)
            return
        except PermissionError:
            _log_path = LOG_DIR / f"run_log_{RUN_ID}.csv"
            print(f"[{time.strftime('%H:%M:%S')}] run_log.csv is locked (open in Excel?). Logging to {_log_path}", flush=True)
        except OSError as exc:
            print(f"[{time.strftime('%H:%M:%S')}] could not write the CSV log: {exc}", flush=True)
            return


def log(msg):
    msg = str(msg)
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    _write_csv([
        time.strftime("%Y-%m-%d %H:%M:%S"), RUN_ID,
        _context["volume"], _context["deed"], _step(msg), msg,
    ])


def on_dialog(dialog):
    log(f"Portal alert: {dialog.message}")
    dialog.accept()