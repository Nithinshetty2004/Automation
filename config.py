import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
]


def _required(name):
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing {name} in .env")
    return value


def _find_chrome():
    configured = os.getenv("CHROME_PATH")
    if configured:
        return configured
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    found = shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    sys.exit("Chrome not found. Set CHROME_PATH in .env")


BASE_URL = os.getenv("PORTAL_URL", "https://enibandhan.bihar.gov.in").rstrip("/")
LOGIN_URL = f"{BASE_URL}/users/login"
USERNAME = _required("PORTAL_USERNAME")
PASSWORD = _required("PORTAL_PASSWORD")

CHROME_PATH = _find_chrome()
DEBUG_PORT = int(os.getenv("DEBUG_PORT", "9222"))
PROFILE_DIR = Path(os.getenv("CHROME_PROFILE_DIR", "chrome_profile")).resolve()
OUT_DIR = Path(os.getenv("OUT_DIR", "downloads"))

TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))
LOGIN_WAIT_SECONDS = int(os.getenv("LOGIN_WAIT_SECONDS", "300"))
LOGIN_ATTEMPTS = int(os.getenv("LOGIN_ATTEMPTS", "3"))
AUTO_CAPTCHA = os.getenv("AUTO_CAPTCHA", "false").strip().lower() == "true"