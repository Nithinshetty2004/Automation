import json
import os
import re
from itertools import chain, islice
from pathlib import Path

from openpyxl import load_workbook

from util import log


WORKLIST_FILE = Path(os.getenv("WORKLIST_FILE", "worklist.xlsx"))
RETURNED_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
WORKLIST_JSON = RETURNED_DIR / "worklist.json"

HEADER_SCAN_ROWS = 3

REQUIRED_COLUMNS = [
    "S. No.",
    "Registration Office",
    "Volume Year",
    "Volume No.",
    "Deed Count",
    "Request Type",
]

OPTIONAL_COLUMNS = ["Book No."]
ALL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

# Allows minor differences in Excel header formatting.
HEADER_ALIASES = {
    "s no": "S. No.",
    "s no.": "S. No.",
    "serial no": "S. No.",
    "registration office": "Registration Office",
    "volume year": "Volume Year",
    "volume no": "Volume No.",
    "volume no.": "Volume No.",
    "book no": "Book No.",
    "book no.": "Book No.",
    "deed count": "Deed Count",
    "request type": "Request Type",
}


class WorklistError(RuntimeError):
    """Raised when the Excel worklist cannot be read safely."""


def normalize_header(value):
    """Normalize a column header for matching."""
    if value is None:
        return ""
    text = str(value).strip().casefold()
    text = re.sub(r"[\s._-]+", " ", text)
    return text.strip()


def normalize_value(value):
    """Convert Excel values to clean strings. 64.0 becomes '64'."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _num_key(text):
    """Numeric values sort by number (2 before 10); non-numeric values sort after them as text."""
    text = str(text).strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text.casefold())


def sort_key(record):
    """Volume no. ascending, then volume year ascending, then office, then book no."""
    return (
        _num_key(record["volume_no"]),
        _num_key(record["volume_year"]),
        record["registration_office"].casefold(),
        _num_key(record["book_no"]),
    )


def _map_columns(headers):
    """Map canonical column names to their indexes for one candidate header row."""
    column_map = {}
    for index, header in enumerate(headers):
        normalized = normalize_header(header)
        canonical = HEADER_ALIASES.get(normalized)

        if canonical is None:
            for required in ALL_COLUMNS:
                if normalized == normalize_header(required):
                    canonical = required
                    break

        if canonical:
            if canonical in column_map:
                raise WorklistError(f"Duplicate column detected: {canonical}")
            column_map[canonical] = index
    return column_map


def load_worklist(file_path=None, sheet_name=None):
    """
    Read the Excel worklist and return records sorted by volume no., then year.

    The header row is auto-detected within the first HEADER_SCAN_ROWS rows.
    "Book No." is optional and defaults to an empty string.

    Each record contains:
        excel_row, serial_no, registration_office, volume_year,
        volume_no, book_no, deed_count, request_type

    excel_row is the row in the original spreadsheet, so it will not be
    sequential after sorting. Invalid rows raise WorklistError instead of
    being silently skipped.
    """
    path = Path(file_path or WORKLIST_FILE)

    if not path.is_file():
        raise WorklistError(f"Worklist Excel file not found: {path.resolve()}")

    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise WorklistError("Unsupported worklist format. Save the file as .xlsx or .xlsm.")

    workbook = load_workbook(path, read_only=True, data_only=True)

    try:
        if sheet_name:
            if sheet_name not in workbook.sheetnames:
                raise WorklistError(
                    f"Worksheet '{sheet_name}' not found. Available sheets: {workbook.sheetnames}"
                )
            sheet = workbook[sheet_name]
        else:
            sheet = workbook.active

        rows = sheet.iter_rows(values_only=True)
        head = list(islice(rows, HEADER_SCAN_ROWS))

        if not head:
            raise WorklistError("The Excel worksheet is empty.")

        column_map = None
        header_row = None
        for row_no, headers in enumerate(head, start=1):
            candidate = _map_columns(headers)
            if all(c in candidate for c in REQUIRED_COLUMNS):
                column_map, header_row = candidate, row_no
                break

        if column_map is None:
            seen = {
                row_no: [normalize_header(h) for h in headers if h is not None]
                for row_no, headers in enumerate(head, start=1)
            }
            raise WorklistError(
                f"Required columns not found in the first {HEADER_SCAN_ROWS} rows. "
                f"Normalized values seen per row: {seen}"
            )

        log(f"Header row detected at Excel row {header_row}.")
        if "Book No." not in column_map:
            log("NOTE: 'Book No.' column not found; book_no will be empty for all records.")

        records = []

        data_rows = chain(head[header_row:], rows)
        for excel_row, values in enumerate(data_rows, start=header_row + 1):
            if not any(value is not None for value in values):
                continue  # ignore completely blank rows

            record = {
                column: normalize_value(
                    values[column_map[column]]
                    if column in column_map and column_map[column] < len(values)
                    else None
                )
                for column in ALL_COLUMNS
            }

            missing_values = [c for c in REQUIRED_COLUMNS if not record[c]]
            if missing_values:
                raise WorklistError(
                    f"Excel row {excel_row} has missing values in: {missing_values}. Row: {record}"
                )

            records.append({
                "excel_row": excel_row,
                "serial_no": record["S. No."],
                "registration_office": record["Registration Office"],
                "volume_year": record["Volume Year"],
                "volume_no": record["Volume No."],
                "book_no": record["Book No."],
                "deed_count": record["Deed Count"],
                "request_type": record["Request Type"],
            })

        if not records:
            raise WorklistError("No worklist records were found below the header row.")

        records.sort(key=sort_key)

        # The same volume listed twice would be matched twice on the portal; flag it.
        seen = {}
        for rec in records:
            ident = (
                rec["registration_office"].casefold(),
                rec["volume_year"],
                rec["volume_no"],
                rec["book_no"],
            )
            if ident in seen:
                log(
                    f"WARNING: duplicate volume in worklist: Excel rows "
                    f"{seen[ident]} and {rec['excel_row']} -> {ident}"
                )
            else:
                seen[ident] = rec["excel_row"]

        log(f"Loaded {len(records)} volume records from {path.name}, worksheet '{sheet.title}'.")
        return records

    finally:
        workbook.close()


def save_worklist_json(records, json_path=None):
    """Write the sorted worklist to returned_data/worklist.json (rebuilt on every run)."""
    target = Path(json_path or WORKLIST_JSON)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)  # atomic: a half-written file is never read
    log(f"Sorted worklist saved to {target.resolve()}")
    return target


def prepare_worklist(file_path=None, sheet_name=None, json_path=None):
    """Load, sort (volume no., then year), save the JSON copy, and return the records."""
    records = load_worklist(file_path, sheet_name)
    save_worklist_json(records, json_path)
    return records


if __name__ == "__main__":
    try:
        for item in prepare_worklist():
            print(item)
    except WorklistError as exc:
        log(f"Worklist error: {exc}")
        raise SystemExit(1)