from util import log

REQUEST_TYPE_COL = 7  # S.No, Office, Vol Year, Vol No, Book No, Forwarded, Created By, Request Type, Action
NEXT_BTN = (
    "a:has-text('Next'), button:has-text('Next'), "
    "li.next:not(.disabled) a, .paginate_button.next:not(.disabled)"
)
LIST_URL_PART = "digitize/metaDataCreation"


def row_signature(cells):
    """Office | volume year | volume no | book no. S.No is left out because it shifts."""
    return "|".join(cells[1:5])


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


def _is_disabled(el):
    return el.evaluate(
        "e => e.getAttribute('aria-disabled') === 'true' || e.disabled"
        " || /disabled/.test(e.className) || /disabled/.test(e.parentElement?.className || '')"
    )


def find_new_row(page, skip=()):
    page_no = 1
    while True:
        page.wait_for_selector("table tbody tr td")
        rows = page.locator("table tbody tr")
        first_sig = rows.first.inner_text()
        for i in range(rows.count()):
            cells = rows.nth(i).locator("td")
            if cells.count() > REQUEST_TYPE_COL and cells.nth(REQUEST_TYPE_COL).inner_text().strip() == "New":
                texts = [cells.nth(c).inner_text().strip() for c in range(cells.count() - 1)]
                if row_signature(texts) in skip:
                    continue
                log(f"New row found on page {page_no}: {texts}")
                return rows.nth(i), texts

        nxt = page.locator(NEXT_BTN).first
        if nxt.count() == 0 or _is_disabled(nxt):
            return None, None
        nxt.scroll_into_view_if_needed()
        nxt.click()
        try:
            page.wait_for_function(
                "s => document.querySelector('table tbody tr')?.innerText !== s",
                arg=first_sig,
                timeout=10_000,
            )
        except Exception:
            return None, None
        page_no += 1
