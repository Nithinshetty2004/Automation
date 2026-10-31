import re
from difflib import SequenceMatcher

from playwright.sync_api import TimeoutError as PWTimeout

from state import volume_signature
from util import log

LIST_URL_PART = "digitize/metaDataCreation"
OFFICE_MIN_SIMILARITY = 0.70
ROWS_SEL = "table tbody tr"
NEXT_SEL = "ul#pagination li.next:not(.disabled) a"
PREV_SEL = "ul#pagination li.prev:not(.disabled) a"
ACTIVE_PAGE_SEL = "ul#pagination li.active a"
LIST_COLUMNS = 9  # S.No, Office, Year, Volume, Book, Forwarded, Created By, Request Type, Action


class NavigationError(RuntimeError):
    pass


def _normalize(text):
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _normalize_office(text):
    """Remove a trailing numeric office code, e.g. 'Bettiah (4700)' -> 'bettiah'."""
    return _normalize(re.sub(r"\s*\(\d+\)\s*$", "", str(text or "")))


def row_signature(cells):
    """Office | year | volume no | book no. S.No is left out because it shifts."""
    return volume_signature(cells[1], cells[2], cells[3], cells[4])


def open_list(page):
    if LIST_URL_PART not in page.url:
        page.get_by_text("Digitization of Registered Records", exact=True).first.click()
        page.get_by_text("Metadata Creation", exact=True).first.click()
        page.wait_for_url(lambda url: LIST_URL_PART in url)
    page.wait_for_selector("table tbody tr td")
    log("Metadata Creation list loaded")


def return_to_list(page):
    """Use the portal's own Back button first, and fall back to the menu."""
    for _ in range(3):
        if LIST_URL_PART in page.url:
            break
        back = page.locator("#backbtn:visible")
        button = back.first if back.count() else page.locator("button:visible", has_text="Back").first
        if button.count() == 0:
            break
        button.click()
        try:
            page.wait_for_url(lambda url: LIST_URL_PART in url, timeout=8_000)
        except Exception:
            pass  # may be the Search Index page, which has its own Back
    open_list(page)


def _is_xhr(resp):
    return resp.request.resource_type in ("xhr", "fetch")


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except PWTimeout:
        pass
    page.wait_for_selector(ROWS_SEL)


def _reset_search(page):
    page.locator("button.reset-btn").click()
    _settle(page)
    log("Search filters reset")


def _search_volume(page, volume_no):
    volume_no = str(volume_no).strip()
    page.locator("#searchInput").fill(volume_no)
    try:
        with page.expect_response(_is_xhr, timeout=10_000):
            page.locator("button.search-btn").click()
    except PWTimeout:
        log("WARNING: no XHR seen after Search (client-side filter?). Relying on the row check.")
    _settle(page)
    try:
        page.wait_for_function(
            """v => {
                const rows = [...document.querySelectorAll('table tbody tr')];
                if (!rows.length) return false;
                return rows.every(r => {
                    const td = r.querySelectorAll('td');
                    if (td.length < 9) return /no records/i.test(r.innerText);
                    return td[3].innerText.trim() === v;
                });
            }""",
            arg=volume_no,
            timeout=15_000,
        )
    except PWTimeout:
        raise NavigationError(f"Search for volume {volume_no} did not produce a filtered list")
    log(f"Portal search submitted for volume number {volume_no}")


def _read_rows(page):
    """[(dom_index, [8 cell texts])] for rows that look like volume rows."""
    rows = page.evaluate(
        "() => [...document.querySelectorAll('table tbody tr')].map(r =>"
        " [...r.querySelectorAll('td')].map(td => td.innerText.trim()))"
    )
    return [(i, cells[: LIST_COLUMNS - 1]) for i, cells in enumerate(rows) if len(cells) >= LIST_COLUMNS]


def _snapshot(page):
    active = page.locator(ACTIVE_PAGE_SEL)
    page_no = active.first.inner_text().strip() if active.count() else ""
    first = page.locator(ROWS_SEL).first.inner_text() if page.locator(ROWS_SEL).count() else ""
    return page_no, first


def _click_pager(page, selector):
    """Click Next/Previous. Returns False if it is unavailable; raises if the page did not change."""
    link = page.locator(selector).first
    if link.count() == 0:
        return False
    before_no, before_first = _snapshot(page)
    link.scroll_into_view_if_needed()
    link.click()
    try:
        page.wait_for_function(
            """([n, f]) => {
                const a = document.querySelector('ul#pagination li.active a');
                const r = document.querySelector('table tbody tr');
                return a && r && a.innerText.trim() !== n && r.innerText !== f;
            }""",
            arg=[before_no, before_first],
            timeout=10_000,
        )
    except PWTimeout:
        raise NavigationError("Pagination did not move after clicking it")
    return True


def find_worklist_row(page, record):
    """Search once, scan the filtered pages in order and stop at the first page with a match.

    Returns (row_locator, cells), or (None, None) when nothing matches. Raises NavigationError
    when the page holds more than one match or pagination misbehaves, so a row is never picked
    arbitrarily. Matches on later pages are not looked for once a page has matched.
    """
    want_year = str(record["volume_year"]).strip()
    want_vol = str(record["volume_no"]).strip()
    want_book = str(record["book_no"]).strip()
    want_office = _normalize_office(record["registration_office"])

    open_list(page)
    _reset_search(page)
    _search_volume(page, want_vol)

    page_index = 1
    while True:
        matches = []
        for _, cells in _read_rows(page):
            if cells[2].strip() != want_year or cells[3].strip() != want_vol or cells[4].strip() != want_book:
                continue
            office = _normalize_office(cells[1])
            ratio = SequenceMatcher(None, want_office, office).ratio()
            log(f"Office similarity: {want_office!r} vs {office!r} = {ratio:.0%} (filtered page {page_index})")
            if ratio >= OFFICE_MIN_SIMILARITY:
                matches.append(cells)

        if len(matches) > 1:
            raise NavigationError(f"{len(matches)} portal rows match this volume on page {page_index}: {matches}")
        if len(matches) == 1:
            cells = matches[0]
            sig = row_signature(cells)
            candidates = [i for i, c in _read_rows(page) if row_signature(c) == sig]
            if len(candidates) != 1:
                raise NavigationError(
                    f"Expected one row with signature {sig} on page {page_index}, found {len(candidates)}"
                )
            log(f"Unique matching volume found on filtered page {page_index}: {cells}")
            return page.locator(ROWS_SEL).nth(candidates[0]), cells

        if not _click_pager(page, NEXT_SEL):
            break
        page_index += 1

    log(f"No portal row matches {record['registration_office']} / {want_year} / vol {want_vol} / book {want_book}")
    return None, None