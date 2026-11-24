import os
import io
import re
import json
import time
import csv
import argparse
import hashlib
from pathlib import Path

import pymupdf as fitz
from PIL import Image
from dotenv import load_dotenv
from google import genai
from google.genai import types


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

DOWNLOADS_DIR = PROJECT_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "output"
PROMPT_FILE = BASE_DIR / "prompt.txt"

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("GEMINI_API_KEY", "")


# ============================================================
# SETTINGS
# ============================================================

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_DPI = 200
JPEG_QUALITY = 85

USD_TO_INR = 87.0

# Cached input is billed at a reduced rate. 10% is an assumption;
# verify on Google's pricing page. Cache storage is not included.
CACHE_READ_FACTOR = 0.10

# Hard cap on the reply. The reply is a few short lines.
MAX_OUTPUT_TOKENS = 700

# Configured pricing in USD per 1 million tokens.
# Verify official prices before using for actual billing estimates.
MODEL_PRICES = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50},
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3.6-flash": {"input": 0.75, "output": 3.75},
}

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 3

USER_TEXT = (
    "Image 1 is the full first page of the deed. {second}"
    "Extract the values and answer only with the required lines."
)
SECOND_MULTI = (
    "Image 2 is the left strip of the last page "
    "(use it for the Registration Date). "
)
SECOND_SINGLE = "The deed has one page, so there is no second image. "


# ============================================================
# PROMPT
# ============================================================

def load_prompt():
    if not PROMPT_FILE.exists():
        raise FileNotFoundError(f"Prompt file not found: {PROMPT_FILE}")

    prompt = PROMPT_FILE.read_text(encoding="utf-8").strip()

    if not prompt:
        raise ValueError("prompt.txt is empty")

    return prompt


# ============================================================
# PDF RENDERING
# First page: full page. Last page: left strip (multi-page only).
# ============================================================

def render_jpeg(doc, idx, dpi=DEFAULT_DPI):
    pix = doc[idx].get_pixmap(
        matrix=fitz.Matrix(dpi / 72, dpi / 72),
        colorspace=fitz.csRGB,
        alpha=False,
    )

    return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)


def render_last_page_left_crop(doc, idx, dpi=DEFAULT_DPI):
    """Left 25% of the page width, from 10% of the height to the bottom."""

    pix = doc[idx].get_pixmap(
        matrix=fitz.Matrix(dpi / 72, dpi / 72),
        colorspace=fitz.csRGB,
        alpha=False,
    )

    image = Image.open(io.BytesIO(pix.tobytes("png")))

    width, height = image.size

    cropped = image.crop((0, int(height * 0.10), int(width * 0.25), height))

    buffer = io.BytesIO()
    cropped.save(buffer, format="JPEG", quality=JPEG_QUALITY)

    return buffer.getvalue()


def prepare_pdf_images(pdf_path, dpi=DEFAULT_DPI):
    doc = fitz.open(pdf_path)

    try:
        page_count = len(doc)

        if page_count == 0:
            raise ValueError("PDF contains no pages")

        images = [{"name": "first_page_full", "data": render_jpeg(doc, 0, dpi)}]

        if page_count > 1:
            images.append({
                "name": "last_page_left_crop",
                "data": render_last_page_left_crop(doc, page_count - 1, dpi),
            })
    finally:
        doc.close()

    return images, page_count


def build_contents(images):
    contents = [
        types.Part.from_bytes(data=image["data"], mime_type="image/jpeg")
        for image in images
    ]

    second = SECOND_MULTI if len(images) > 1 else SECOND_SINGLE
    contents.append(USER_TEXT.format(second=second))

    return contents


# ============================================================
# PROMPT CACHE
# Only the prompt is cached. It is identical for every deed, so
# the cache is found again by name on the next run and reused.
# The page images are different every time and are never cached.
# ============================================================

def cache_key(model, prompt):
    digest = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:10]
    return f"deed-{model}-{digest}"


