import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout

from config import OUT_DIR
from session import SessionLost, session_lost
from util import log


def _extract_pdf(resp):
    body = resp.body()
    if body[:4] == b"%PDF":
        return body, body
    try:
        data = resp.json()
    except Exception:
        return None, body
    stack = [data]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, list):
            stack.extend(v)
        elif isinstance(v, str) and len(v) > 100:
            try:
                raw = base64.b64decode(v.split(",")[-1])
                if raw[:4] == b"%PDF":
                    return raw, body
            except Exception:
                pass
    return None, body


def _recover_mojibake_pdf(pdf_bytes: bytes) -> bytes:
    """Reverse a UTF-8 encoding of Windows-1252-decoded PDF bytes."""
    text = pdf_bytes.decode("utf-8", errors="strict")
    undefined_cp1252 = {"\u0081": 0x81, "\u008d": 0x8D, "\u008f": 0x8F, "\u0090": 0x90, "\u009d": 0x9D}
    recovered = bytearray()
    for position, char in enumerate(text):
        if char in undefined_cp1252:
            recovered.append(undefined_cp1252[char])
            continue
        try:
            recovered.extend(char.encode("cp1252"))
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"Cannot recover PDF character U+{ord(char):04X} at text position {position}"
            ) from exc
    result = bytes(recovered)
    if not result.startswith(b"%PDF-"):
        raise ValueError("Recovered data does not have a PDF header")
    if b"%%EOF" not in result[-2048:]:
        raise ValueError("Recovered data has no PDF EOF marker near the end")
    return result


def _repair_pdf_if_needed(pdf_bytes: bytes) -> bytes:
    corrupted_header_marker = b"\xc3\xa2\xc3\xa3\xc3\x8f\xc3\x93"
    if corrupted_header_marker not in pdf_bytes[:96]:
        return pdf_bytes
    repaired = _recover_mojibake_pdf(pdf_bytes)
    log(f"Recovered PDF byte encoding: {len(pdf_bytes):,} -> {len(repaired):,} bytes")
    return repaired


FETCH_ATTEMPTS = 3
FETCH_TIMEOUT_MS = 45_000
LIST_TIMEOUT_MS = 20_000
PROCESS_TIMEOUT_MS = 20_000
ITEM_TIMEOUT_MS = 10_000
YEAR_TIMEOUT_MS = 10_000
SELECT_ATTEMPTS = 2

# "/deed/list/" does not match "/deed-completed/list/", "/deed-progress/list/" or "/pres-year/list/deed".
LIST_URL_PART = {"P": "/deed/list/", "I": "/deed-progress/list/"}
OTHER_STATUS = {"P": "I", "I": "P"}
STATUS_NAME = {"P": "Pending", "I": "In-Progress"}


@dataclass
class ListResult:
    ok: bool
    deeds: list = field(default_factory=list)
    reason: str = ""

    @property
    def empty(self):
        """True only when the list loaded successfully and holds no deeds."""
        return self.ok and not self.deeds


@dataclass
class Outcome:
    kind: str  # ready | deed | empty | all_tried | failed
    deed: str = None
    folder: Path = None
    reason: str = ""
    labels: list = field(default_factory=list)


