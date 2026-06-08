from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import Page


@dataclass(frozen=True)
class DiscoveredThread:
    url: str
    discovered_at: str
    started_at: str | None


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _extract_time_dt(elem) -> datetime | None:
    try:
        dt = elem.get_attribute("datetime")
        if dt:
            return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        return None


def _parse_title_datetime(title: str) -> datetime | None:
    t = (title or "").strip()
    if not t:
        return None

    # Forum shows localized datetime without tz in a title attr (e.g. "25-03-26, 12:11 AM").
    # Interpret it in the local timezone and convert to UTC for stable comparisons.
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    for fmt in (
        "%d-%m-%y, %I:%M %p",
        "%d-%m-%y, %H:%M",
        "%d-%m-%y",
    ):
        try:
            dt = datetime.strptime(t, fmt)
            dt_local = dt.replace(tzinfo=local_tz)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _extract_datetime_from_text(text: str) -> datetime | None:
    t = (text or "").strip()
    if not t:
        return None

    import re

    m = re.search(r"\b(\d{2}-\d{2}-\d{2},\s*\d{1,2}:\d{2}\s*[AP]M)\b", t, flags=re.IGNORECASE)
    if m:
        dt = _parse_title_datetime(m.group(1))
        if dt is not None:
            return dt

    m = re.search(r"\b(\d{2}-\d{2}-\d{2},\s*\d{1,2}:\d{2})\b", t)
    if m:
        dt = _parse_title_datetime(m.group(1))
        if dt is not None:
            return dt

    return _parse_title_datetime(t)


def _normalize_thread_url(abs_url: str) -> str:
    parsed = urlparse(abs_url)
    q = parsed.query
    if q:
        parts = []
        for kv in q.split("&"):
            k = kv.split("=", 1)[0]
            if k in {"action", "page", "pid", "tid", "highlight", "session", "utm_source", "utm_medium", "utm_campaign"}:
                continue
            parts.append(kv)
        q = "&".join([p for p in parts if p])

    cleaned = parsed._replace(fragment="", query=q)
    return urlunparse(cleaned)