def get_or_create_cache(client, model, prompt, cache_hours=1.0):
    key = cache_key(model, prompt)
    ttl = f"{int(cache_hours * 3600)}s"

    try:
        for cache in client.caches.list():
            if getattr(cache, "display_name", None) == key:
                try:
                    client.caches.update(
                        name=cache.name,
                        config=types.UpdateCachedContentConfig(ttl=ttl),
                    )
                except Exception:
                    pass

                print(f"Cache reused: {cache.name}")
                return cache.name

    except Exception as e:
        print(f"Could not list caches: {str(e)[:150]}")

    try:
        count = client.models.count_tokens(model=model, contents=prompt)
        print(f"Prompt tokens: {count.total_tokens}")
    except Exception:
        pass

    try:
        cache = client.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(
                system_instruction=prompt,
                ttl=ttl,
                display_name=key,
            ),
        )

        print(f"Cache created: {cache.name}")
        return cache.name

    except Exception as e:
        print(f"Cache not used ({str(e)[:200]})")
        print("Continuing without explicit cache.")
        return None


def delete_cache(client, cache_name):
    if not cache_name:
        return

    try:
        client.caches.delete(name=cache_name)
        print(f"Cache deleted: {cache_name}")

    except Exception as e:
        print(f"Cache deletion failed: {e}")


# ============================================================
# GEMINI REQUEST (thinking off, plain-text reply)
# ============================================================

def build_configs(prompt, cache_name):
    base = {"temperature": 0, "max_output_tokens": MAX_OUTPUT_TOKENS}

    if cache_name:
        base["cached_content"] = cache_name
    else:
        base["system_instruction"] = prompt

    thinking_options = [
        lambda: types.ThinkingConfig(thinking_level="minimal"),
        lambda: types.ThinkingConfig(thinking_budget=0),
        None,
    ]

    configs = []

    for make in thinking_options:
        kwargs = dict(base)

        if make is not None:
            try:
                kwargs["thinking_config"] = make()
            except Exception:
                continue

        try:
            configs.append(types.GenerateContentConfig(**kwargs))
        except Exception:
            continue

    return configs


def call_model(client, model, prompt, contents, cache_name=None):
    last_error = None

    for config in build_configs(prompt, cache_name):
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                print(f"API attempt {attempt}/{MAX_ATTEMPTS}")

                return client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )

            except Exception as e:
                last_error = e
                message = str(e).lower()

                print(f"API attempt failed: {e}")

                # The cache expired or was deleted: retry once without it.
                if cache_name and "cachedcontent" in message.replace(" ", ""):
                    print("Cache unavailable, retrying without it.")
                    return call_model(client, model, prompt, contents, None)

                # This model rejected the thinking setting: try the next config.
                if "thinking" in message:
                    break

                if attempt < MAX_ATTEMPTS:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
        else:
            raise RuntimeError(
                f"Gemini API failed after {MAX_ATTEMPTS} attempts: {last_error}"
            )

    raise RuntimeError(f"Gemini API failed: {last_error}")


# ============================================================
# VALUES-ONLY REPLY -> THE JSON THE APP EXPECTS
#
# PF|<page no from>
# PD|<presentation date>
# RD|<registration date>
# E|<name>|<relation>
# C|<name>|<relation>
# P|<thana name>|<khata no>|<khasra no>
#
# The saved JSON keeps the structure extract.py and Returned.py read.
# Thana No is not asked for, so its slot is left empty and the app
# looks it up from the Thana Name.
# ============================================================

EMPTY_WORDS = {"null", "none", "n/a", "na", "nil", "-"}


def _clean(value):
    text = (value or "").strip().strip('"').strip()
    return "" if text.lower() in EMPTY_WORDS else text


