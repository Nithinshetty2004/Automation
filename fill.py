import difflib
import re

import Forms
from playwright.sync_api import TimeoutError as PWTimeout

from Forms import (
    Activity,
    choose_option,
    click_save,
    fill_date,
    fill_field,
    fill_property_input,
    open_tab,
    pick_village_progressive_backspace,
    wait_row,
)
from session import StopRun
from util import log

# Every deed is a sale. These are the portal's exact option texts, not extracted data.
INSTRUMENT_TYPE = "Sale / Conveyance"
INSTRUMENT_SUB_TYPE = "Sale"
VILLAGE_FUZZY_MIN = 0.6
PARTY_ROWS = "#partydetails tbody tr"
PROPERTY_ROWS = "#propdetails tbody tr"
PARTY_TRASH = "#partydetails tbody tr button:has(em.fa-trash)"
PROPERTY_TRASH = "#propdetails tbody tr button:has(em.fa-trash)"
MAX_PAGE_RETRIES = 3

CONFIRM_ALERT = re.compile(r"are you sure|do you want|confirm", re.I)
CREATED_ALERT = re.compile(r"created successfully", re.I)


class SubmitUnconfirmed(StopRun):
    pass


def clean_name(val):
    """Strip trailing code numbers like (4002) to match the portal text."""
    return re.sub(r"\s*\(\d+\)", "", str(val or "")).strip()


def _row_count(page, selector):
    """Data rows only. The empty table still holds one 'No records found' placeholder row."""
    return page.evaluate(
        """sel => [...document.querySelectorAll(sel)]
            .filter(r => !/no records/i.test(r.innerText)).length""",
        selector,
    )


def _check_modify_radio_is_no(page):
    radios = page.locator('input[name="addphysicalstamp"][value="N"]:visible')
    if radios.count() != 1:
        raise RuntimeError(f"Expected one visible 'modify deed details = No' radio, found {radios.count()}")
    if not radios.first.is_checked():
        raise RuntimeError("'Do you want to modify the fetched deed details?' is not set to No")


def _delete_all_rows(page, tab_id, pane_id, rows_sel, trash_sel, what):
    """Delete every saved row through the portal's trash button (util.on_dialog accepts the confirms)."""
    open_tab(page, tab_id, pane_id)
    total = _row_count(page, rows_sel)
    if total == 0:
        log(f"{what}: no saved rows to delete")
        return
    log(f"{what}: deleting {total} saved row(s)...")
    for _ in range(total):
        before = _row_count(page, rows_sel)
        buttons = page.locator(trash_sel)
        if buttons.count() == 0:
            html = page.evaluate(
                "sel => { const r = document.querySelector(sel); return r ? r.outerHTML : ''; }", rows_sel
            )
            raise RuntimeError(f"{what}: no delete button found. First row HTML: {html[:800]}")
        buttons.first.click()
        try:
            page.wait_for_function(
                """([sel, n]) => [...document.querySelectorAll(sel)]
                    .filter(r => !/no records/i.test(r.innerText)).length < n""",
                arg=[rows_sel, before],
                timeout=15_000,
            )
        except PWTimeout:
            raise RuntimeError(f"{what}: row count did not drop below {before} after clicking delete")
        page.wait_for_timeout(500)
    left = _row_count(page, rows_sel)
    if left:
        raise RuntimeError(f"{what}: {left} row(s) still present after deleting")
    log(f"{what}: all saved rows deleted")


def clear_saved(page):
    """Remove saved Party and Property rows so an In-Progress deed is refilled from scratch."""
    _delete_all_rows(page, "nav-profile-tab", "nav-profile", PARTY_ROWS, PARTY_TRASH, "Party")
    _delete_all_rows(page, "nav-property-tab", "nav-property", PROPERTY_ROWS, PROPERTY_TRASH, "Property")


