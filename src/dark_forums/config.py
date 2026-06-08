from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _first_nonempty(*values: str) -> str:
    for value in values:
        if value is not None:
            v = str(value).strip()
            if v:
                return v
    return ""


@dataclass(frozen=True)
class Settings:
    base_url: str
    leaks_url: str
    forum_urls: tuple[str, ...]
    full_site_mode: bool
    latest_page_only: bool
    username: str
    password: str
    proxy_server: str
    only_today: bool
    forum_sort_query: str
    max_pages_per_forum: int
    max_age_hours: int
    headless: bool
    reply_templates: tuple[str, ...]
    max_threads_per_day: int
    scrape_workers: int
    feishu_enabled: bool
    feishu_app_id: str
    feishu_app_secret: str
    feishu_chat_id: str
    feishu_max_posts_per_run: int
    dingtalk_enabled: bool
    dingtalk_webhook: str
    dingtalk_secret: str
    dingtalk_max_posts_per_run: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_use_proxy: bool
    llm_proxy_server: str
    data_dir: Path
    logs_dir: Path


def load_settings(project_root: Path) -> Settings:
    load_dotenv(project_root / ".env")
    load_dotenv(project_root / ".env.local", override=True)

    base_url = os.getenv("DARKFORUMS_BASE_URL", "").strip()
    leaks_url = os.getenv("DARKFORUMS_LEAKS_URL", "").strip()
    forum_urls_raw = os.getenv("DARKFORUMS_FORUM_URLS", "").strip()
    full_site_mode = os.getenv("DARKFORUMS_FULL_SITE_MODE", "0").strip() not in {"0", "false", "False", ""}
    latest_page_only = os.getenv("DARKFORUMS_LATEST_PAGE_ONLY", "0").strip() not in {"0", "false", "False", ""}
    username = os.getenv("DARKFORUMS_USERNAME", "").strip()
    password = os.getenv("DARKFORUMS_PASSWORD", "").strip()

    proxy_server = os.getenv("DARKFORUMS_PROXY_SERVER", "http://127.0.0.1:7890").strip()

    only_today = os.getenv("DARKFORUMS_ONLY_TODAY", "1").strip() not in {"0", "false", "False"}

    forum_sort_query = os.getenv(
        "DARKFORUMS_FORUM_SORT_QUERY",
        "sortby=started&order=desc&datecut=9999&prefix=0",
    ).strip()

    max_pages_per_forum = int(os.getenv("DARKFORUMS_MAX_PAGES_PER_FORUM", "2").strip())

    max_age_hours = int(os.getenv("DARKFORUMS_MAX_AGE_HOURS", "24").strip())

    headless = os.getenv("DARKFORUMS_HEADLESS", "1").strip() not in {"0", "false", "False"}
    reply_templates_raw = os.getenv("DARKFORUMS_REPLY_TEMPLATES", "Thanks").strip()
    reply_templates = tuple([s.strip() for s in reply_templates_raw.split("|") if s.strip()]) or ("Thanks",)

    max_threads_per_day = int(os.getenv("DARKFORUMS_MAX_THREADS_PER_DAY", "200").strip())
    scrape_workers = max(1, int(os.getenv("DARKFORUMS_SCRAPE_WORKERS", "1").strip()))

    feishu_enabled = os.getenv("FEISHU_ENABLED", "0").strip() not in {"0", "false", "False", ""}
    feishu_app_id = os.getenv("FEISHU_APP_ID", "").strip()
    feishu_app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    feishu_chat_id = os.getenv("FEISHU_CHAT_ID", "").strip()
    feishu_max_posts_per_run = int(os.getenv("FEISHU_MAX_POSTS_PER_RUN", "20").strip())

    if feishu_enabled and not (feishu_app_id and feishu_app_secret and feishu_chat_id):
        raise ValueError("Feishu enabled but FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_CHAT_ID not set")

    dingtalk_enabled = os.getenv("DINGTALK_ENABLED", "0").strip() not in {"0", "false", "False", ""}
    dingtalk_webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    dingtalk_secret = os.getenv("DINGTALK_SECRET", "").strip()
    # default fallback to FEISHU_MAX_POSTS_PER_RUN if present to keep similar behavior
    dingtalk_max_posts_per_run = int(os.getenv("DINGTALK_MAX_POSTS_PER_RUN", os.getenv("FEISHU_MAX_POSTS_PER_RUN", "20")).strip())
    llm_base_url = _first_nonempty(
        os.getenv("OPENAI_COMPAT_BASE_URL"),
        os.getenv("MIMO_BASE_URL"),
        "https://api.minimaxi.com/v1",
    ).rstrip("/")
    llm_api_key = _first_nonempty(
        os.getenv("OPENAI_COMPAT_API_KEY"),
        os.getenv("MIMO_API_KEY"),
    )
    llm_model = _first_nonempty(
        os.getenv("OPENAI_COMPAT_MODEL"),
        os.getenv("MIMO_MODEL"),
        "MiniMax-M2.7",
    )
    llm_use_proxy = _first_nonempty(
        os.getenv("OPENAI_COMPAT_USE_PROXY"),
        os.getenv("MIMO_USE_PROXY"),
        "1",
    ).strip() not in {"0", "false", "False", ""}
    llm_proxy_server = _first_nonempty(
        os.getenv("OPENAI_COMPAT_PROXY_SERVER"),
        os.getenv("MIMO_PROXY_SERVER"),
        proxy_server,
    )

    if dingtalk_enabled and not dingtalk_webhook:
        raise ValueError("DingTalk enabled but DINGTALK_WEBHOOK not set")

    forum_urls: tuple[str, ...]
    if forum_urls_raw:
        forum_urls = tuple([s.strip() for s in forum_urls_raw.split("|") if s.strip()])
    elif leaks_url:
        forum_urls = (leaks_url,)
    else:
        forum_urls = tuple()

    data_dir = project_root / "data"
    logs_dir = project_root / "logs"

    if not base_url:
        raise ValueError("DARKFORUMS_BASE_URL is required")
    if not username:
        raise ValueError("DARKFORUMS_USERNAME is required")
    if not password:
        raise ValueError("DARKFORUMS_PASSWORD is required")

    return Settings(
        base_url=base_url,
        leaks_url=leaks_url,
        forum_urls=forum_urls,
        full_site_mode=full_site_mode,
        latest_page_only=latest_page_only,
        username=username,
        password=password,
        proxy_server=proxy_server,
        only_today=only_today,
        forum_sort_query=forum_sort_query,
        max_pages_per_forum=max_pages_per_forum,
        max_age_hours=max_age_hours,
        headless=headless,
        reply_templates=reply_templates,
        max_threads_per_day=max_threads_per_day,
        scrape_workers=scrape_workers,
        feishu_enabled=feishu_enabled,
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        feishu_chat_id=feishu_chat_id,
        feishu_max_posts_per_run=feishu_max_posts_per_run,
        dingtalk_enabled=dingtalk_enabled,
        dingtalk_webhook=dingtalk_webhook,
        dingtalk_secret=dingtalk_secret,
        dingtalk_max_posts_per_run=dingtalk_max_posts_per_run,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_use_proxy=llm_use_proxy,
        llm_proxy_server=llm_proxy_server,
        data_dir=data_dir,
        logs_dir=logs_dir,
    )
