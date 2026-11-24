import argparse
import csv
import json
import os
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from browser import launch_or_attach
from deed import STATUS_NAME, download_next, process_volume
from login import login
from navigation import NavigationError, find_worklist_row, return_to_list
from session import SessionLost, StopRun, session_lost
from state import volume_signature
from util import log, on_dialog, set_context

RETURNED_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
DONE_FILE = RETURNED_DIR / "download_only_done.json"
REPORT_FILE = RETURNED_DIR / "download_only_report.csv"
MAX_RELOGINS = int(os.getenv("MAX_RELOGINS", "5"))


class Tracker:
    """Stands in for State. Remembers downloaded deeds in its own file, never touches state.json."""

    def __init__(self, path):
        self.path = Path(path)
        self.done = set()
        self.attempted = set()  # tried in this run (success or failure), so nothing loops
        if self.path.exists():
            self.done = set(json.loads(self.path.read_text(encoding="utf-8")))

    @staticmethod
    def _key(sig, deed):
        return f"{sig}#{deed}"

    def tried(self, sig, deed):
        key = self._key(sig, deed)
        return key in self.done or key in self.attempted

    def mark(self, sig, deed, status, detail=None, *args, **kwargs):
        self.attempted.add(self._key(sig, deed))

    def finish(self, sig, deed):
        self.done.add(self._key(sig, deed))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self.done), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None  # any other State method is a no-op here


def report(sig, list_name, deed, result, detail=""):
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        new = not REPORT_FILE.exists() or REPORT_FILE.stat().st_size == 0
        with REPORT_FILE.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "volume", "list", "deed", "result", "detail"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), sig, list_name, deed, result, detail])
    except OSError as exc:
        log(f"WARNING: could not write {REPORT_FILE}: {exc}")


def open_volume(page, record):
    """Find the volume and open its Search Index page. Retries portal trouble, stops if it persists."""
    last = None
    for attempt in range(1, 4):
        try:
            row, cells = find_worklist_row(page, record)
            if row is None:
                raise NavigationError("no matching row in the portal list")
            out = process_volume(page, row)
            if out.kind != "ready":
                raise NavigationError(out.reason)
            return cells
        except (StopRun, SessionLost):
            raise
        except Exception as exc:
            if session_lost(page):
                raise SessionLost() from exc
            last = exc
            log(f"Opening the volume failed (attempt {attempt}/3): {exc!r}")
            time.sleep(5 * attempt)
            try:
                return_to_list(page)
            except Exception:
                pass
    raise StopRun(f"Could not open the volume: {last!r}")


def ensure_search_index(page, record, cells):
    """Back to Search Index after a deed: use Back while the deed form is open, else reopen the volume."""
    try:
        if page.locator("#addVolumeBtn").is_visible():
            back = page.locator("#backbtn:visible")
            (back.first if back.count() else page.locator("button:visible", has_text="Back").first).click()
        page.locator("#deed_status").wait_for(state="visible", timeout=25_000)
        page.locator("#addVolumeBtn").wait_for(state="hidden", timeout=10_000)
        return cells
    except PWTimeout:
        log("Search Index not reached; reopening the volume from the list")
        return_to_list(page)
        return open_volume(page, record)


def has_pdf(folder):
    return any(p for p in Path(folder).glob("*.pdf") if "_unrepaired" not in p.name)


def work(page, tracker, record, sig, lists, limit, stats):
    cells = open_volume(page, record)
    for status in lists:
        list_failures = 0
        while True:
            if limit and stats["downloaded"] >= limit:
                log(f"Limit of {limit} download(s) reached.")
                return
            set_context(deed="")
            out = download_next(page, cells, tracker, sig, status)

            if out.kind == "empty":
                log(f"{STATUS_NAME[status]} list: no more deeds")
                break
            if out.kind == "all_tried":
                log(f"{STATUS_NAME[status]} list: remaining deeds were already downloaded or failed in this run: {out.labels}")
                break
            if out.kind == "failed":
                if out.deed is None:
                    list_failures += 1
                    report(sig, STATUS_NAME[status], "", "list_failed", str(out.reason)[:200])
                    if list_failures >= 2:
                        raise StopRun(f"{STATUS_NAME[status]} list would not load: {out.reason}")
                    log(f"{STATUS_NAME[status]} list failed ({out.reason}); reopening the volume and trying again")
                    return_to_list(page)
                    cells = open_volume(page, record)
                    continue
                set_context(deed=out.deed)
                stats["failed"].append(out.deed)
                report(sig, STATUS_NAME[status], out.deed, "download_failed", str(out.reason)[:200])
                log(f"Deed {out.deed}: download failed ({out.reason}); continuing")
                return_to_list(page)
                cells = open_volume(page, record)
                continue

            set_context(deed=out.deed)
            if not has_pdf(out.folder):
                stats["failed"].append(out.deed)
                report(sig, STATUS_NAME[status], out.deed, "no_pdf", str(out.folder))
                log(f"Deed {out.deed}: no PDF in {out.folder} (see stream_raw.bin there); NOT counted as downloaded")
            else:
                tracker.finish(sig, out.deed)
                stats["downloaded"] += 1
                report(sig, STATUS_NAME[status], out.deed, "downloaded", str(out.folder))
                log(f"Deed {out.deed}: downloaded to {out.folder}")
            cells = ensure_search_index(page, record, cells)


def main():
    ap = argparse.ArgumentParser(description="Download the PDF of every deed in one volume. Fills and submits nothing.")
    ap.add_argument("--office", required=True)
    ap.add_argument("--year", required=True)
    ap.add_argument("--volume", required=True)
    ap.add_argument("--book", required=True)
    ap.add_argument("--lists", default="P,I", help="P, I or P,I (default P,I)")
    ap.add_argument("--max", type=int, default=0, help="stop after this many downloads (0 = no limit)")
    args = ap.parse_args()

    lists = [s for s in (x.strip().upper() for x in args.lists.split(",")) if s in ("P", "I")]
    if not lists:
        raise SystemExit("--lists must contain P and/or I")
    record = {
        "registration_office": args.office,
        "volume_year": str(args.year),
        "volume_no": str(args.volume),
        "book_no": str(args.book),
    }
    sig = volume_signature(args.office, args.year, args.volume, args.book)
    tracker = Tracker(DONE_FILE)
    stats = {"downloaded": 0, "failed": []}
    log(f"Download-only run for {sig}: lists={','.join(lists)} max={args.max or 'no limit'}")

    with sync_playwright() as p:
        _, page = launch_or_attach(p)
        page.on("dialog", on_dialog)
        login(page)
        set_context(volume=sig, deed="")
        relogins = 0
        try:
            while True:
                try:
                    work(page, tracker, record, sig, lists, args.max, stats)
                    break
                except SessionLost:
                    relogins += 1
                    if relogins > MAX_RELOGINS:
                        raise StopRun(f"Logged out {relogins} times, stopping")
                    log("Session lost. Logging in again and resuming.")
                    login(page, force=True)
        finally:
            log(f"Finished: downloaded={stats['downloaded']} failed={stats['failed'] or 'none'}. "
                f"Report: {REPORT_FILE}")


if __name__ == "__main__":
    main()