def _selected_text(page, selector):
    return page.evaluate(
        """sel => { const s = document.querySelector(sel);
                    const o = s && s.selectedOptions[0];
                    return o ? o.textContent.trim() : ''; }""",
        selector,
    )


def _select_instrument(page):
    choose_option(page, "#instrumentType", INSTRUMENT_TYPE, "Instrument Type", strict=True)
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except PWTimeout:
        pass
    page.wait_for_function("document.getElementById('instrumentSubType').options.length > 1", timeout=10_000)
    page.wait_for_timeout(500)
    choose_option(page, "#instrumentSubType", INSTRUMENT_SUB_TYPE, "Instrument Sub-Type", strict=True)
    for selector, expected in (("#instrumentType", INSTRUMENT_TYPE), ("#instrumentSubType", INSTRUMENT_SUB_TYPE)):
        actual = _selected_text(page, selector)
        if actual != expected:
            raise RuntimeError(f"{selector} shows {actual!r} after selecting, expected {expected!r}")


def _fill_index(page, act, values, on_pages_conflict=None):
    log("Filling Index tab...")
    open_tab(page, "nav-home-tab", "nav-home")
    fill_date(page, "#presentation_date", "#date_suggestions", values["presentation_date"], "Presentation Date")
    fill_date(page, "#registration_date", "#registration_suggestions", values["registration_date"], "Registration Date")
    _select_instrument(page)
    for retry in range(MAX_PAGE_RETRIES + 1):
        fill_field(page, "#start_page", values["page_from"], "Page No. From")
        fill_field(page, "#end_page", values["page_to"], "Page No. To")
        act.check("Index fields")
        try:
            click_save(page, act, "Index Save")
            return
        except RuntimeError as exc:
            if "already been assigned" not in str(exc) or on_pages_conflict is None:
                raise
            if retry == MAX_PAGE_RETRIES:
                raise RuntimeError(
                    f"Pages still rejected after {MAX_PAGE_RETRIES} shifts (last tried "
                    f"{values['page_from']}-{values['page_to']}): {exc}"
                ) from exc
            new_from, new_to = on_pages_conflict(values["page_from"], values["page_to"])
            log(f"Pages {values['page_from']}-{values['page_to']} rejected by the portal; "
                f"shifted to {new_from}-{new_to} (retry {retry + 1}/{MAX_PAGE_RETRIES})")
            values["page_from"], values["page_to"] = new_from, new_to


def _fill_party(page, act, values):
    log("Filling Party tab...")
    open_tab(page, "nav-profile-tab", "nav-profile")
    if _row_count(page, PARTY_ROWS):
        raise RuntimeError("Party table already has rows; refusing to add duplicates")

    for number, party in enumerate(values["parties"], 1):
        choose_option(
            page, "#party_type", party["party_type"], "Party Type",
            aliases={"executant": "Executant", "claimant": "Claimant"},
        )
        fill_field(page, "#party_name", party["name"], "Party Name")
        fill_field(page, "#party_relation_name", party["relation_name"], "Relation Name")
        act.check(f"Party {number} fields")
        page.locator("#createbtn").click()
        act.wait_for_event(timeout_ms=5000)
        act.check(f"Add Party {number}")
        wait_row(page, PARTY_ROWS, [party["name"]])
        count = _row_count(page, PARTY_ROWS)
        if count != number:
            raise RuntimeError(f"After adding party {number} the table has {count} row(s)")
        log(f"Party {number}/{len(values['parties'])} verified: {party['party_type']} {party['name']}")


def _village_name_sim(option_text, village):
    text = re.sub(r"\(\s*\d+\s*\)\s*$", "", option_text)
    return difflib.SequenceMatcher(None, Forms.norm(text), Forms.norm(village)).ratio()


