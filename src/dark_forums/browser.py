from __future__ import annotations

import random
import textwrap
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)

DEFAULT_VIEWPORT = {"width": 1536, "height": 864}
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "sec-ch-ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version": '"147.0.3912.72"',
    "sec-ch-ua-full-version-list": '"Microsoft Edge";v="147.0.3912.72", "Not.A/Brand";v="8.0.0.0", "Chromium";v="147.0.7727.102"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"19.0.0"',
    "Upgrade-Insecure-Requests": "1",
}
DEFAULT_INIT_SCRIPT = textwrap.dedent(
    """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
    Object.defineProperty(navigator, 'language', { get: () => 'zh-CN' });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
    Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0 });
    if (!window.chrome) {
        Object.defineProperty(window, 'chrome', {
            value: { runtime: {}, app: {}, csi: () => ({}), loadTimes: () => ({}) },
        });
    }
    const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
    if (originalQuery) {
        window.navigator.permissions.query = (parameters) => (
            parameters && parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    }
    """
).strip()


@dataclass
class BrowserSession:
    playwright: Playwright
    browser: Browser
    context: BrowserContext


def start_browser(headless: bool, storage_state_path: Path | None, proxy_server: str | None) -> BrowserSession:
    pw = sync_playwright().start()
    proxy = None
    if proxy_server:
        proxy = {"server": proxy_server}
    browser = pw.chromium.launch(
        headless=headless,
        proxy=proxy,
        args=[
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context_kwargs = {
        "ignore_https_errors": True,
        "user_agent": DEFAULT_USER_AGENT,
        "viewport": DEFAULT_VIEWPORT,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "extra_http_headers": DEFAULT_HEADERS,
        "device_scale_factor": 1.25,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
    }

    if storage_state_path and storage_state_path.exists():
        context = browser.new_context(storage_state=str(storage_state_path), **context_kwargs)
    else:
        context = browser.new_context(**context_kwargs)

    context.add_init_script(DEFAULT_INIT_SCRIPT)
    context.set_default_timeout(60_000)
    return BrowserSession(playwright=pw, browser=browser, context=context)


def stop_browser(session: BrowserSession) -> None:
    session.context.close()
    session.browser.close()
    session.playwright.stop()


def new_page(session: BrowserSession) -> Page:
    page = session.context.new_page()
    return page


def human_delay(min_s: float = 1.5, max_s: float = 6.0) -> None:
    import time

    time.sleep(random.uniform(min_s, max_s))
