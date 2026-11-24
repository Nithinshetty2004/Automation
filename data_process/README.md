# Deed OCR with Gemini API

Python OCR pipeline that sends scanned PDFs to Gemini for multilingual document understanding, English transliteration, and extraction of deed dates, parties, and property identifiers into JSON. Tesseract is not used.

## Requirements
- Windows 10/11
- Python 3.10+ (Python 3.11 recommended)
- Gemini API key from Google AI Studio
- Internet connection and an authorized PDF

## Setup (PowerShell)
1. Extract this ZIP and open PowerShell in the folder.
2. Create and activate a virtual environment:
   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
   If activation is blocked, use `.venv\Scripts\python.exe` directly.
3. Install dependencies:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```
4. Create your `.env`:
   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```
5. Put your Gemini API key in `.env`. Never share the key or commit `.env`.
6. Place a PDF in `input`, or pass a path to the script.

## Run
```powershell
python data_processor.py
```
or:
```powershell
python data_processor.py --input "C:\path\to\1950_5024_1989.pdf"
```
The result is saved beside the PDF as `<pdf-name>_extracted.json` and in
`output/<model>/<pdf-name>.json`.

## Customize
Edit `prompt.txt` to change the extraction instructions. The script processes the first page and a crop from the last page.

## Notes
- API access, quotas, and billing/free-tier availability depend on your Google AI Studio project and current Google terms.
- Gemini Files API uploads are temporary; Google's documentation states uploaded files are stored for 48 hours.
- Review legal/land-record data before submitting official forms. Generated values are labelled as synthetic or unclear rather than represented as verified facts.
- Only upload documents to an external API if you are authorized to share them.

## Automation integration

The root application calls `data_processor.py --input <pdf>` through
`extract.py`. It validates the returned JSON, then `Returned.py` maps the
`Parties` and `Properties` positional arrays into named records for the browser
form-filling code. The saved JSON keeps the processor's original array structure.

Page numbers are not extracted by Gemini. The browser flow counts the PDF pages,
starts the first deed at page 1, and assigns each later deed a range immediately
after the highest range already recorded for that volume. If the portal rejects
a range, the existing conflict handler advances it and records the rejected
range.

The extracted `Thana Name` is matched against `Village Name` in `THANA_NO.xlsx`,
scoped to the SRO from `meta.json` or `details.json`. A missing or ambiguous
match is held for review rather than resolved against another SRO.
Submission remains controlled by the root application's run settings.
