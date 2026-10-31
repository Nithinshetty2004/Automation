import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types, errors


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
PROMPT_FILE = BASE_DIR / "prompt.txt"

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

PRIMARY_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
    if model.strip()
]

MAX_ATTEMPTS = 4
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

# Output schema: flat top-level fields plus two lists of objects.
TOP_FIELDS = [
    "Presentation Date",
    "Registration Date",
    "Page No. From",
    "Page No. To",
]
PARTY_FIELDS = ["Party Type", "Name", "Relation Name"]
PROPERTY_FIELDS = ["Circle", "Village/Thana", "Khata No", "Khasra No"]

NOT_A_VALUE_PREFIX = ("unclear", "unreadable", "illegible", "not found", "not available")
NOT_A_VALUE_EXACT = {"n/a", "na", "null", "none", "-"}


# ============================================================
# HELPERS
# ============================================================

def log(message):
    print(message, flush=True)


def load_prompt():
    """Read the extraction instructions from prompt.txt."""
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_FILE}")

    prompt = PROMPT_FILE.read_text(encoding="utf-8").strip()

    if not prompt:
        raise ValueError("prompt.txt is empty.")

    return prompt


def find_pdf(pdf_argument=None):
    """Use the supplied PDF path or the first PDF in input/."""

    if pdf_argument:
        pdf_path = Path(pdf_argument).expanduser()

        if not pdf_path.is_absolute():
            pdf_path = (Path.cwd() / pdf_path).resolve()

        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        if pdf_path.suffix.lower() != ".pdf":
            raise ValueError("The supplied file must be a PDF.")

        return pdf_path

    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(
        path for path in INPUT_DIR.iterdir()
        if path.is_file() and path.suffix.lower() == ".pdf"
    )

    if not pdf_files:
        raise FileNotFoundError(
            f"No PDF found in {INPUT_DIR}\n"
            "Place your PDF in the input folder or pass its path "
            "as a command-line argument."
        )

    if len(pdf_files) > 1:
        log(f"Found {len(pdf_files)} PDFs. Using the first: {pdf_files[0].name}")

    return pdf_files[0]


def get_year_from_filename(filename):
    """Trailing four-digit year of a filename such as 206639_113_1921.pdf (context only)."""
    match = re.search(r"(\d{4})$", Path(filename).stem)
    return match.group(1) if match else None


def build_prompt(base_prompt, pdf_path):
    """Add runtime context without treating filenames as evidence."""

    filename_year = get_year_from_filename(pdf_path.name)

    context = (
        "\n\nIMPORTANT EXTRACTION RULES:\n"
        "1. Read the document itself as the primary source of truth.\n"
        "2. The document may contain Urdu, Hindi, English, "
        "handwriting, stamps, or mixed scripts.\n"
        "3. Translate or transliterate names and labels into English "
        "where possible, preserving their meaning.\n"
        "4. Do not invent legal names, dates, registration numbers, "
        "property identifiers, or other factual values.\n"
        "5. If a single value cannot be read, use null for that value only. "
        "Never use placeholder text such as 'Unreadable' or 'N/A'.\n"
        "6. Do not treat a filename, a guessed value, or a typical "
        "regional practice as proof of a document fact.\n"
        "7. Follow the JSON structure given in the instructions above exactly. "
        "Top-level keys: " + json.dumps(TOP_FIELDS + ["Parties", "Properties"], ensure_ascii=False) + ". "
        "Each Parties entry has exactly the keys " + json.dumps(PARTY_FIELDS, ensure_ascii=False) + ". "
        "Each Properties entry has exactly the keys " + json.dumps(PROPERTY_FIELDS, ensure_ascii=False) + ".\n"
    )

    if filename_year:
        context += (
            "\nFilename context: the filename contains the year "
            f"{filename_year}. This is not verified document evidence. "
            "Do not use it as a field value unless the document itself "
            "supports that value.\n"
        )

    context += (
        "\nDo not wrap the JSON in Markdown fences. "
        "Do not include explanations outside the JSON object."
    )

    return base_prompt + context


def extract_json(text):
    """Parse JSON, tolerating Markdown code fences if returned."""

    if not text or not text.strip():
        raise ValueError("Gemini returned an empty response.")

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end <= start:
            raise ValueError(
                "Gemini did not return a valid JSON object.\n"
                f"Response received:\n{cleaned[:1500]}"
            )

        data = json.loads(cleaned[start:end + 1])

    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object containing the extracted fields.")

    return data