def _try_village(page, thana_no, villages, what="Property Village/Thana"):
    """Type the Thana No in Village/Thana and click a matching option. Returns the chosen text or None."""
    inp = page.locator("#prop_village")
    items = page.locator("#prop_village_suggestions li")
    suffix = re.compile(rf"\(\s*0*{re.escape(thana_no)}\s*\)\s*$")
    attempts = [thana_no] + ([thana_no[:-1]] if len(thana_no) > 1 else [])

    for n, text in enumerate(attempts):
        inp.scroll_into_view_if_needed()
        inp.click()
        inp.fill("")
        inp.press_sequentially(text, delay=40)
        inp.dispatch_event("input")
        inp.dispatch_event("keyup")
        page.wait_for_timeout(800)
        if items.count() == 0:
            log(f"{what}: no options after typing {text!r}")
            continue
        page.locator("#prop_village_suggestions").evaluate("e => e.style.display = 'block'")
        texts = [t.strip() for t in items.all_inner_texts()]
        log(f"{what}: typed {text!r}, options: {texts[:10]}")

        def score(i):
            return max(_village_name_sim(texts[i], v) for v in villages)

        hits = [i for i, t in enumerate(texts) if suffix.search(t)]
        if hits:
            idx = max(hits, key=score)
            if len(hits) > 1:
                log(f"{what}: {len(hits)} options end with ({thana_no}); took the closest to {villages}")
        elif n < len(attempts) - 1:
            log(f"{what}: no option ends with ({thana_no}); backspacing one character")
            continue
        else:
            idx = max(range(len(texts)), key=score)
            if score(idx) < VILLAGE_FUZZY_MIN:
                log(f"{what}: closest option {texts[idx]!r} is too different from {villages}")
                return None
            log(f"WARNING: {what}: no option ends with ({thana_no}); took closest {texts[idx]!r} to {villages}")
        items.nth(idx).click(force=True)
        page.wait_for_timeout(300)
        log(f"{what}: selected {texts[idx]!r} (field shows {inp.input_value()!r})")
        return texts[idx]
    return None


def _pick_circle_and_village(page, prop):
    """Try each candidate circle until one gives a village. Raises if none does."""
    for number, cand in enumerate(prop["candidates"], 1):
        log(f"Property: trying circle {number}/{len(prop['candidates'])}: {cand['circle']!r} for Thana No {prop['thana_no']}")
        pick_village_progressive_backspace(
            page,
            value=clean_name(cand["circle"]),
            input_sel="#prop_circle",
            list_sel="#prop_circle_suggestions",
            hidden_sel="#prop_circle_id",
            what="Property Circle",
            require_id=False,
        )
        log(f"Property Circle: field shows {page.locator('#prop_circle').input_value()!r}")
        page.wait_for_timeout(1000)
        if _try_village(page, prop["thana_no"], cand["villages"]):
            return
        log(f"Property: no village for Thana No {prop['thana_no']} under circle {cand['circle']!r}")
    raise RuntimeError(
        f"No circle gave a village for Thana No {prop['thana_no']}; tried {[c['circle'] for c in prop['candidates']]}"
    )


