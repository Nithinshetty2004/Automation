import csv
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

from util import log

MAX_DEED_ATTEMPTS = int(os.getenv("MAX_DEED_ATTEMPTS", "3"))
RETRY_NEEDS_REVIEW = os.getenv("RETRY_NEEDS_REVIEW", "false").strip().lower() == "true"

VOLUME_STATUSES = {"in_progress", "completed", "needs_review", "paused"}
DONE_DEED_STATUSES = {"submitted", "done", "completed", "saved"}
REPORT_COLUMNS = ["record_type", "volume_signature", "deed_no", "status", "detail", "updated_at"]


def volume_signature(office, year, volume, book):
    """office|year|volume|book, lowercased, trailing '(4700)' code removed."""
    office = re.sub(r"\s*\(\d+\)\s*$", "", str(office or ""))
    office = re.sub(r"\s+", " ", office).strip().casefold()
    return "|".join([office, str(year).strip(), str(volume).strip(), str(book).strip()])


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class State:
    """Remembers which deeds and volumes were handled so the loop never repeats one."""

    def __init__(self, path, report_path=None):
        self.path = Path(path)
        self.report_path = Path(report_path) if report_path else Path("returned_data") / "processing_report.csv"
        self.data = {"deeds": {}, "volumes": {}, "volumes_done": []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        for key in ("deeds", "volumes"):
            self.data.setdefault(key, {})
        self.data.setdefault("volumes_done", [])
        self.data.setdefault("pages", {})
        self._attempted = set()

    # ---- deeds -------------------------------------------------------
    @staticmethod
    def _key(sig, deed_no):
        return f"{sig}#{deed_no}"

    def tried(self, sig, deed_no):
        """True only for a repeat within this run, a finished deed, or too many attempts."""
        key = self._key(sig, deed_no)
        if key in self._attempted:
            return True
        entry = self.data["deeds"].get(key)
        if not entry:
            return False
        if entry["status"] in DONE_DEED_STATUSES:
            return True
        return entry.get("attempts", 0) >= MAX_DEED_ATTEMPTS

    def mark(self, sig, deed_no, status, detail=None):
        key = self._key(sig, deed_no)
        self._attempted.add(key)
        old = self.data["deeds"].get(key, {})
        attempts = old.get("attempts", 0) + (1 if status == "in_progress" else 0)
        self.data["deeds"][key] = {
            "status": status,
            "detail": detail,
            "attempts": attempts,
            "at": _now(),
        }
        self._save()

    def release_in_progress(self):
        """After a session loss: flag interrupted deeds and let them be retried in this run."""
        for key, entry in self.data["deeds"].items():
            if entry["status"] == "in_progress":
                entry["status"] = "interrupted_needs_check"
                entry["at"] = _now()
                self._attempted.discard(key)
        self._save()

    # ---- volumes -----------------------------------------------------
    def mark_volume(self, sig, status, detail=None):
        if status not in VOLUME_STATUSES:
            raise ValueError(f"Unknown volume status: {status}")
        self.data["volumes"][sig] = {"status": status, "detail": detail, "at": _now()}
        self._save()

    def volume_status(self, sig):
        return self.data["volumes"].get(sig, {}).get("status")

    def should_skip_volume(self, sig):
        status = self.volume_status(sig)
        if status == "completed":
            return True
        if status == "needs_review":
            return not RETRY_NEEDS_REVIEW
        return False

    def mark_volume_done(self, sig):
        self.mark_volume(sig, "completed")

    def done_volumes(self):
        done = {s for s, v in self.data["volumes"].items() if v["status"] == "completed"}
        return done | set(self.data["volumes_done"])

    def used_page_ranges(self, sig, exclude=None):
        pages = self.data["pages"].get(sig, {})
        return [(int(a), int(b)) for deed, (a, b) in pages.items() if deed != str(exclude)]

    def set_pages(self, sig, deed, start, end):
        self.data["pages"].setdefault(sig, {})[str(deed)] = [int(start), int(end)]
        self._save()

    def shift_pages(self, sig, deed, rejected_from, rejected_to, span):
        """The portal rejected this range. Remember it as used, then return the next free range."""
        pages = self.data["pages"].setdefault(sig, {})
        pages[f"{deed}:rejected:{rejected_from}-{rejected_to}"] = [int(rejected_from), int(rejected_to)]
        used = self.used_page_ranges(sig, exclude=deed)
        start = max(b for _, b in used) + 1
        end = start + int(span) - 1
        pages[str(deed)] = [start, end]
        self._save()
        return start, end

    # ---- reporting / saving -----------------------------------------
    def summary(self):
        deeds = Counter(e["status"] for e in self.data["deeds"].values())
        vols = Counter(v["status"] for v in self.data["volumes"].values())
        fmt = lambda c: ", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "none"
        return f"Deed outcomes: {fmt(deeds)} | Volume outcomes: {fmt(vols)}"

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self._write_report()

    def _write_report(self):
        rows = []
        for sig, v in self.data["volumes"].items():
            rows.append(["volume", sig, "", v["status"], v.get("detail") or "", v["at"]])
        for key, e in self.data["deeds"].items():
            sig, _, deed_no = key.rpartition("#")
            rows.append(["deed", sig, deed_no, e["status"], e.get("detail") or "", e.get("at", "")])
        try:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.report_path.with_suffix(".tmp")
            with tmp.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(REPORT_COLUMNS)
                writer.writerows(rows)
            os.replace(tmp, self.report_path)
        except OSError as exc:
            log(f"WARNING: could not write {self.report_path} (open in Excel?): {exc}")