def _clean_value(value):
    """String or None. Placeholder text counts as None so it can never reach the portal."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    low = text.lower()
    if not text or low in NOT_A_VALUE_EXACT or low.startswith(NOT_A_VALUE_PREFIX):
        return None
    return text


def _pick(source, fields):
    """Copy the wanted fields from a dict, matching keys case-insensitively. Missing -> None."""
    lookup = {str(k).strip().casefold(): k for k in source}
    out = {}
    for field in fields:
        key = lookup.get(field.casefold())
        out[field] = _clean_value(source[key]) if key is not None else None
    return out


def _pick_list(data, name, fields):
    lookup = {str(k).strip().casefold(): k for k in data}
    key = lookup.get(name.casefold())
    items = data[key] if key is not None else None
    if not isinstance(items, list):
        return []
    return [_pick(item, fields) for item in items if isinstance(item, dict)]


def normalize_data(data):
    """Return the fixed structure. Missing values stay None; nothing is fabricated."""
    result = _pick(data, TOP_FIELDS)
    result["Parties"] = _pick_list(data, "Parties", PARTY_FIELDS)
    result["Properties"] = _pick_list(data, "Properties", PROPERTY_FIELDS)
    return result


def is_retryable_error(exc):
    """Identify transient API errors that are safe to retry."""

    status_code = getattr(exc, "code", None)

    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        return False

    return status_code in RETRYABLE_STATUS_CODES


# ============================================================
# GEMINI API
# ============================================================

def generate_with_retry(client, model_name, uploaded_file, prompt):
    """Retry temporary Gemini failures with exponential backoff."""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return client.models.generate_content(
                model=model_name,
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )

        except errors.APIError as exc:
            if not is_retryable_error(exc):
                raise

            status_code = getattr(exc, "code", "unknown")

            if attempt == MAX_ATTEMPTS:
                log(f"Model {model_name} failed after {MAX_ATTEMPTS} attempts (HTTP {status_code}).")
                raise

            wait_seconds = 2 ** attempt
            log(
                f"Temporary API error {status_code} for {model_name}. "
                f"Retrying in {wait_seconds}s (attempt {attempt + 1}/{MAX_ATTEMPTS})..."
            )
            time.sleep(wait_seconds)


def analyze_pdf(client, pdf_path, prompt):
    """Upload the PDF and try the configured model list."""

    model_candidates = list(dict.fromkeys([PRIMARY_MODEL] + FALLBACK_MODELS))

    log(f"Uploading PDF: {pdf_path.name}")

    uploaded_file = client.files.upload(
        file=str(pdf_path),
        config=types.UploadFileConfig(
            mime_type="application/pdf",
            display_name=pdf_path.name,
        ),
    )

    log("PDF uploaded successfully.")

    errors_seen = []

    try:
        for model_name in model_candidates:
            log(f"Analyzing with model: {model_name}")

            try:
                response = generate_with_retry(
                    client=client,
                    model_name=model_name,
                    uploaded_file=uploaded_file,
                    prompt=prompt,
                )

                response_text = getattr(response, "text", None)

                if not response_text:
                    raise ValueError(f"Model {model_name} returned no text.")

                log(f"Extraction succeeded using {model_name}.")
                return response_text

            except Exception as exc:
                errors_seen.append((model_name, exc))
                log(f"Model {model_name} failed: {type(exc).__name__}: {exc}")
                continue

        summary = "\n".join(
            f"- {model}: {type(exc).__name__}: {exc}"
            for model, exc in errors_seen
        )

        raise RuntimeError(
            "All configured Gemini models failed.\n"
            "Check model availability, API quota, and network access.\n"
            f"{summary}"
        )

    finally:
        try:
            client.files.delete(name=uploaded_file.name)
            log("Temporary Gemini upload cleaned up.")
        except Exception as exc:
            log(f"Note: temporary upload cleanup did not complete: {exc}")


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_KEY or API_KEY == "paste_your_api_key_here":
        raise ValueError(
            "GEMINI_API_KEY is missing.\n"
            "Set it in your local .env file. "
            "Do not share your API key."
        )

    pdf_argument = sys.argv[1] if len(sys.argv) > 1 else None
    pdf_path = find_pdf(pdf_argument)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    base_prompt = load_prompt()
    prompt = build_prompt(base_prompt, pdf_path)

    client = genai.Client(api_key=API_KEY)

    response_text = analyze_pdf(client=client, pdf_path=pdf_path, prompt=prompt)

    extracted_data = extract_json(response_text)
    normalized_data = normalize_data(extracted_data)

    output_path = OUTPUT_DIR / f"{pdf_path.stem}_extracted.json"

    output_path.write_text(
        json.dumps(normalized_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    log("\nExtraction complete.")
    log(f"Output saved to: {output_path}")

    log("\nExtracted fields:")
    print(json.dumps(normalized_data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("\nOperation cancelled by user.")
        sys.exit(130)
    except Exception as exc:
        log(f"\nERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)