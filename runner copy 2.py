import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from browser import launch_or_attach
from config import OUT_DIR
from deed import STATUS_NAME, download_next, process_volume, reload_list
from extract import ExtractionError, ensure_extracted
from fill import SubmitUnconfirmed, fill_all, submit_deed
from login import login
from navigation import NavigationError, find_worklist_row, return_to_list
from Returned import load_extracted
from session import SessionLost, StopRun, session_lost
from state import State, volume_signature
from util import log, on_dialog, set_context
from worklist import prepare_worklist


def _flag(name, default):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


AUTO_SUBMIT = _flag("AUTO_SUBMIT", "false")
ALLOW_REVIEW_SUBMIT = _flag("ALLOW_REVIEW_SUBMIT", "false")  # submit even when fields were matched loosely
MAX_DEEDS = int(os.getenv("MAX_DEEDS", "1"))  # 0 means no limit
MAX_VOLUMES = int(os.getenv("MAX_VOLUMES", "0"))  # 0 means no limit
MAX_FAILURES = int(os.getenv("MAX_FAILURES", "3"))
MAX_RELOGINS = int(os.getenv("MAX_RELOGINS", "5"))
LIST_ORDER = tuple(s for s in (x.strip().upper() for x in os.getenv("LIST_ORDER", "P,I").split(",")) if s in ("P", "I"))
if not LIST_ORDER:
    raise SystemExit("LIST_ORDER must contain P and/or I, for example 'P,I' or 'I'")


class Counters:
    def __init__(self):
        self.deeds = 0
        self.volumes = 0
        self.failures_in_row = 0

    def deed_cap_reached(self):
        return bool(MAX_DEEDS) and self.deeds >= MAX_DEEDS

    def note(self, failed):
        self.failures_in_row = self.failures_in_row + 1 if failed else 0
        if self.failures_in_row >= MAX_FAILURES:
            raise StopRun(f"{self.failures_in_row} deeds failed in a row, stopping")


def _screenshot(page):
    try:
        path = OUT_DIR / "debug.png"
        path.write_bytes(page.screenshot())
        log(f"Screenshot saved: {path}")
    except Exception:
        pass


def _on_search_index(page):
    try:
        return page.locator("#deed_status").is_visible() and not page.locator("#addVolumeBtn").is_visible()
    except Exception:
        return False


def _reopen(page, record):
    """Back to the list, find the volume again and open its Search Index page."""
    log("Reopening the volume from the list")
    return_to_list(page)
    row, cells = find_worklist_row(page, record)
    if row is None:
        raise NavigationError("volume was not found again after reopening")
    out = process_volume(page, row)
    if out.kind != "ready":
        raise NavigationError(out.reason)
    return cells


def _pdf_pages(folder, meta):
    from pypdf import PdfReader

    pdf = Path(folder) / f"{meta['volume_id']}_{meta['doct_no']}_{meta['presentation_year']}.pdf"
    try:
        count = len(PdfReader(str(pdf)).pages)
    except Exception as exc:
        raise ValueError(f"cannot read PDF page count: {exc!r}") from exc
    if count < 1:
        raise ValueError("PDF has no pages")
    return count


def _resolve_pages(state, sig, deed, folder, meta, values):
    """Use Gemini's pages unless they overlap a range already used in this volume."""
    start, end = int(values["page_from"]), int(values["page_to"])
    used = state.used_page_ranges(sig, exclude=deed)
    log(f"Pages: deed {deed} read as {start}-{end}; ranges already used in this volume: {used}")
    if not any(start <= b and a <= end for a, b in used):
        log(f"Pages: no overlap, using {start}-{end}")
        return values, None
    new_start = max(b for _, b in used) + 1
    new_end = new_start + _pdf_pages(folder, meta) - 1
    note = f"pages shifted {start}-{end} -> {new_start}-{new_end} (overlap)"
    log(f"Pages: {note}")
    return {**values, "page_from": str(new_start), "page_to": str(new_end)}, note