def _apply_forum_sort_query(forum_url: str, sort_query: str) -> str:
    if not sort_query:
        return forum_url
    parsed = urlparse(forum_url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    desired = dict(parse_qsl(sort_query, keep_blank_values=True))

    for k, v in desired.items():
        existing[k] = v

    merged_query = urlencode(list(existing.items())) if existing else ""
    return urlunparse(parsed._replace(query=merged_query))


def _is_browser_check(html: str) -> bool:
    h = (html or "").lower()
    if "checking your browser" in h:
        return True
    if "powered by" in h and "darkforums" in h and "please move your mouse" in h:
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
        return False

    try:
        return not _is_browser_check(page.content())
    except Exception:
        return False


def _looks_like_thread_link(href: str) -> bool:
    if not href:
        return False
    h = href.lower()
    if "misc.php" in h:
        return False
    if "action=lastpost" in h:
        return False
    if "action=login" in h:
        return False
    if "/user-" in h or "user-" in h:
        return False
    if "/forum-" in h or "forum-" in h:
        return False
    if "/thread-" in h or "thread-" in h:
        return True
    return False



def discover_today_threads(
    page: Page,
    forum_url: str,
    max_pages: int = 5,
    *,
    only_today: bool = True,
    max_age_hours: int = 24,
    sort_query: str = "",
    cursor_iso: str | None = None,
) -> Iterable[DiscoveredThread]:
    if not forum_url:
        raise ValueError("forum_url is required to discover threads")

    forum_url = _apply_forum_sort_query(forum_url, sort_query)

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            page.goto(forum_url, wait_until="domcontentloaded", timeout=90_000)
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            print(
                f"[发现] 打开版块失败，第 {attempt + 1}/3 次：forum={forum_url}，"
                f"可能是网络不通或代理异常，错误={repr(e)}"
            )
            page.wait_for_timeout(1000 * (attempt + 1))

    if last_exc is not None:
        try:
            from pathlib import Path

            out_dir = Path.cwd() / "data" / "debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "discover_goto_failed.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        raise last_exc

    html = page.content()
    if _is_browser_check(html):
        print(f"[发现] 版块页面命中浏览器验证，forum={forum_url}，正在尝试自动绕过。")
        for _ in range(3):
            if _try_bypass_browser_check(page):
                html = page.content()
                break
        if _is_browser_check(html):
            try:
                from pathlib import Path

                out_dir = Path.cwd() / "data" / "debug"
                out_dir.mkdir(parents=True, exist_ok=True)
                safe = urlparse(page.url).path.strip("/") or "forum"
                safe = safe.replace("/", "_")
                (out_dir / f"browser_check_discover_{safe}.html").write_text(html, encoding="utf-8")
            except Exception:
                pass
            print(f"[发现] 版块页面浏览器验证绕过失败，已跳过：forum={forum_url}")
            return

    today = _today_utc()

    cutoff_dt: datetime | None = None
    try:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=int(max_age_hours))
    except Exception:
        cutoff_dt = None

    cursor_dt: datetime | None = None
    if cursor_iso:
        try:
            cursor_dt = datetime.fromisoformat(cursor_iso.replace("Z", "+00:00"))
        except Exception:
            cursor_dt = None

    seen_urls: set[str] = set()

    saw_any_today = False
    for _ in range(max_pages):
        page.wait_for_timeout(1000)
        yielded_count = 0
        sample_hrefs: list[str] = []
        skip_reasons: list[str] = []

        # Limit discovery scope: forum pages contain many non-thread links (nav, widgets, etc.).
        # Prefer anchors inside the thread listing container if we can identify it.
        scope = None
        try:
            # Prefer the actual thread list container; avoid subforum last-post blocks above it.
            scope = page.locator(
                "section#forum-display:has(tr.inline_row a[href^='Thread-']), "
                "section#forum-display:has(tr.inline_row a[href^='thread-']), "
                "#forum-display:has(tr.inline_row a[href^='Thread-']), "
                "#forum-display:has(tr.inline_row a[href^='thread-']), "
                "table:has(tr.inline_row a[href^='Thread-']), "
                "table:has(tr.inline_row a[href^='thread-'])"
            ).first
            if scope.count() <= 0:
                scope = None
        except Exception:
            scope = None

        subject_selector = (
            "tr.inline_row span.subject_new a[href^='Thread-'], "
            "tr.inline_row span.subject_old a[href^='Thread-'], "
            "tr.inline_row span.subject_new a[href^='thread-'], "
            "tr.inline_row span.subject_old a[href^='thread-']"
        )
        broad_selector = (
            "tr.inline_row a[href^='Thread-'], "
            "tr.inline_row a[href^='thread-']"
        )

        if scope is not None:
            anchors = scope.locator(subject_selector)
            if anchors.count() <= 0:
                anchors = scope.locator(broad_selector)
        else:
            anchors = page.locator(
                "section#forum-display tr.inline_row span.subject_new a[href^='Thread-'], "
                "section#forum-display tr.inline_row span.subject_old a[href^='Thread-'], "
                "section#forum-display tr.inline_row span.subject_new a[href^='thread-'], "
                "section#forum-display tr.inline_row span.subject_old a[href^='thread-']"
            )
            if anchors.count() <= 0:
                anchors = page.locator(
                    "section#forum-display tr.inline_row a[href^='Thread-'], "
                    "section#forum-display tr.inline_row a[href^='thread-']"
                )
        count = anchors.count()
        page_had_today = False
        page_had_older = False
        hit_cursor = False

        for i in range(count):
            a = anchors.nth(i)
            href = a.get_attribute("href")
            if not href:
                continue

            if len(sample_hrefs) < 10:
                sample_hrefs.append(href)

            if not _looks_like_thread_link(href):
                if len(skip_reasons) < 20:
                    skip_reasons.append(f"skip:not_thread_link href={href}")
                continue

            abs_url = _normalize_thread_url(urljoin(page.url, href))

            if abs_url in seen_urls:
                if len(skip_reasons) < 20:
                    skip_reasons.append(f"skip:seen url={abs_url}")
                continue
            seen_urls.add(abs_url)

            row = a.locator("xpath=ancestor::tr[1]")
            if row.count() <= 0:
                row = a.locator("xpath=ancestor::li[1]")
            if row.count() <= 0:
                row = a.locator("xpath=ancestor::div[1]")
            # Some rows can be 'delete thread' notices or not visible; skip them early.
            try:
                row_txt = (row.inner_text(timeout=500) or "").lower()
            except Exception:
                row_txt = ""
            if any(k in row_txt for k in ("delete thread", "thread deleted", "not visible", "不可见")):
                if len(skip_reasons) < 20:
                    skip_reasons.append(f"skip:row_hidden url={abs_url}")
                continue
            started_dt: datetime | None = None

            time_loc = row.locator("time")
            if time_loc.count() > 0:
                started_dt = _extract_time_dt(time_loc.first)

            if started_dt is None:
                # Many forum skins render the thread date directly inside this span.
                date_loc = row.locator("span.forum-display__thread-date")
                if date_loc.count() > 0:
                    try:
                        date_txt = date_loc.first.inner_text(timeout=1000) or ""
                    except Exception:
                        date_txt = ""
                    dt0 = _extract_datetime_from_text(date_txt)
                    if dt0 is not None:
                        started_dt = dt0

            if started_dt is None:
                # Prefer the forum thread date span with title attribute.
                title_loc = row.locator("span.forum-display__thread-date span[title]")
                if title_loc.count() <= 0:
                    # Fallback: MyBB often uses generic span[title] with a formatted datetime.
                    title_loc = row.locator("span[title]")
                if title_loc.count() > 0:
                    try:
                        title_attr = title_loc.first.get_attribute("title") or ""
                    except Exception:
                        title_attr = ""
                    started_dt = _parse_title_datetime(title_attr)

            if started_dt is None:
                # Some skins render the visible datetime in the 2nd span inside the date area (no title attr).
                try:
                    vis_loc = row.locator("span.forum-display__thread-date span:nth-of-type(2)")
                except Exception:
                    vis_loc = None
                if vis_loc and vis_loc.count() > 0:
                    try:
                        vis_txt = vis_loc.first.inner_text(timeout=1000) or ""
                    except Exception:
                        vis_txt = ""
                    if vis_txt.strip():
                        cand = vis_txt.strip()
                        dt2 = _parse_title_datetime(cand)
                        if dt2 is not None:
                            started_dt = dt2

            if started_dt is None:
                # Generic fallback: regex match a localized datetime from the row text.
                dt3 = _extract_datetime_from_text(row_txt)
                if dt3 is not None:
                    started_dt = dt3

            if started_dt is None:
                # Forum variants: date often appears in the 2nd TD's inner spans (no title attr),
                # e.g. /html/body/div[1]/main/div/section[2]/table/tbody/tr[6]/td[2]/div/div/span[2]
                try:
                    td2_span2 = row.locator("td:nth-child(2) div div span:nth-of-type(2)")
                except Exception:
                    td2_span2 = None
                if td2_span2 and td2_span2.count() > 0:
                    try:
                        ttxt = td2_span2.first.inner_text(timeout=1000) or ""
                    except Exception:
                        ttxt = ""
                    if ttxt.strip():
                        dt4 = _extract_datetime_from_text(ttxt.strip())
                        if dt4 is not None:
                            started_dt = dt4

            if cursor_dt is not None and started_dt is not None and started_dt <= cursor_dt:
                if len(skip_reasons) < 20:
                    skip_reasons.append(f"skip:cursor url={abs_url} started_at={started_dt.isoformat()}")
                hit_cursor = True
                break

            if cutoff_dt is not None:
                if started_dt is None:
                    # Strict mode: if we cannot determine started time, do not include it.
                    if len(skip_reasons) < 20:
                        skip_reasons.append(f"skip:no_started_dt url={abs_url}")
                    continue
                if started_dt < cutoff_dt:
                    if len(skip_reasons) < 20:
                        skip_reasons.append(
                            f"skip:older_than_cutoff url={abs_url} started_at={started_dt.isoformat()} cutoff={cutoff_dt.isoformat()}"
                        )
                    hit_cursor = True
                    break

            if only_today:
                is_today = False
                if started_dt is not None:
                    is_today = started_dt.date() == today
                else:
                    try:
                        txt = row.inner_text(timeout=1000).lower()
                    except Exception:
                        txt = ""
                    if "today" in txt:
                        is_today = True

                if is_today:
                    saw_any_today = True
                    page_had_today = True
                else:
                    page_had_older = True
                    if len(skip_reasons) < 20:
                        skip_reasons.append(f"skip:not_today url={abs_url} started_at={(started_dt.isoformat() if started_dt is not None else 'unknown')}")
                    continue

            yielded_count += 1
            yield DiscoveredThread(
                url=abs_url,
                discovered_at=datetime.now(timezone.utc).isoformat(),
                started_at=(started_dt.isoformat() if started_dt is not None else None),
            )

        if hit_cursor:
            break

        # Debug: if discovery yields 0 rows or an unusually high number, dump HTML and reasons for tuning.
        if yielded_count == 0 or count == 0 or count > 200:
            try:
                from pathlib import Path

                out_dir = Path.cwd() / "data" / "debug"
                out_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                safe = urlparse(page.url).path.strip("/").replace("/", "_") or "forum"
                tag = "empty" if yielded_count == 0 else "suspicious"
                (out_dir / f"discover_{tag}_{safe}_{stamp}.html").write_text(
                    page.content(), encoding="utf-8"
                )
                debug_lines = [
                    f"page_url={page.url}",
                    f"anchor_count={count}",
                    f"yielded_count={yielded_count}",
                    f"cutoff_dt={(cutoff_dt.isoformat() if cutoff_dt is not None else 'None')}",
                    "sample_hrefs:",
                    *sample_hrefs,
                    "skip_reasons:",
                    *skip_reasons,
                ]
                (out_dir / f"discover_{tag}_{safe}_{stamp}.txt").write_text(
                    "\n".join(debug_lines), encoding="utf-8"
                )
            except Exception:
                pass
            print(
                f"[发现] 已输出调试文件：page_url={page.url}，anchor_count={count}，"
                f"yielded_count={yielded_count}，tag={tag}"
            )

        if only_today and page_had_older and saw_any_today and not page_had_today:
            break

        next_selectors = [
            "a[rel='next']",
            "a.pageNav-jump--next",
            "a:has-text('Next')",
            "a:has-text('>')",
        ]
        clicked = False
        for sel in next_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                page.wait_for_load_state("domcontentloaded")
                clicked = True
                break
        if not clicked:
            break


def discover_today_threads_from_forums(
    page: Page,
    forum_urls: Iterable[str],
    max_pages_per_forum: int = 5,
    *,
    only_today: bool = True,
    sort_query: str = "",
) -> Iterable[DiscoveredThread]:
    for url in forum_urls:
        try:
            yield from discover_today_threads(
                page,
                url,
                max_pages=max_pages_per_forum,
                only_today=only_today,
                sort_query=sort_query,
            )
        except Exception:
            continue