def _fill_property(page, act, values):
    log("Filling Property tab...")
    open_tab(page, "nav-property-tab", "nav-property")
    if _row_count(page, PROPERTY_ROWS):
        raise RuntimeError("Property table already has rows; refusing to add duplicates")

    district = clean_name(page.locator("#volume_edit_district").inner_text())
    sro = clean_name(page.locator("#volume_edit_sro").inner_text())
    if not district or not sro:
        raise RuntimeError("Volume Details did not show a District and Registration Office")
    log(f"Volume Details -> District: {district!r}, Registration Office: {sro!r}")

    for number, prop in enumerate(values["properties"], 1):
        fill_property_input(page, "#prop_district", "#prop_district_suggestions", "#prop_district_id", district, "Property District")
        page.wait_for_timeout(500)
        fill_property_input(page, "#prop_sro", "#prop_sro_suggestions", "#prop_sro_id", sro, "Property Registration Office")
        page.wait_for_timeout(500)
        _pick_circle_and_village(page, prop)
        page.wait_for_timeout(400)
        fill_field(page, "#khata_no", prop["khata_no"], "Khata No.")
        fill_field(page, "#khasra_no", prop["khasra_no"], "Khasra No.")
        act.check(f"Property {number} fields")

        page.locator("#createpropbtn").click()
        act.wait_for_event(timeout_ms=5000)
        act.check(f"Add Property {number}")
        wait_row(page, PROPERTY_ROWS, [prop["khata_no"], prop["khasra_no"]])
        count = _row_count(page, PROPERTY_ROWS)
        if count != number:
            raise RuntimeError(f"After adding property {number} the table has {count} row(s)")
        log(f"Property {number}/{len(values['properties'])} verified: Khata {prop['khata_no']}, Khasra {prop['khasra_no']}")

    if page.locator("#nav-property button:visible", has_text="Save").count() > 0:
        log("Separate Save button found in the Property tab. Saving...")
        click_save(page, act, "Property Save")


def fill_all(page, values, clear_existing=False, on_pages_conflict=None):
    """Fill and save every tab. Returns the review notes (empty when everything matched exactly).

    clear_existing=True deletes saved Party and Property rows first (In-Progress deeds).
    on_pages_conflict(page_from, page_to) -> (new_from, new_to) handles pages the portal rejects.
    """
    values = dict(values)
    Forms.REVIEW_NOTES.clear()
    Forms.REVIEW_NOTES.extend(values.get("lookup_notes", []))
    _check_modify_radio_is_no(page)
    if clear_existing:
        clear_saved(page)
    act = Activity(page)
    act.start()
    try:
        _fill_index(page, act, values, on_pages_conflict)
        _fill_party(page, act, values)
        _fill_property(page, act, values)
    finally:
        act.stop()
    return list(Forms.REVIEW_NOTES)


def submit_deed(page):
    """Click Submit, require a successful write plus a 'created successfully' alert,
    then require the portal to return to Search Index. Never retry a SubmitUnconfirmed blindly."""
    button = page.locator("#addVolumeBtn")
    button.wait_for(state="visible")
    act = Activity(page)
    act.start()
    try:
        button.click()
        if not act.wait_for_event(timeout_ms=20_000):
            raise SubmitUnconfirmed("Submit was clicked but no server request or alert followed")

        waited = 0
        while waited < 30_000:
            created = any(CREATED_ALERT.search(m) for m in act.dialogs)
            wrote = any(m in ("POST", "PUT") and s < 400 for m, _, s in act.writes)
            if created and wrote:
                break
            page.wait_for_timeout(250)
            waited += 250

        for method, url, status in act.writes:
            log(f"Submit: {method} {url} -> {status}")
        for msg in act.dialogs:
            log(f"Submit: portal alert: {msg}")

        errors = [w for w in act.writes if w[2] >= 400]
        bad = [
            m for m in act.dialogs
            if Forms.BAD_ALERT.search(m) and not CONFIRM_ALERT.search(m) and not CREATED_ALERT.search(m)
        ]
        created = any(CREATED_ALERT.search(m) for m in act.dialogs)
        wrote = any(m in ("POST", "PUT") and s < 400 for m, _, s in act.writes)
        if errors or bad:
            raise SubmitUnconfirmed(f"Submit failed. Server errors: {errors}. Alerts: {bad}")
        if not (created and wrote):
            raise SubmitUnconfirmed(f"Submit unconfirmed (created alert: {created}, successful write: {wrote})")
    finally:
        act.stop()

    try:
        page.locator("#deed_status").wait_for(state="visible", timeout=20_000)
        button.wait_for(state="hidden", timeout=10_000)
    except PWTimeout:
        raise SubmitUnconfirmed("Submit was accepted but the Search Index page did not reappear")
    log("Submit confirmed: created successfully and back on Search Index")