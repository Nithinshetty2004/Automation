import difflib
import re

from playwright.sync_api import TimeoutError as PWTimeout

from util import log

BAD_ALERT = re.compile(
    r"\b(please|invalid|required|mandatory|must|error|fail(ed|ure)?|already|unable|cannot|can't|wrong)\b",
    re.I,
)
SAVED_ALERT = re.compile(r"saved successfully", re.I)

# Filled whenever a field was matched loosely or a default was taken. Submit is blocked while it has entries.
REVIEW_NOTES = []


def norm(text):
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _same(option_text, value):
    a, b = norm(option_text), norm(value)
    return a == b or a.startswith(b + " (") or b.startswith(a + " (")


def _best_index(texts, target):
    """(index, exact). Exact match first, otherwise the most similar option."""
    for i, t in enumerate(texts):
        if _same(t, target):
            return i, True
    scores = [difflib.SequenceMatcher(None, norm(t), norm(target)).ratio() for t in texts]
    return max(range(len(texts)), key=scores.__getitem__), False


class Activity:
    """Records alerts and non-GET responses so every step can be verified.

    It does not accept dialogs. Register util.on_dialog on the page for that.
    """

    def __init__(self, page):
        self.page = page
        self.writes = []
        self.dialogs = []

    def _on_response(self, resp):
        method = resp.request.method
        if method != "GET":
            self.writes.append((method, resp.url.split("?")[0], resp.status))

    def _on_dialog(self, dialog):
        self.dialogs.append(dialog.message)

    def start(self):
        self.page.on("response", self._on_response)
        self.page.on("dialog", self._on_dialog)

    def stop(self):
        self.page.remove_listener("response", self._on_response)
        self.page.remove_listener("dialog", self._on_dialog)

    def wait_for_event(self, timeout_ms=10_000):
        waited = 0
        while not (self.writes or self.dialogs) and waited < timeout_ms:
            self.page.wait_for_timeout(250)
            waited += 250
        return bool(self.writes or self.dialogs)

    def wait_for_saved_alert(self, timeout_ms=8_000):
        """Wait until a 'saved successfully' alert or an error alert arrives."""
        waited = 0
        while waited < timeout_ms:
            if any(SAVED_ALERT.search(m) or BAD_ALERT.search(m) for m in self.dialogs):
                return
            self.page.wait_for_timeout(250)
            waited += 250

    def check(self, what, expect_saved=False):
        for method, url, status in self.writes:
            log(f"{what}: {method} {url} -> {status}")
        for msg in self.dialogs:
            log(f"{what}: portal alert: {msg}")
        errors = [w for w in self.writes if w[2] >= 400]
        bad = [m for m in self.dialogs if BAD_ALERT.search(m)]
        saved = any(SAVED_ALERT.search(m) for m in self.dialogs)
        had_event = bool(self.writes or self.dialogs)
        self.writes.clear()
        self.dialogs.clear()
        if errors or bad:
            raise RuntimeError(f"{what} failed. Server errors: {errors}. Alerts: {bad}")
        if expect_saved and not saved:
            raise RuntimeError(f"{what}: no 'saved successfully' alert was seen, so the save is unconfirmed")
        return had_event


def fill_field(page, selector, value, what, blur=True):
    loc = page.locator(selector)
    loc.fill(value)
    if norm(loc.input_value()) != norm(value):
        loc.fill("")
        loc.press_sequentially(value, delay=30)
    if blur:
        loc.press("Tab")
    got = loc.input_value()
    if norm(got) != norm(value):
        raise RuntimeError(f"{what}: wanted {value!r} but the field shows {got!r}")
    log(f"{what}: {got}")


def fill_date(page, selector, suggestions_sel, value, what):
    fill_field(page, selector, value, what, blur=False)
    page.wait_for_timeout(400)
    items = page.locator(f"{suggestions_sel} li")
    if items.count() and page.locator(suggestions_sel).is_visible():
        texts = [t.strip() for t in items.all_inner_texts()]
        hits = [i for i, t in enumerate(texts) if norm(t) == norm(value)]
        if hits:
            items.nth(hits[0]).click(force=True)
        else:
            log(f"{what}: suggestions shown but none equal {value!r}, keeping typed value")
            page.locator(suggestions_sel).evaluate("e => e.style.display = 'none'")
    page.locator(selector).press("Tab")
    got = page.locator(selector).input_value()
    if norm(got) != norm(value):
        raise RuntimeError(f"{what}: wanted {value!r} but the field shows {got!r}")


