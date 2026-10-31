import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from extract import validate
from util import log

RETURNED_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
PROCESSOR_OUTPUT_DIR = Path(os.getenv("PROCESSOR_DIR", "data_process")) / "output"


def _s(value):
    return str(value).strip()


def _locate(name):
    """Return the file in returned_data, copying it from data_process/output first if needed."""
    target = RETURNED_DIR / name
    if target.exists():
        return target
    source = PROCESSOR_OUTPUT_DIR / name
    if not source.exists():
        raise FileNotFoundError(f"Extraction result not found in {RETURNED_DIR} or {PROCESSOR_OUTPUT_DIR}: {name}")
    RETURNED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    shutil.copyfile(source, tmp)
    os.replace(tmp, target)
    return target


def _volume_year(meta):
    """Volume Year from the portal list row (S.No, Office, Year, Volume, Book, ...). None if unknown."""
    row = meta.get("list_row")
    if isinstance(row, list) and len(row) > 2 and str(row[2]).strip().isdigit():
        return int(str(row[2]).strip())
    return None


def _move_to_year(text, year, label, name):
    date = datetime.strptime(text, "%d-%m-%Y")
    if date.year == year:
        return date, text
    try:
        moved = date.replace(year=year)
    except ValueError:
        raise ValueError(f"{name}: {label} {text} cannot be moved to the Volume Year {year}")
    result = moved.strftime("%d-%m-%Y")
    log(f"Volume Year fix: {label} {text} -> {result} (Volume Year {year}, from {name})")
    return moved, result


def _fix_years(data, meta, name):
    """Presentation Date -> the deed's Presentation Year (portal rule).
    Registration Date -> the Volume Year (portal rule). Day and month are kept.
    Returns (presentation_date, registration_date)."""
    presentation = _s(data["Presentation Date"])
    registration = _s(data["Registration Date"])
    presented = datetime.strptime(presentation, "%d-%m-%Y")
    registered = datetime.strptime(registration, "%d-%m-%Y")

    pres_year = int(str(meta["presentation_year"]).strip())
    if presented.year != pres_year:
        try:
            presented = presented.replace(year=pres_year)
        except ValueError:
            raise ValueError(f"{name}: Presentation Date {presentation} cannot be moved to Presentation Year {pres_year}")
        fixed = presented.strftime("%d-%m-%Y")
        log(f"Presentation Year fix: Presentation Date {presentation} -> {fixed} (from {name})")
        presentation = fixed

    vol_year = _volume_year(meta)
    if vol_year is not None and registered.year != vol_year:
        try:
            registered = registered.replace(year=vol_year)
        except ValueError:
            raise ValueError(f"{name}: Registration Date {registration} cannot be moved to Volume Year {vol_year}")
        fixed = registered.strftime("%d-%m-%Y")
        log(f"Volume Year fix: Registration Date {registration} -> {fixed} (Volume Year {vol_year}, from {name})")
        registration = fixed

    if registered < presented:
        raise ValueError(
            f"{name}: Registration Date {registration} is before Presentation Date {presentation}"
        )
    return presentation, registration


def load_extracted(folder):
    """Return (meta, values) from <volume>_<deed>_<year>_extracted.json."""
    meta = json.loads((Path(folder) / "meta.json").read_text(encoding="utf-8"))
    path = _locate(f"{meta['volume_id']}_{meta['doct_no']}_{meta['presentation_year']}_extracted.json")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc

    problems = validate(data, meta)
    if problems:
        raise ValueError(f"{path.name} failed validation: " + "; ".join(problems))
    presentation_date, registration_date = _fix_years(data, meta, path.name)

    values = {
        "presentation_date": presentation_date,
        "registration_date": registration_date,
        "page_from": _s(data["Page No. From"]),
        "page_to": _s(data["Page No. To"]),
        "parties": [
            {
                "party_type": _s(p["Party Type"]).title(),
                "name": _s(p["Name"]),
                "relation_name": _s(p["Relation Name"]),
            }
            for p in data["Parties"]
        ],
        "properties": [
            {
                "circle": _s(p["Circle"]),
                "village": _s(p["Village/Thana"]),
                "khata_no": _s(p["Khata No"]),
                "khasra_no": _s(p["Khasra No"]),
            }
            for p in data["Properties"]
        ],
    }
    return meta, values