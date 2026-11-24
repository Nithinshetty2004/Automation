import csv
import difflib
import os
import re
import time
from pathlib import Path

from openpyxl import load_workbook

from util import log

THANA_FILE = Path(os.getenv("THANA_FILE", "THANA_NO.xlsx"))
THANA_SHEET = os.getenv("THANA_SHEET", "").strip() or None
RETURNED_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
MISSING_CSV = RETURNED_DIR / "missing_thana.csv"
COLUMNS = {"sro name": "sro", "circle name": "circle", "thana no": "thana", "village name": "village"}
# Fuzzy village matching. A match is accepted only if it scores at least FUZZY_MIN and beats the
# runner-up by FUZZY_MARGIN. Anything weaker is held for review instead of guessed.
FUZZY_MIN = float(os.getenv("THANA_FUZZY_MIN", "0.82"))
FUZZY_MARGIN = float(os.getenv("THANA_FUZZY_MARGIN", "0.05"))

_by_key = {}  # (sro, thana) -> {circle: [villages]} in Excel order
_by_village = {}  # (sro, normalized village) -> set of Thana numbers
_villages_by_sro = {}  # sro -> set of normalized village names
_sros = set()
_loaded = False


class ThanaNotFound(ValueError):
    pass


def base_thana(value):
    """'341 / 3' -> '341'; 24.0 -> '24'; '025' -> '25'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).split("/")[0].strip()
    return str(int(text)) if text.isdigit() else text.casefold()


def norm_sro(value):
    text = re.sub(r"\s*\(\d+\)\s*$", "", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


def norm_village(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def village_names(value):
    text = _clean(value)
    return {
        norm_village(name)
        for name in (text, *text.split("/"))
        if norm_village(name)
    }


def _clean(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def load():
    """Read THANA_NO.xlsx. Called at every start of main.py."""
    global _loaded
    if not THANA_FILE.is_file():
        raise FileNotFoundError(f"Thana Excel not found: {THANA_FILE.resolve()}")
    wb = load_workbook(THANA_FILE, read_only=True, data_only=True)
    count = 0
    try:
        ws = wb[THANA_SHEET] if THANA_SHEET else wb.active
        rows = ws.iter_rows(values_only=True)
        headers = [re.sub(r"[\s._-]+", " ", str(h or "")).strip().casefold() for h in next(rows)]
        missing = [h for h in COLUMNS if h not in headers]
        if missing:
            raise ValueError(f"{THANA_FILE.name}: missing columns {missing}. Found: {headers}")
        idx = {COLUMNS[h]: headers.index(h) for h in COLUMNS}
        _by_key.clear()
        _by_village.clear()
        _villages_by_sro.clear()
        _sros.clear()
        for row in rows:
            rec = {k: _clean(row[i]) if i < len(row) else "" for k, i in idx.items()}
            rec["thana"] = base_thana(row[idx["thana"]] if idx["thana"] < len(row) else None)
            if not (rec["sro"] and rec["thana"] and rec["circle"] and rec["village"]):
                continue
            count += 1
            sro = norm_sro(rec["sro"])
            _sros.add(sro)
            villages = _by_key.setdefault((sro, rec["thana"]), {}).setdefault(rec["circle"], [])
            if rec["village"] not in villages:
                villages.append(rec["village"])
            for name in village_names(rec["village"]):
                village_key = (sro, name)
                _by_village.setdefault(village_key, set()).add(rec["thana"])
                _villages_by_sro.setdefault(sro, set()).add(name)
    finally:
        wb.close()
    _loaded = True
    multi = sum(1 for c in _by_key.values() if len(c) > 1)
    log(f"Thana map loaded from {THANA_FILE.name}: {count} rows, {len(_by_key)} keys, "
        f"{multi} keys with 2+ circles (tried one by one), {len(_sros)} SROs")


def _compact(value):
    """Letters and digits only, lower case: 'Bhawani Pur' and 'bhawanipur' become the same."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


_PHONETIC = (
    ("z", "j"), ("chh", "c"), ("ch", "c"), ("sh", "s"), ("ph", "f"), ("bh", "b"), ("dh", "d"),
    ("gh", "g"), ("kh", "k"), ("th", "t"), ("jh", "j"), ("ee", "i"), ("oo", "u"), ("aa", "a"),
    ("w", "v"), ("y", "i"),
)


def _phonetic(value):
    """Rough sound key for transliteration variants (Kazha/Kajha, Bhawani/Bavani)."""
    text = _compact(value)
    for old, new in _PHONETIC:
        text = text.replace(old, new)
    return re.sub(r"(.)\1+", r"\1", text)