def _summary(deed, list_status, pages, notes, outcome, started):
    log(
        f"Deed summary: deed={deed} list={STATUS_NAME[list_status]} pages={pages} "
        f"review_notes={notes or 'none'} outcome={outcome} seconds={time.time() - started:.1f}"
    )


def _handle_deed(page, state, sig, out, list_status):
    """Extract, fill and submit one downloaded deed. Returns True when it counts as a failure."""
    deed, folder = out.deed, out.folder
    started = time.time()
    pages, notes, outcome, failed = None, [], "unknown", False
    try:
        try:
            meta, values = load_extracted(folder)
            log(f"Extraction: using existing JSON for deed {deed}")
        except FileNotFoundError:
            log(f"Extraction: no JSON for deed {deed}, running the processor")
            t = time.time()
            ensure_extracted(folder)
            log(f"Extraction: processor finished in {time.time() - t:.1f}s")
            meta, values = load_extracted(folder)

        values, shift_note = _resolve_pages(state, sig, deed, folder, meta, values)
        pages = f"{values['page_from']}-{values['page_to']}"
        state.set_pages(sig, deed, values["page_from"], values["page_to"])

        t = time.time()
        log(f"Fill: starting deed {deed} (clear existing rows: {list_status == 'I'})")
        notes = fill_all(page, values, clear_existing=(list_status == "I"))
        log(f"Fill: finished in {time.time() - t:.1f}s, review notes: {notes or 'none'}")
        extra = [shift_note] if shift_note else []

        if notes and not ALLOW_REVIEW_SUBMIT:
            log(f"Deed {deed} filled but NOT submitted, needs review: {notes}")
            state.mark(sig, deed, "needs_review", "review notes, not submitted: " + "; ".join(extra + notes)[:280])
            outcome = "needs_review (review notes, not submitted)"
        elif AUTO_SUBMIT:
            t = time.time()
            log(f"Submit: starting deed {deed}")
            submit_deed(page)
            log(f"Submit: finished in {time.time() - t:.1f}s")
            state.mark(sig, deed, "submitted", "; ".join(extra + notes)[:280] or None)
            outcome = "submitted"
        else:
            input("Deed filled and saved. Check the page, click Submit yourself if it is correct, then press Enter... ")
            state.mark(sig, deed, "filled_manual", "; ".join(extra)[:280] or None)
            outcome = "filled_manual"

    except SubmitUnconfirmed as exc:
        log(f"Deed {deed}: submit unconfirmed, parked. Check the portal lists. {exc}")
        _screenshot(page)
        state.mark(sig, deed, "needs_review", "SUBMIT UNCONFIRMED, check portal lists: " + str(exc)[:240])
        outcome, failed = "needs_review (SUBMIT UNCONFIRMED)", True
    except (StopRun, SessionLost):
        outcome = "interrupted (stop or session lost)"
        raise
    except (ExtractionError, ValueError) as exc:
        log(f"Deed {deed} needs review: {exc}")
        state.mark(sig, deed, "needs_review", str(exc)[:300])
        outcome = f"needs_review ({str(exc)[:80]})"
    except Exception as exc:
        if session_lost(page):
            outcome = "interrupted (session lost)"
            raise SessionLost() from exc
        log(f"Deed {deed} failed: {exc!r}")
        _screenshot(page)
        state.mark(sig, deed, "needs_review", "fill failed: " + repr(exc)[:280])
        outcome, failed = f"needs_review (fill failed: {repr(exc)[:80]})", True
    finally:
        _summary(deed, list_status, pages, notes, outcome, started)
    return failed


def _park(state, sig, detail):
    log(f"Volume parked as needs_review: {detail}")
    state.mark_volume(sig, "needs_review", detail[:300])


