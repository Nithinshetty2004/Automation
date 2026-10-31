# Deed OCR with Gemini API

Python starter that sends scanned PDFs directly to Gemini for multilingual document understanding, English translation/transliteration, and extraction of 13 fields into JSON. Tesseract is not used.

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
python main.py
```
or:
```powershell
python main.py "input\\1950_5024_1989.pdf"
```
The result is saved to `output/<pdf-name>_extracted.json`.

## Customize
Edit `prompt.txt`. The script passes the PDF filename and extracts a final four-digit year from the filename stem when available. This is fallback context only; readable document dates take priority.

## Notes
- API access, quotas, and billing/free-tier availability depend on your Google AI Studio project and current Google terms.
- Gemini Files API uploads are temporary; Google's documentation states uploaded files are stored for 48 hours.
- Review legal/land-record data before submitting official forms. Generated values are labelled as synthetic or unclear rather than represented as verified facts.
- Only upload documents to an external API if you are authorized to share them.
- This starter extracts fields to JSON only. Browser form filling/submission remains a separate next step.