def _similarity(a, b):
    ca, cb = _compact(a), _compact(b)
    if ca and ca == cb:
        return 1.0
    pa, pb = _phonetic(a), _phonetic(b)
    if pa and pa == pb:
        return 0.97
    return max(
        difflib.SequenceMatcher(None, ca, cb).ratio(),
        difflib.SequenceMatcher(None, pa, pb).ratio(),
    )


def _fuzzy_village(sro_key, village_name):
    """Return ((matched normalized village, score) or None, top 3 [(village, score)]) within one SRO."""
    scored = sorted(
        ((_similarity(village_name, name), name) for name in _villages_by_sro.get(sro_key, ())),
        reverse=True,
    )
    top = [(name, round(score, 2)) for score, name in scored[:3]]
    if not scored:
        return None, top
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    # One wrong letter in a very short name is a different village (Mara/Marwa), so be stricter.
    needed = max(FUZZY_MIN, 0.92) if len(_compact(village_name)) <= 5 else FUZZY_MIN
    if best_score >= needed and best_score - runner_up >= FUZZY_MARGIN:
        return (best, best_score), top
    return None, top


def _missing(sro, lookup_value, deed, volume, reason):
    try:
        MISSING_CSV.parent.mkdir(parents=True, exist_ok=True)
        new = not MISSING_CSV.exists() or MISSING_CSV.stat().st_size == 0
        with MISSING_CSV.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "volume_id", "deed", "sro", "lookup_value", "reason"])
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), volume, deed, sro, lookup_value, reason])
    except OSError as exc:
        log(f"WARNING: could not write {MISSING_CSV}: {exc}")
    log(f"Thana lookup failed: {reason}")
    return reason


def resolve(sro, prop, deed="", volume=""):
    """Return {thana_no, candidates:[{circle, villages}], note}, or raise ThanaNotFound."""
    if not _loaded:
        load()
    sro_key = norm_sro(sro)
    village_name = _clean(prop.get("Thana Name"))
    thana_raw = _clean(prop.get("Thana No"))
    base = base_thana(thana_raw)
    lookup_value = village_name or thana_raw

    if sro_key not in _sros:
        raise ThanaNotFound(_missing(
            sro, lookup_value, deed, volume, f"SRO {sro_key!r} is not in {THANA_FILE.name}"
        ))

    if village_name:
        note = None
        target = norm_village(village_name)
        matching_thanas = _by_village.get((sro_key, target), set())
        if not matching_thanas:
            hit, top = _fuzzy_village(sro_key, village_name)
            if not hit:
                raise ThanaNotFound(_missing(
                    sro, village_name, deed, volume,
                    f"Village {village_name!r} not found for SRO {sro_key!r} in {THANA_FILE.name}; "
                    f"closest: {top}",
                ))
            target, score = hit
            matching_thanas = _by_village[(sro_key, target)]
            log(f"Thana lookup: {village_name!r} matched {target!r} ({score:.0%}) for SRO {sro_key!r}")
            if score < 1.0:
                note = (
                    f"Thana Name {village_name!r} fuzzy-matched to village {target!r} "
                    f"({score:.0%}) for SRO {sro_key!r}"
                )
        if base and base not in matching_thanas:
            raise ThanaNotFound(_missing(
                sro, village_name, deed, volume,
                f"Village {village_name!r} maps to Thana No(s) {sorted(matching_thanas)}, "
                f"not extracted Thana No {base}",
            ))
        if base:
            thana_no = base
        elif len(matching_thanas) == 1:
            thana_no = next(iter(matching_thanas))
        else:
            raise ThanaNotFound(_missing(
                sro, village_name, deed, volume,
                f"Village {village_name!r} is ambiguous in SRO {sro_key!r}: "
                f"Thana No(s) {sorted(matching_thanas)}",
            ))

        circles = _by_key[(sro_key, thana_no)]
        candidates = [
            {"circle": circle, "villages": [v for v in villages if target in village_names(v)]}
            for circle, villages in circles.items()
            if any(target in village_names(v) for v in villages)
        ]
        log(
            f"Thana lookup: SRO {sro_key!r} village {village_name!r} -> "
            f"Thana {thana_no}, {[(c['circle'], c['villages']) for c in candidates]}"
        )
        return {"thana_no": thana_no, "candidates": candidates, "note": note}

    circles = _by_key.get((sro_key, base)) if base else None
    if not circles:
        raise ThanaNotFound(_missing(
            sro, thana_raw, deed, volume,
            f"Thana No {thana_raw or 'missing'} not found for SRO {sro_key!r} in {THANA_FILE.name}",
        ))
    candidates = [{"circle": c, "villages": v} for c, v in circles.items()]
    log(f"Thana lookup: SRO {sro_key!r} thana {base} -> {[(c['circle'], c['villages']) for c in candidates]}")
    return {"thana_no": base, "candidates": candidates, "note": None}