def run_volume(page, state, record, sig, counters):
    row, cells = find_worklist_row(page, record)
    if row is None:
        return _park(state, sig, "no matching row in the portal list")
    out = process_volume(page, row)
    if out.kind != "ready":
        return _park(state, sig, out.reason)
    state.mark_volume(sig, "in_progress")

    for status in LIST_ORDER:
        while True:
            set_context(deed="")
            if counters.deed_cap_reached():
                log("Deed limit reached; volume paused.")
                state.mark_volume(sig, "paused", "deed limit reached")
                return
            out = download_next(page, cells, state, sig, status)

            if out.kind == "empty":
                log(f"{STATUS_NAME[status]} list is empty, moving on")
                break
            if out.kind == "all_tried":
                return _park(state, sig, f"{STATUS_NAME[status]} deeds remain but were already tried or parked: {out.labels}")
            if out.kind == "failed":
                if out.deed is None:
                    return _park(state, sig, f"{STATUS_NAME[status]} list: {out.reason}")
                set_context(deed=out.deed)
                counters.deeds += 1
                counters.note(True)
                cells = _reopen(page, record)
                continue

            set_context(deed=out.deed)
            failed = _handle_deed(page, state, sig, out, status)
            counters.deeds += 1
            counters.note(failed)
            if not _on_search_index(page):
                cells = _reopen(page, record)

    set_context(deed="")
    if set(LIST_ORDER) != {"P", "I"}:
        log(f"Partial run (LIST_ORDER={','.join(LIST_ORDER)}): final check skipped, volume NOT marked completed")
        state.mark_volume(sig, "paused", f"partial run, LIST_ORDER={','.join(LIST_ORDER)}")
        return
    log("Final check: reloading In-Progress and Pending lists")
    in_progress = reload_list(page, "I")
    pending = reload_list(page, "P")
    if in_progress.empty and pending.empty:
        state.mark_volume_done(sig)
        log("Volume completed: Pending and In-Progress lists are both empty.")
    else:
        _park(
            state, sig,
            f"final check not empty: In-Progress ok={in_progress.ok} deeds={in_progress.deeds}; "
            f"Pending ok={pending.ok} deeds={pending.deeds}",
        )


def run():
    OUT_DIR.mkdir(exist_ok=True)
    state = State(OUT_DIR / "state.json")
    records = prepare_worklist()
    counters = Counters()
    relogins = 0
    log(f"Run settings: AUTO_SUBMIT={AUTO_SUBMIT} ALLOW_REVIEW_SUBMIT={ALLOW_REVIEW_SUBMIT} "
        f"MAX_DEEDS={MAX_DEEDS} MAX_VOLUMES={MAX_VOLUMES} MAX_FAILURES={MAX_FAILURES} "
        f"LIST_ORDER={','.join(LIST_ORDER)}")

    with sync_playwright() as p:
        _, page = launch_or_attach(p)
        page.on("dialog", on_dialog)
        login(page)
        try:
            for record in records:
                sig = volume_signature(
                    record["registration_office"], record["volume_year"], record["volume_no"], record["book_no"]
                )
                if state.should_skip_volume(sig):
                    continue
                if MAX_VOLUMES and counters.volumes >= MAX_VOLUMES:
                    log("Volume limit reached.")
                    break
                if counters.deed_cap_reached():
                    log("Deed limit reached.")
                    break
                counters.volumes += 1
                set_context(volume=sig, deed="")
                log(f"=== Volume {sig} ===")

                while True:
                    try:
                        run_volume(page, state, record, sig, counters)
                        break
                    except StopRun:
                        raise
                    except SessionLost:
                        pass
                    except Exception as exc:
                        if not session_lost(page):
                            log(f"Volume failed: {exc!r}")
                            _screenshot(page)
                            _park(state, sig, "volume failed: " + repr(exc)[:280])
                            try:
                                return_to_list(page)
                            except Exception:
                                pass
                            break
                    relogins += 1
                    if relogins > MAX_RELOGINS:
                        raise StopRun(f"Logged out {relogins} times, stopping")
                    log("Session lost. Logging in again and retrying this volume.")
                    state.release_in_progress()
                    login(page, force=True)
                set_context(volume="", deed="")
        finally:
            log(state.summary())