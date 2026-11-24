import json
from datetime import datetime
from pathlib import Path

import thana
from extract import (
    PARTY_FIELDS,
    PROPERTY_FIELDS,
    adopt_legacy,
    decode_records,
    extracted_path,
    validate,
)
from util import log


def _s(value):
    return "" if value is None else str(value).strip()


def _volume_year(meta):
    row = meta.get("list_row")
    if isinstance(row, list) and len(row) > 2 and str(row[2]).strip().isdigit():
        return int(str(row[2]).strip())
    return None


def _volume_sro(meta, folder):
    row = meta.get("list_row")
    if isinstance(row, list) and len(row) > 1 and str(row[1]).strip():
        return str(row[1]).strip()

    for field in ("sro_name_en", "vault_sro_name_en", "registration_office"):
        if str(meta.get(field) or "").strip():
            return str(meta[field]).strip()

    details_path = Path(folder) / "details.json"
    if details_path.is_file():
        try:
            details = json.loads(details_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{details_path.name} is not valid JSON: {exc}") from exc
        details_data = details.get("data", details) if isinstance(details, dict) else {}
        if isinstance(details_data, dict):
            for field in ("sro_name_en", "vault_sro_name_en", "registration_office"):
                if str(details_data.get(field) or "").strip():
                    return str(details_data[field]).strip()

    raise ValueError("Registration Office name not found in meta.json or details.json")


def _fix_years(data, meta, name):
    """Presentation Date -> the deed's Presentation Year. Registration Date -> the Volume Year."""
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
        raise ValueError(f"{name}: Registration Date {registration} is before Presentation Date {presentation}")
    return presentation, registration


def load_extracted(folder):
    """Return (meta, values) from the JSON inside the deed folder.

    FileNotFoundError: no JSON in the deed folder (or a legacy copy). ValueError: invalid JSON,
    failed validation, or a Thana lookup failure (ThanaNotFound is a ValueError).
    """
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    path = extracted_path(folder, meta)
    if not adopt_legacy(path):
        raise FileNotFoundError(f"Extraction result not found in the deed folder: {path}")
    log(f"JSON: reading {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc

    problems = validate(data, meta)
    if problems:
        raise ValueError(f"{path.name} failed validation: " + "; ".join(problems))
    presentation_date, registration_date = _fix_years(data, meta, path.name)

    sro = _volume_sro(meta, folder)
    party_records = decode_records(data, "Parties", PARTY_FIELDS)
    property_records = decode_records(data, "Properties", PROPERTY_FIELDS)
    properties = []
    lookup_notes = []
    for prop in property_records:
        found = thana.resolve(sro, prop, deed=meta["doct_no"], volume=meta["volume_id"])
        if found.get("note"):
            lookup_notes.append(found["note"])
        properties.append({
            "thana_no": found["thana_no"],
            "candidates": found["candidates"],
            "khata_no": _s(prop["Khata No"]),
            "khasra_no": _s(prop["Khasra No"]),
        })

    values = {
        "presentation_date": presentation_date,
        "registration_date": registration_date,
        "parties": [
            {
                "party_type": _s(p["Party Type"]).title(),
                "name": _s(p["Name"]),
                "relation_name": _s(p["Relation Name"]),
            }
            for p in party_records
        ],
        "properties": properties,
        "page_from_hint": _s(data.get("Page No. From")),
        "lookup_notes": lookup_notes,
    }
    return meta, values