def _fetch_with_retry(page):
    """Click Fetch Details and wait for both responses, retrying on timeouts."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        started = time.time()
        try:
            with page.expect_response(
                lambda r: r.url.endswith("/metadata/details"), timeout=FETCH_TIMEOUT_MS
            ) as details, page.expect_response(
                lambda r: "/file/v1/read/stream/" in r.url, timeout=FETCH_TIMEOUT_MS
            ) as stream:
                page.get_by_text("Fetch Details").first.click()
            log(f"Fetch Details: both responses received on attempt {attempt} in {time.time() - started:.1f}s")
            return details.value, stream.value
        except PWTimeout:
            if session_lost(page):
                raise SessionLost() from None
            log(
                f"Fetch Details attempt {attempt}/{FETCH_ATTEMPTS} timed out "
                f"(details received: {details.is_done()}, PDF stream received: {stream.is_done()})"
            )
            if attempt == FETCH_ATTEMPTS:
                raise
            page.wait_for_timeout(2000)


def _select_deed(page, item, label):
    """Click the deed in the dropdown and return the pres-year response. Fetch Details needs it."""
    for attempt in range(1, SELECT_ATTEMPTS + 1):
        started = time.time()
        try:
            if attempt > 1:
                page.locator("#deed_no").click(timeout=ITEM_TIMEOUT_MS)
                item.wait_for(state="visible", timeout=ITEM_TIMEOUT_MS)
            with page.expect_response(lambda r: "/pres-year/list/deed" in r.url, timeout=YEAR_TIMEOUT_MS) as info:
                item.click()
            log(f"Deed selection: pres-year response on attempt {attempt} in {time.time() - started:.1f}s")
            return info.value
        except PWTimeout:
            if session_lost(page):
                raise SessionLost() from None
            shown = page.locator("#deed_no").input_value().strip()
            log(
                f"Deed selection attempt {attempt}/{SELECT_ATTEMPTS}: no pres-year response in "
                f"{YEAR_TIMEOUT_MS // 1000}s (#deed_no shows {shown!r}, wanted {label!r})"
            )
    raise RuntimeError(f"Deed {label}: the portal never returned the presentation year")


def _download_selected(page, item, row_cells, list_status):
    started = time.time()
    label = item.inner_text().strip()
    year_resp = _select_deed(page, item, label)
    year_req = year_resp.request.post_data_json
    log(f"Deed selected: {year_req}")
    try:
        log(f"Presentation years offered: {year_resp.json().get('data')}")
    except Exception:
        log("pres-year response was not readable JSON")

    details_resp, stream_resp = _fetch_with_retry(page)

    details_req = details_resp.request.post_data_json
    stream_req = stream_resp.request.post_data_json
    volume_id = year_req["volume_id"]
    deed_no = details_req["doct_no"]
    year = details_req["presentation_year"]
    if not str(year or "").strip():
        raise RuntimeError(f"Fetch Details was sent without a presentation year: {details_req}")

    folder = OUT_DIR / f"deed_{deed_no}_{year}_vol{volume_id}"
    folder.mkdir(parents=True, exist_ok=True)

    pdf, raw = _extract_pdf(stream_resp)
    if pdf:
        pdf_path = folder / f"{volume_id}_{deed_no}_{year}.pdf"
        try:
            pdf = _repair_pdf_if_needed(pdf)
        except (UnicodeDecodeError, ValueError) as exc:
            original_path = folder / f"{volume_id}_{deed_no}_{year}_unrepaired.pdf"
            original_path.write_bytes(pdf)
            log(f"WARNING: PDF encoding recovery failed: {exc}. Original bytes saved to {original_path}")
            raise
        pdf_path.write_bytes(pdf)
        log(f"PDF saved: {pdf_path} ({len(pdf)} bytes)")
    else:
        (folder / "stream_raw.bin").write_bytes(raw)
        log("WARNING: stream response was not a PDF or base64 PDF. Raw body saved as stream_raw.bin")

    try:
        (folder / "details.json").write_text(
            json.dumps(details_resp.json(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        log("details response was not JSON, skipped details.json")

    meta = {
        **details_req,
        "volume_id": volume_id,
        "file_path": stream_req.get("file_path"),
        "module_id": stream_req.get("module_id"),
        "list_row": row_cells,
        "list_status": list_status,
        "pdf_saved": bool(pdf),
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Download complete: {folder} (select + fetch + save took {time.time() - started:.1f}s)")
    return folder


def _list_matcher(status):
    part = LIST_URL_PART[status]
    return lambda r: r.request.method == "POST" and part in r.url


def _change_status(page, status):
    """Select a Deed Status and return the matching list response (raises PWTimeout)."""
    with page.expect_response(_list_matcher(status), timeout=LIST_TIMEOUT_MS) as info:
        page.locator("#deed_status").select_option(value=status)
    return info.value


def _parse_list(resp):
    if not resp.ok:
        return ListResult(False, reason=f"list request returned HTTP {resp.status}")
    try:
        body = resp.json()
    except Exception:
        return ListResult(False, reason="list response was not JSON")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("status") is not True or not isinstance(data, list):
        return ListResult(False, reason=f"unexpected list response: {str(body)[:200]}")
    deeds = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("doct_no") is None:
            return ListResult(False, reason=f"list entry without doct_no: {str(entry)[:200]}")
        label = str(entry["doct_no"]).strip()
        if label in deeds:
            log(f"WARNING: deed number {label} appears more than once in the list")
            continue
        deeds.append(label)
    if not deeds and body.get("count") not in (0, "0"):
        return ListResult(False, reason=f"empty data but count={body.get('count')!r}")
    deeds.sort(key=lambda d: (0, int(d), "") if d.isdigit() else (1, 0, d))
    return ListResult(True, deeds)


def reload_list(page, status, attempts=2):
    """Force-load the Pending ('P') or In-Progress ('I') list and report what the portal returned.

    A timeout or bad response is a failure, never an empty list.
    """
    started = time.time()
    select = page.locator("#deed_status")
    reason = f"{STATUS_NAME[status]} list did not load (timeout)"
    resp = None

    for attempt in range(1, attempts + 1):
        try:
            select.wait_for(state="visible", timeout=PROCESS_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)  # let the page finish its own startup
            except PWTimeout:
                pass
            if select.input_value() == status:
                # onchange only fires on a change, so move away first
                try:
                    _change_status(page, OTHER_STATUS[status])
                except PWTimeout:
                    log(f"{STATUS_NAME[status]} list: the switch to {STATUS_NAME[OTHER_STATUS[status]]} gave no response")
            resp = _change_status(page, status)
            break
        except PWTimeout:
            if session_lost(page):
                raise SessionLost() from None
            try:
                shown = select.input_value()
            except Exception:
                shown = "unknown"
            log(f"{STATUS_NAME[status]} list: no response on attempt {attempt}/{attempts} (#deed_status shows {shown!r})")

    if resp is None:
        log(f"{STATUS_NAME[status]} list FAILED: {reason}")
        return ListResult(False, reason=reason)

    result = _parse_list(resp)
    if result.ok:
        log(f"{STATUS_NAME[status]} list loaded: {len(result.deeds)} deed(s) in {time.time() - started:.1f}s; "
            f"ascending: {result.deeds[:30]}")
    else:
        log(f"{STATUS_NAME[status]} list FAILED: {result.reason}")
    return result


def process_volume(page, row):
    """Click Process on the matched row and wait for the Search Index page."""
    started = time.time()
    row.get_by_text("Process", exact=True).first.click()
    try:
        page.locator("#deed_status").wait_for(state="visible", timeout=PROCESS_TIMEOUT_MS)
    except PWTimeout:
        if session_lost(page):
            raise SessionLost() from None
        log("Process: Search Index page did not open")
        return Outcome("failed", reason="Search Index page did not open after Process")
    log(f"Process: Search Index page opened in {time.time() - started:.1f}s")
    return Outcome("ready")


def download_next(page, row_cells, state, sig, status):
    """Load the list for `status`, pick the lowest deed not tried yet and save its PDF.

    Outcome kinds: deed, empty, all_tried, failed.
    """
    result = reload_list(page, status)
    if not result.ok:
        return Outcome("failed", reason=result.reason)
    if result.empty:
        return Outcome("empty")

    deed_label = next((d for d in result.deeds if not state.tried(sig, d)), None)
    if deed_label is None:
        log(f"Every {STATUS_NAME[status]} deed in this volume was already tried")
        return Outcome("all_tried", labels=result.deeds, reason="deeds remain but were already tried")

    log(f"Next deed: {deed_label} ({STATUS_NAME[status]})")
    state.mark(sig, deed_label, "in_progress")
    try:
        page.locator("#deed_no").click(timeout=ITEM_TIMEOUT_MS)
        item = page.locator(
            "#deed_suggestions li.list-group-item:visible",
            has_text=re.compile(rf"^\s*{re.escape(deed_label)}\s*$"),
        )
        item.wait_for(state="visible", timeout=ITEM_TIMEOUT_MS)
        folder = _download_selected(page, item, row_cells, status)
        return Outcome("deed", deed=deed_label, folder=folder)
    except SessionLost:
        raise
    except PWTimeout as exc:
        if session_lost(page):
            raise SessionLost() from None
        log(f"Download failed for deed {deed_label}: {exc!r}")
        state.mark(sig, deed_label, "download_failed", repr(exc)[:300])
        return Outcome("failed", deed=deed_label, reason=repr(exc)[:300])
    except Exception as exc:
        log(f"Download failed for deed {deed_label}: {exc!r}")
        state.mark(sig, deed_label, "download_failed", repr(exc)[:300])
        return Outcome("failed", deed=deed_label, reason=repr(exc)[:300])