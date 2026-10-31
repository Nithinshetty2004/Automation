class SessionLost(Exception):
    """The portal session ended; log in again and restart from the top."""


class StopRun(RuntimeError):
    """Stop the whole batch and report, never continue blindly."""


def session_lost(page):
    try:
        url = page.url
        if "users/login" in url or "sessionTimeout" in url:
            return True
        return not page.evaluate("sessionStorage.getItem('token')")
    except Exception:
        return False
