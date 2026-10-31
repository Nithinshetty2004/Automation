import time

from config import LOGIN_ATTEMPTS, LOGIN_URL, LOGIN_WAIT_SECONDS, PASSWORD, USERNAME
from util import log


def _token(page):
    try:
        return page.evaluate("sessionStorage.getItem('token')")
    except Exception:
        return None


def _wait_for_login(page):
    """True on success, False if the portal bounced back to a blank login page."""
    deadline = time.time() + LOGIN_WAIT_SECONDS
    while time.time() < deadline:
        if _token(page):
            page.wait_for_url(lambda url: "users/login" not in url)
            page.wait_for_load_state("domcontentloaded")
            return True
        try:
            if page.locator("#user_name").count() and page.input_value("#user_name") == "":
                return False
        except Exception:
            pass
        page.wait_for_timeout(1000)
    raise TimeoutError(f"Login not completed within {LOGIN_WAIT_SECONDS}s")


def login(page, force=False):
    if not force and "enibandhan.bihar.gov.in" in page.url and _token(page):
        log("Already logged in")
        return

    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        page.goto(LOGIN_URL)
        page.evaluate("sessionStorage.removeItem('token'); sessionStorage.removeItem('refresh_token')")
        page.wait_for_selector("#user_name")
        page.fill("#user_name", USERNAME)
        page.fill("#password", PASSWORD)
        page.press("#password", "Tab")  # blur fires onchange, which refreshes the CAPTCHA
        page.wait_for_function("document.getElementById('captchagenerate').src.length > 0")
        page.wait_for_timeout(1500)  # let the refreshed CAPTCHA image settle
        page.focus("#txtCaptcha")
        log(f"Attempt {attempt}/{LOGIN_ATTEMPTS}: type the CAPTCHA and click LOGIN in the browser")

        if _wait_for_login(page):
            log("Login successful")
            return
        log("Login rejected by the portal, retrying")

    raise RuntimeError("Login failed after all attempts")