def choose_option(page, select_sel, value, what, aliases=None, strict=False, settle_ms=500):
    """Select an option and verify it stays selected.

    strict=True allows only an exact (or alias) match; no loose word or nearest matching.
    """
    sel = page.locator(select_sel)
    options = [
        (v, t)
        for v, t in sel.evaluate("s => [...s.options].map(o => [o.value, o.textContent.trim()])")
        if v not in ("", "0")
    ]
    want = norm(value)
    alias = (aliases or {}).get(want)
    if alias:
        log(f"{what}: {value!r} mapped by alias to {alias!r}")
        want = norm(alias)

    # 1. Exact match
    hits = [v for v, t in options if norm(t) == want]

    # 2. Substring / word overlap match
    if not hits and not strict:
        cleaned_want = re.sub(r"\bdeed\b", "", want).strip()
        tokens = [t for t in re.split(r"[\s/,-]+", cleaned_want) if len(t) > 2]
        token_hits = [v for v, t in options if any(tok in norm(t) for tok in tokens)]
        if len(token_hits) == 1:
            hits = token_hits
        elif len(token_hits) > 1:
            hits = [token_hits[0]]
        if hits:
            REVIEW_NOTES.append(f"{what}: {value!r} matched loosely by words")

    # 3. Nearest match fallback via difflib
    if not hits and not strict:
        option_texts = [norm(t) for _, t in options]
        close = difflib.get_close_matches(want, option_texts, n=1, cutoff=0.4)
        if close:
            best_text = close[0]
            hits = [v for v, t in options if norm(t) == best_text]
            log(f"{what}: nearest match for {value!r} is {best_text!r}")
            REVIEW_NOTES.append(f"{what}: {value!r} only nearest-matched to {best_text!r}")

    if len(hits) != 1:
        kind = "no" if not hits else "multiple"
        raise RuntimeError(f"{what}: {kind} option matches {value!r}. Options: {[t for _, t in options][:25]}")

    shown = ""
    for attempt in (1, 2):
        sel.select_option(value=hits[0])
        page.wait_for_timeout(settle_ms)  # the portal may reload or reset the select after a change
        shown = sel.evaluate("s => s.selectedIndex >= 0 ? s.options[s.selectedIndex].textContent.trim() : ''")
        if sel.input_value() == hits[0]:
            log(f"{what}: {shown}")
            return
        log(f"{what}: selection was reset to {shown!r}, retrying ({attempt}/2)")
    raise RuntimeError(f"{what}: the select shows {shown!r} after selecting {value!r}; the portal keeps resetting it")


def fill_property_input(page, input_sel, list_sel, hidden_sel, value, what):
    clean_val = re.sub(r"\s*\(\d+\)", "", str(value or "")).strip()
    inp = page.locator(input_sel)
    inp.scroll_into_view_if_needed()
    inp.click()
    page.wait_for_timeout(200)

    inp.fill("")
    inp.press_sequentially(clean_val, delay=30)
    inp.dispatch_event("input")
    inp.dispatch_event("keyup")
    page.wait_for_timeout(800)

    items = page.locator(f"{list_sel} li")
    if items.count() > 0:
        page.locator(list_sel).evaluate("e => e.style.display = 'block'")
        texts = [t.strip() for t in items.all_inner_texts()]
        chosen_idx, exact = _best_index(texts, clean_val)
        chosen_text = texts[chosen_idx]
        if not exact:
            REVIEW_NOTES.append(f"{what}: no option equals {clean_val!r}, took closest {chosen_text!r}")
        items.nth(chosen_idx).click(force=True)
        page.wait_for_timeout(300)
        chosen_id = page.locator(hidden_sel).evaluate("e => e.value") if page.locator(hidden_sel).count() else ""
        if not chosen_id:
            REVIEW_NOTES.append(f"{what}: the portal stored no id after selecting {chosen_text!r}")
        log(f"{what}: selected {chosen_text!r} (hidden id: {chosen_id})")
    else:
        inp.press("Tab")
        got = inp.input_value()
        log(f"{what}: filled with {got!r}")
        REVIEW_NOTES.append(f"{what}: no suggestions appeared, only typed {got!r}")


