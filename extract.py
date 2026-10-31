import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from util import log

PROCESSOR_DIR = Path(os.getenv("PROCESSOR_DIR", "data_process")).resolve()
RETURNED_DIR = Path(os.getenv("RETURNED_DIR", "returned_data"))
TIMEOUT_S = int(os.getenv("PROCESSOR_TIMEOUT_SECONDS", "300"))
ATTEMPTS = int(os.getenv("PROCESSOR_ATTEMPTS", "2"))

TOP_FIELDS = ["Presentation Date", "Registration Date", "Page No. From", "Page No. To"]
PARTY_FIELDS = ["Party Type", "Name", "Relation Name"]
PROPERTY_FIELDS = ["Circle", "Village/Thana", "Khata No", "Khasra No"]

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
    if presented and registered and registered < presented:
        problems.append("Registration Date is before Presentation Date")
    if presented and meta and str(presented.year) != str(meta.get("presentation_year")):
        problems.append(f"Presentation year {presented.year} differs from the portal's {meta.get('presentation_year')}")

    p_from, p_to = top["Page No. From"], top["Page No. To"]
    for f, v in (("Page No. From", p_from), ("Page No. To", p_to)):
        if v is not None and not v.isdigit():
            problems.append(f"{f}: not a number ({v!r})")
    if p_from and p_to and p_from.isdigit() and p_to.isdigit() and int(p_to) < int(p_from):
        problems.append("Page No. To is smaller than Page No. From")

    parties = data.get("Parties")
    if not isinstance(parties, list) or not parties:
        problems.append("Parties: missing or empty")
    else:
        executants = claimants = 0
        for i, party in enumerate(parties, 1):
            if not isinstance(party, dict):
                problems.append(f"Party {i}: not an object")
                continue
            for f in PARTY_FIELDS:
                if _text(party.get(f)) is None:
                    problems.append(f"Party {i} {f}: missing or unreadable")
            kind = (_text(party.get("Party Type")) or "").casefold()
            if kind == "executant":
                executants += 1
            elif kind == "claimant":
                claimants += 1
            elif kind:
                problems.append(f"Party {i}: Party Type must be Executant or Claimant, got {party.get('Party Type')!r}")
        if executants != 1:
            problems.append(f"Parties: expected exactly 1 Executant, found {executants}")
        if claimants < 1:
            problems.append("Parties: no Claimant found")

    props = data.get("Properties")
    if not isinstance(props, list) or not props:
        problems.append("Properties: missing or empty")
    else:
        for i, prop in enumerate(props, 1):
            if not isinstance(prop, dict):
                problems.append(f"Property {i}: not an object")
                continue
            for f in PROPERTY_FIELDS:
                if _text(prop.get(f)) is None:
                    problems.append(f"Property {i} {f}: missing or unreadable")
    return problems


def _check_file(path, meta):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExtractionError(f"{path.name} is not valid JSON: {exc}") from exc
    problems = validate(data, meta)
    if problems:
        raise ExtractionError(f"{path.name} failed validation: " + "; ".join(problems))


def ensure_extracted(folder):
    """Return returned_data/<volume>_<deed>_<year>_extracted.json, running the processor if needed.

    Every returned file has passed validate(), including hand-edited ones.
    """
    folder = Path(folder)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    stem = f"{meta['volume_id']}_{meta['doct_no']}_{meta['presentation_year']}"

    target = RETURNED_DIR / f"{stem}_extracted.json"
    if target.exists():
        log(f"Extraction result already present: {target}")
        _check_file(target, meta)  # delete the file to force a fresh extraction
        return target

    pdf = folder / f"{stem}.pdf"
    if not pdf.exists():
        raise ExtractionError(f"PDF not found: {pdf}")
    script = PROCESSOR_DIR / "data_processor.py"
    if not script.exists():
        raise ExtractionError(f"Processor not found: {script}")

    raw_out = PROCESSOR_DIR / "output" / f"{stem}_extracted.json"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    last_error = "unknown"

    for attempt in range(1, ATTEMPTS + 1):
        raw_out.unlink(missing_ok=True)
        log(f"Extracting {pdf.name} (attempt {attempt}/{ATTEMPTS})")
        try:
            result = subprocess.run(
                [_python(), str(script), str(pdf.resolve())],
                cwd=PROCESSOR_DIR,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            last_error = f"processor exceeded {TIMEOUT_S}s"
        else:
            if result.returncode == 0 and raw_out.exists():
                try:
                    _check_file(raw_out, meta)
                except ExtractionError as exc:
                    last_error = str(exc)
                else:
                    RETURNED_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(".json.tmp")
                    shutil.copyfile(raw_out, tmp)
                    os.replace(tmp, target)  # atomic: a half-written file is never read
                    log(f"Extraction saved: {target}")
                    return target
            else:
                tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-3:])
                last_error = f"exit code {result.returncode}: {tail}"
        log(f"Extraction attempt {attempt} failed: {last_error}")
        if attempt < ATTEMPTS:
            time.sleep(10 * attempt)

    raise ExtractionError(last_error)