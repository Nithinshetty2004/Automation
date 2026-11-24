import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from util import log
LEGACY_FALLBACK = os.getenv("LEGACY_JSON_FALLBACK", "true").strip().lower() in ("1", "true", "yes")
PROCESSOR_DIR = Path(os.getenv("PROCESSOR_DIR", "data_process")).resolve()
RETURNED_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
TIMEOUT_S = int(os.getenv("PROCESSOR_TIMEOUT_SECONDS", "300"))
ATTEMPTS = int(os.getenv("PROCESSOR_ATTEMPTS", "2"))

TOP_FIELDS = ["Presentation Date", "Registration Date"]
PARTY_FIELDS = ("Party Type", "Name", "Relation Name")
PROPERTY_FIELDS = ("Thana Name", "Thana No", "Khata No", "Khasra No")

NOT_A_VALUE_PREFIX = ("unclear", "unreadable", "illegible", "not found", "not available")
NOT_A_VALUE_EXACT = {"n/a", "na", "null", "none", "-"}


class ExtractionError(RuntimeError):
    pass


def _python():
    configured = os.getenv("PROCESSOR_PYTHON")
    if configured:
        return configured
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = PROCESSOR_DIR / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _text(value):
    """Clean string, or None when the value is missing or placeholder text."""
    if value is None:
        return None
    text = str(value).strip()
    low = text.lower()
    if not text or low in NOT_A_VALUE_EXACT or low.startswith(NOT_A_VALUE_PREFIX):
        return None
    return text


def _date(text):
    try:
        return datetime.strptime(text, "%d-%m-%Y")
    except (TypeError, ValueError):
        return None


def decode_records(data, field_name, fields):
    """Convert the processor's positional rows to named records for internal use."""
    rows = data.get(field_name)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{field_name}: missing or empty")

    records = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError(
                f"{field_name} {index}: expected an array of {len(fields)} values"
            )
        records.append(dict(zip(fields, row)))
    return records


def validate(data, meta=None):
    """Return a list of problems. An empty list means the data is safe to fill."""
    if not isinstance(data, dict):
        return ["expected a JSON object"]
    problems = []

    top = {f: _text(data.get(f)) for f in TOP_FIELDS}
    for f, v in top.items():
        if v is None:
            problems.append(f"{f}: missing or unreadable")

    presented = _date(top["Presentation Date"])
    registered = _date(top["Registration Date"])
    for f, d in (("Presentation Date", presented), ("Registration Date", registered)):
        if top[f] is not None and d is None:
            problems.append(f"{f}: not dd-mm-yyyy ({top[f]!r})")
    if presented and meta and str(presented.year) != str(meta.get("presentation_year")):
        problems.append(f"Presentation year {presented.year} differs from the portal's {meta.get('presentation_year')}")

    try:
        parties = decode_records(data, "Parties", PARTY_FIELDS)
    except ValueError as exc:
        problems.append(str(exc))
        parties = []

    executants = claimants = 0
    for i, party in enumerate(parties, 1):
        for field in ("Party Type", "Name"):
            if _text(party[field]) is None:
                problems.append(f"Party {i} {field}: missing or unreadable")
        kind = (_text(party["Party Type"]) or "").casefold()
        if kind == "executant":
            executants += 1
        elif kind == "claimant":
            claimants += 1
        elif kind:
            problems.append(
                f"Party {i}: Party Type must be Executant or Claimant, "
                f"got {party['Party Type']!r}"
            )
    if parties and executants < 1:
        problems.append("Parties: no Executant found")
    if parties and claimants < 1:
        problems.append("Parties: no Claimant found")

    try:
        properties = decode_records(data, "Properties", PROPERTY_FIELDS)
    except ValueError as exc:
        problems.append(str(exc))
        properties = []

    for i, prop in enumerate(properties, 1):
        for field in ("Khata No", "Khasra No"):
            if _text(prop[field]) is None:
                problems.append(f"Property {i} {field}: missing or unreadable")
        if _text(prop["Thana Name"]) is None and _text(prop["Thana No"]) is None:
            problems.append(f"Property {i} Thana Name or Thana No: missing or unreadable")
        thana_text = _text(prop["Thana No"])
        if thana_text is not None and not thana_text.split("/")[0].strip().isdigit():
            problems.append(f"Property {i} Thana No: not a number ({thana_text!r})")
    return problems


def _check_file(path, meta):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExtractionError(f"{path.name} is not valid JSON: {exc}") from exc
    problems = validate(data, meta)
    if problems:
        raise ExtractionError(f"{path.name} failed validation: " + "; ".join(problems))

def extracted_path(folder, meta):
    """The JSON lives in the deed's own folder."""
    return Path(folder) / f"{meta['volume_id']}_{meta['doct_no']}_{meta['presentation_year']}_extracted.json"


def adopt_legacy(target):
    """If the deed folder has no JSON, copy a non-empty one from returned_data or data_process/output."""
    if target.exists():
        return True
    if not LEGACY_FALLBACK:
        return False
    for src_dir in (RETURNED_DIR, PROCESSOR_DIR / "output"):
        src = src_dir / target.name
        if src.exists() and src.stat().st_size > 0:
            tmp = target.with_suffix(".json.tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, target)
            log(f"JSON: copied {src} into the deed folder {target.parent}")
            return True
    return False

def ensure_extracted(folder):
    """Return the deed folder's extracted JSON, running the processor if it is missing.

    Every returned file has passed validate(), including hand-edited ones.
    """
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    stem = f"{meta['volume_id']}_{meta['doct_no']}_{meta['presentation_year']}"

    target = extracted_path(folder, meta)
    if adopt_legacy(target):
        log(f"Extraction result already present: {target}")
        _check_file(target, meta)
        return target

    pdf = folder / f"{stem}.pdf"
    if not pdf.exists():
        raise ExtractionError(f"PDF not found: {pdf}")
    script = PROCESSOR_DIR / "data_processor.py"
    if not script.exists():
        raise ExtractionError(f"Processor not found: {script}")

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    last_error = "unknown"

    for attempt in range(1, ATTEMPTS + 1):
        target.unlink(missing_ok=True)
        log(f"Extracting {pdf.name} (attempt {attempt}/{ATTEMPTS})")
        try:
            result = subprocess.run(
                [_python(), str(script), "--input", str(pdf.resolve())],
                cwd=PROCESSOR_DIR, env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            last_error = f"processor exceeded {TIMEOUT_S}s"
        else:
            if result.returncode == 0 and target.exists():
                try:
                    _check_file(target, meta)
                except ExtractionError as exc:
                    last_error = str(exc)
                else:
                    log(f"Extraction saved in the deed folder: {target}")
                    return target
            else:
                tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-3:])
                last_error = f"exit code {result.returncode}: {tail}"
        log(f"Extraction attempt {attempt} failed: {last_error}")
        if attempt < ATTEMPTS:
            time.sleep(10 * attempt)

    raise ExtractionError(last_error)