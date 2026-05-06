from __future__ import annotations

import random
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Page


def is_logged_in(page: Page) -> bool:
    html = page.content().lower()
    if "logout" in html:
        return True
    if "log out" in html:
        return True
    return False


def _is_browser_check(html: str) -> bool:
    h = (html or "").lower()
    if "checking your browser" in h:
        return True
    if "powered by" in h and "darkforums" in h and "please move your mouse" in h:
        return True
    return False


def _is_captcha_challenge(html: str) -> bool:
    h = (html or "").lower()
    if "captcha challenge" in h:
        return True
    if "please complete the following captcha" in h:
        return True
    if "g-recaptcha" in h:
        return True
    if "recaptcha" in h and "redirected once you have passed the challenge" in h:
        return True
    return False


def _try_bypass_browser_check(page: Page) -> bool:
    try:
        page.mouse.move(random.randint(10, 600), random.randint(10, 400))
        page.wait_for_timeout(300)
        page.mouse.move(random.randint(10, 600), random.randint(10, 400))
    except Exception:
        pass

    try:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(200)
        page.keyboard.press("ArrowUp")
    except Exception:
        pass

    try:
        page.wait_for_timeout(2_000)
        page.reload(wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(500)
    except Exception:
        pass

    try:
        return not _is_browser_check(page.content())
    except Exception:
        return False


def login(page: Page, base_url: str, username: str, password: str) -> None:
    login_url = urljoin(base_url, "/member.php?action=login")
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=90_000)
    except Exception:
        _dump_debug(page, "login_goto_failed")
        raise

    # Sometimes the site serves an anti-bot interstitial instead of the login form.
    for attempt in range(3):
        html = page.content()
        if not _is_browser_check(html):
            break
        _dump_debug(page, f"login_browser_check_{attempt}")
        if _try_bypass_browser_check(page):
            break
        page.wait_for_timeout(5_000)
    else:
        raise RuntimeError("Login blocked by browser_check")

    html = page.content()
    if _is_captcha_challenge(html):
        _dump_debug(page, "login_captcha_challenge")
        raise RuntimeError("Login blocked by captcha challenge")

    if is_logged_in(page):
        return

    page.wait_for_timeout(500)

    user_selectors = [
        "input[name='username']",
        "input[name='user']",
        "input[name='login']",
        "input[id*='username' i]",
        "input[id*='user' i]",
        "input[placeholder*='username' i]",
        "input[type='text'][autocomplete='username']",
    ]
    pass_selectors = [
        "input[name='password']",
        "input[id*='password' i]",
        "input[placeholder*='password' i]",
        "input[type='password']",
    ]

    user_filled = False
    user_input = None
    for sel in user_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            user_input = loc.first
            user_input.fill(username)
            user_filled = True
            break

    if not user_filled:
        _dump_debug(page, "login_username_not_found")
        html = page.content()
        if _is_captcha_challenge(html):
            raise RuntimeError("Login blocked by captcha challenge")
        raise RuntimeError("Login page: username input not found")

    pass_filled = False
    pass_input = None
    for sel in pass_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            pass_input = loc.first
            pass_input.fill(password)
            pass_filled = True
            break

    if not pass_filled:
        _dump_debug(page, "login_password_not_found")
        html = page.content()
        if _is_captcha_challenge(html):
            raise RuntimeError("Login blocked by captcha challenge")
        raise RuntimeError("Login page: password input not found")

    login_form = None
    if user_input is not None:
        try:
            login_form = user_input.locator("xpath=ancestor::form[1]")
        except Exception:
            login_form = None
    if login_form is None and pass_input is not None:
        try:
            login_form = pass_input.locator("xpath=ancestor::form[1]")
        except Exception:
            login_form = None

    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Log in')",
        "button:has-text('Login')",
        "input[type='submit'][value*='Login' i]",
        "input[type='submit'][value*='Log' i]",
    ]

    clicked = False
    if login_form is not None and login_form.count() > 0:
        for sel in submit_selectors:
            loc = login_form.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                clicked = True
                break

    if not clicked:
        for sel in submit_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                clicked = True
                break

    if not clicked:
        _dump_debug(page, "login_submit_not_found")
        raise RuntimeError("Login page: submit button not found")

    page.wait_for_timeout(500)

    try:
        page.wait_for_load_state("domcontentloaded", timeout=90_000)
    except Exception:
        _dump_debug(page, "login_wait_domcontentloaded_failed")
        raise

    try:
        page.wait_for_url("**/member.php?action=login", timeout=2_000)
    except Exception:
        pass

    if not is_logged_in(page):
        _dump_debug(page, "login_failed")
        raise RuntimeError("Login failed: did not detect logged-in state")


def _dump_debug(page: Page, name: str) -> None:
    try:
        root = Path.cwd()
        out_dir = root / "data" / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)

        html_path = out_dir / f"{name}.html"
        html_path.write_text(page.content(), encoding="utf-8")

        try:
            page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
        except Exception:
            pass
    except Exception:
        pass


def save_storage_state(page: Page, storage_state_path: Path) -> None:
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(storage_state_path))