def parse_response(response_text):
    text = (response_text or "").strip()

    if not text:
        raise ValueError("Gemini returned an empty response")

    text = text.replace("```", "")

    data = {
        "Presentation Date": "",
        "Registration Date": "",
        "Page No. From": "",
        "Parties": [],
        "Properties": [],
    }

    recognised = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if "|" not in line:
            continue

        tag, *values = [part.strip() for part in line.split("|")]
        tag = tag.upper()
        first = _clean(values[0]) if values else ""

        if tag == "PF":
            match = re.search(r"\d+", first)
            data["Page No. From"] = match.group(0) if match else ""

        elif tag == "PD":
            data["Presentation Date"] = first

        elif tag == "RD":
            data["Registration Date"] = first

        elif tag in ("E", "C"):
            values = (values + ["", ""])[:2]
            data["Parties"].append([
                "Executant" if tag == "E" else "Claimant",
                _clean(values[0]),
                _clean(values[1]),
            ])

        elif tag == "P":
            if data["Properties"]:
                print("WARNING: extra P line ignored (one property expected)")
                continue

            values = (values + ["", "", ""])[:3]
            data["Properties"].append([
                _clean(values[0]),
                "",
                _clean(values[1]),
                _clean(values[2]),
            ])

        else:
            continue

        recognised += 1

    if recognised == 0:
        raise ValueError(f"No recognised lines in the reply: {text[:200]!r}")

    return data


# ============================================================
# TOKEN USAGE AND COST
# ============================================================

def get_usage(response):
    usage = getattr(response, "usage_metadata", None)

    def field(name):
        return (getattr(usage, name, 0) or 0) if usage is not None else 0

    input_tokens = field("prompt_token_count")
    cached_tokens = field("cached_content_token_count")
    output_tokens = field("candidates_token_count")
    thinking_tokens = field("thoughts_token_count")
    total_tokens = field("total_token_count")

    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens + thinking_tokens

    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": thinking_tokens,
        "billable_output_tokens": output_tokens + thinking_tokens,
        "total_tokens": total_tokens,
    }


def calculate_cost(model, usage):
    prices = MODEL_PRICES.get(model)

    if prices is None:
        return None

    fresh_input = usage["input_tokens"] - usage["cached_tokens"]

    input_usd = (
        fresh_input * prices["input"]
        + usage["cached_tokens"] * prices["input"] * CACHE_READ_FACTOR
    ) / 1_000_000

    output_usd = usage["billable_output_tokens"] * prices["output"] / 1_000_000

    total_usd = input_usd + output_usd

    return {
        "input_usd": input_usd,
        "output_usd": output_usd,
        "total_usd": total_usd,
        "total_inr": total_usd * USD_TO_INR,
    }


