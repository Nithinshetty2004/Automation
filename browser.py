import subprocess
import time
from urllib.request import urlopen

from config import CHROME_PATH, DEBUG_PORT, LOGIN_URL, PROFILE_DIR, TIMEOUT_MS
from util import log

CDP_HTTP = f"http://127.0.0.1:{DEBUG_PORT}"


def _cdp_up():
    try:
        urlopen(f"{CDP_HTTP}/json/version", timeout=1)
        return True
    except Exception:
        return False


def launch_or_attach(playwright):
    if _cdp_up():
        log(f"Attaching to Chrome already running on port {DEBUG_PORT}")
    else:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [
                CHROME_PATH,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                LOGIN_URL,
            ]
        )
        for _ in range(40):
            if _cdp_up():
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Chrome did not open the debugging port in 20s")
        log("Chrome launched")

    browser = playwright.chromium.connect_over_cdp(CDP_HTTP)
    context = browser.contexts[0]
    open_pages = [p for p in context.pages if not p.is_closed()]
    page = next((p for p in open_pages if "enibandhan.bihar.gov.in" in p.url), None) or context.new_page()
    page.set_default_timeout(TIMEOUT_MS)
    return browser, page