def pick_village_progressive_backspace(
    page,
    value,
    input_sel="#prop_village",
    list_sel="#prop_village_suggestions",
    hidden_sel="#prop_village_id",
    what="Property Village/Thana",
    require_id=True,
):
    """Type the full value, then backspace one character at a time until the dropdown
    shows options, and click the closest one. Works for Circle and Village/Thana."""
    inp = page.locator(input_sel)
    items = page.locator(f"{list_sel} li")
    target_val = str(value or "").strip()

    inp.scroll_into_view_if_needed()
    inp.click()
    page.wait_for_timeout(200)

    log(f"{what}: typing full name {target_val!r}...")
    inp.fill("")
    inp.press_sequentially(target_val, delay=40)
    inp.dispatch_event("input")
    inp.dispatch_event("keyup")
    page.wait_for_timeout(600)

    def _stored_id():
        if not page.locator(hidden_sel).count():
            return ""
        for _ in range(5):
            chosen_id = page.locator(hidden_sel).evaluate("e => e.value")
            if chosen_id:
                return chosen_id
            page.wait_for_timeout(300)
        return ""

    def _try_pick():
        if items.count() == 0:
            return False
        page.locator(list_sel).evaluate("e => e.style.display = 'block'")
        texts = [t.strip() for t in items.all_inner_texts()]
        chosen_idx, exact = _best_index(texts, target_val)
        chosen_text = texts[chosen_idx]
        if not exact:
            REVIEW_NOTES.append(f"{what}: no option equals {target_val!r}, took closest {chosen_text!r}")
            log(f"{what}: options {texts[:10]}; closest to {target_val!r} is {chosen_text!r}")
        items.nth(chosen_idx).click(force=True)
        page.wait_for_timeout(300)
        chosen_id = _stored_id()
        if not chosen_id:
            REVIEW_NOTES.append(f"{what}: the portal stored no id after selecting {chosen_text!r}")
            if require_id:
                raise RuntimeError(f"{what}: selected {chosen_text!r} but the portal stored no id ({hidden_sel} is empty)")
        log(f"{what}: selected {chosen_text!r} (hidden id: {chosen_id})")
        return True

    if _try_pick():
        return

    current_text = target_val
    while len(current_text) > 0:
        log(f"{what}: backspacing from {current_text!r}...")
        inp.press("Backspace")
        current_text = current_text[:-1]
        inp.dispatch_event("input")
        inp.dispatch_event("keyup")
        page.wait_for_timeout(500)
        if _try_pick():
            return

    log(f"{what}: field empty, triggering suggestions via ArrowDown...")
    inp.press("ArrowDown")
    page.wait_for_timeout(600)
    if _try_pick():
        return

    inp.press_sequentially("a", delay=40)
    inp.press("Backspace")
    inp.dispatch_event("input")
    inp.dispatch_event("keyup")
    page.wait_for_timeout(600)
    if not _try_pick():
        raise RuntimeError(f"{what}: no options could be selected after backspacing.")


def has_rows(page, row_sel):
    return page.evaluate(
        "sel => [...document.querySelectorAll(sel)].some(r =>"
        " !r.innerText.toLowerCase().includes('no records found') && r.querySelectorAll('td').length > 1)",
        row_sel,
    )


def wait_row(page, row_sel, parts, timeout_ms=10_000):
    page.wait_for_function(
        "([sel, parts]) => [...document.querySelectorAll(sel)].some(r => {"
        " const t = r.innerText.toLowerCase();"
        " return !t.includes('no records found') && parts.every(p => t.includes(p.toLowerCase())); })",
        arg=[row_sel, parts],
        timeout=timeout_ms,
    )


def click_save(page, act, what):
    buttons = page.locator("button:visible", has_text=re.compile(r"^\s*Save\s*$"))
    count = buttons.count()
    if count != 1:
        if count:
            found = [buttons.nth(i).evaluate("e => e.outerHTML") for i in range(count)]
        else:
            found = page.locator("button:visible").all_inner_texts()
        raise RuntimeError(f"{what}: expected one visible Save button, found {count}: {found}")
    buttons.first.click()
    if not act.wait_for_event():
        raise RuntimeError(f"{what}: no server request or alert seen within 10s, so the save is unconfirmed")
    act.wait_for_saved_alert()
    act.check(what, expect_saved=True)


def open_tab(page, tab_id, pane_id):
    page.locator(f"#{tab_id}").click()
    page.locator(f"#{pane_id}.active").wait_for()