# ============================================================
# SAVE
# ============================================================

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def append_usage_csv(output_dir, model, result):
    usage_csv = output_dir / f"{model}_usage.csv"
    is_new = not usage_csv.exists() or usage_csv.stat().st_size == 0

    usage = result["usage"]
    cost = result["cost"] or {}

    with open(usage_csv, "a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as f:
        writer = csv.writer(f)

        if is_new:
            writer.writerow([
                "PDF", "Model", "Input Tokens", "Cached Tokens",
                "Output Tokens", "Thinking Tokens", "Total Tokens",
                "Final Cost USD", "Final Cost INR", "Time Seconds",
            ])

        writer.writerow([
            result["pdf"], result["model"],
            usage["input_tokens"], usage["cached_tokens"],
            usage["output_tokens"], usage["thinking_tokens"],
            usage["total_tokens"],
            cost.get("total_usd", ""), cost.get("total_inr", ""),
            round(result["elapsed_seconds"], 2),
        ])

    return usage_csv


# ============================================================
# PROCESS ONE PDF
# ============================================================

def process_pdf(client, pdf_path, model, output_dir, dpi, cache_name):
    pdf_path = Path(pdf_path)

    print("\n" + "=" * 70)
    print(f"PDF   : {pdf_path.name}")
    print(f"MODEL : {model}")
    print("=" * 70)

    start_time = time.time()

    prompt = load_prompt()
    images, page_count = prepare_pdf_images(pdf_path, dpi)

    print(f"Pages in PDF    : {page_count}")
    print(f"Images prepared : {len(images)}")

    response = call_model(
        client=client,
        model=model,
        prompt=prompt,
        contents=build_contents(images),
        cache_name=cache_name,
    )

    extracted_data = parse_response(response.text)

    usage = get_usage(response)
    cost = calculate_cost(model, usage)

    deed_output_path = pdf_path.parent / f"{pdf_path.stem}_extracted.json"
    central_output_path = output_dir / model / f"{pdf_path.stem}.json"

    save_json(deed_output_path, extracted_data)
    save_json(central_output_path, extracted_data)

    elapsed = time.time() - start_time

    print("\nEXTRACTED DATA:")
    print(json.dumps(extracted_data, indent=2, ensure_ascii=False))

    print("\nTOKEN USAGE")
    print("-" * 40)
    print(f"Input tokens    : {usage['input_tokens']:,}")
    print(f"Cached tokens   : {usage['cached_tokens']:,}")
    print(f"Output tokens   : {usage['output_tokens']:,}")
    print(f"Thinking tokens : {usage['thinking_tokens']:,}")

    if cost:
        print(f"Cost USD        : {cost['total_usd']:.8f}")
        print(f"Cost INR        : {cost['total_inr']:.6f}")
    else:
        print("Pricing not configured for this model.")

    print(f"\nTime taken  : {elapsed:.2f} seconds")
    print(f"Saved deed  : {deed_output_path}")
    print(f"Saved output: {central_output_path}")

    return {
        "pdf": pdf_path.name,
        "model": model,
        "data": extracted_data,
        "usage": usage,
        "cost": cost,
        "elapsed_seconds": elapsed,
    }


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="PDF deed extraction using Gemini")

    parser.add_argument("--input", default=str(DOWNLOADS_DIR),
                        help="PDF file or directory containing PDFs")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Gemini model ID")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="PDF rendering DPI")
    parser.add_argument("--no-cache", action="store_true", help="Disable the prompt cache")
    parser.add_argument("--cache-hours", type=float, default=1.0,
                        help="Cache lifetime. Extended on every run.")
    parser.add_argument("--delete-cache", action="store_true",
                        help="Delete the prompt cache after processing")

    args = parser.parse_args()

    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY missing from .env")

    input_path = Path(args.input)

    if not input_path.is_absolute():
        input_path = (Path.cwd() / input_path).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        pdfs = [input_path]
    elif input_path.is_dir():
        pdfs = sorted(
            p for p in input_path.rglob("*")
            if p.is_file() and p.suffix.lower() == ".pdf"
        )
    else:
        pdfs = []

    if not pdfs:
        print(f"No PDFs found in {input_path}")
        return

    client = genai.Client(api_key=API_KEY)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nFound {len(pdfs)} PDF(s)")
    print(f"Model      : {args.model}")
    print(f"Cache      : {'Disabled' if args.no_cache else 'Enabled'}")

    cache_name = None

    if not args.no_cache:
        cache_name = get_or_create_cache(
            client, args.model, load_prompt(), args.cache_hours
        )

    failures = 0
    total_inr = 0.0

    try:
        for pdf in pdfs:
            try:
                result = process_pdf(client, pdf, args.model, OUTPUT_DIR, args.dpi, cache_name)
                append_usage_csv(OUTPUT_DIR, args.model, result)
                total_inr += (result["cost"] or {}).get("total_inr", 0)

            except Exception as e:
                failures += 1
                print(f"\nFAILED: {pdf.name}")
                print(f"Error: {e}")

    finally:
        if args.delete_cache:
            delete_cache(client, cache_name)

    print("\n" + "=" * 70)
    print("PROCESSING COMPLETED")
    print("=" * 70)
    print(f"Succeeded : {len(pdfs) - failures}")
    print(f"Failed    : {failures}")
    print(f"Total INR : {total_inr:.6f}")

    # extract.py reads the exit code, so a failed deed must not exit 0.
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()