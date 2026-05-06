from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page

from bs4 import BeautifulSoup

from .text_extract import html_to_text


@dataclass(frozen=True)
class ScrapeResult:
    download_urls: tuple[str, ...]
    title: str | None
    first_post_text: str | None
    created_at: str | None
    author_name: str | None
    author_posts: str | None
    author_threads: str | None
    author_joined: str | None
    author_reputation: str | None
    author_contacts: str | None
    fetched_at: str
    screenshot_path: str | None
    did_reply: bool = False


class BrowserCheckError(RuntimeError):
    pass


def _needs_reply(page: Page) -> bool:
    html = page.content()
    return _needs_reply_from_html(html)


def _needs_reply_from_html(html: str) -> bool:
    soup = BeautifulSoup(html, "lxml")
    scope = None
    try:
        scope = soup.select_one(
            "main div table tbody tr:first-of-type td div div:first-of-type"
        )
    except Exception:
        scope = None

    if scope is None:
        return False

    try:
        body = scope.select_one(".post_body")
        if body is not None:
            scope = body
    except Exception:
        pass

    try:
        if scope.select_one(".hidden-content") is not None:
            return True
    except Exception:
        pass

    try:
        text = scope.get_text("\n", strip=True)
    except Exception:
        text = ""

    h = (text or "").lower()
    try:
        h2 = (str(scope) or "").lower()
    except Exception:
        h2 = ""
    keywords = [
        "hidden content",
        "reply to view",
        "reply to see",
        "you must reply",
        "you must reply to this thread to view this content",
        "you must replay",
        "replay to view",
        "replay to see",
        "reply and refresh",
        "回复可见",
        "回帖可见",
        # broader triggers per requirement
        "reply",
        "hidden",
        "回复",
        "隐藏",
    ]
    if any((k in h) or (k in h2) for k in keywords):
        return True

    # Extra safety: some pages vary wording/casing; detect the core pattern.
    try:
        if re.search(r"\byou\s+must\s+repl(?:y|ay)\b", h, flags=re.IGNORECASE):
            return True
    except Exception:
        pass
    try:
        if re.search(r"\byou\s+must\s+repl(?:y|ay)\b", h2, flags=re.IGNORECASE):
            return True
    except Exception:
        pass

    return False


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
        pass

    try:
        return not _is_browser_check(page.content())
    except Exception:
        return False


def _extract_download_urls(html: str, base_url: str) -> tuple[str, ...]:
    soup = BeautifulSoup(html, "lxml")

    urls: list[str] = []

    first_post_scope = None
    try:
        first_post_scope = soup.select_one(
            "main div table tbody tr:first-of-type td div div:first-of-type"
        )
    except Exception:
        first_post_scope = None

    if first_post_scope is not None:
        try:
            body = first_post_scope.select_one(".post_body")
            if body is not None:
                first_post_scope = body
        except Exception:
            pass

    def _is_noise_url(abs_url: str) -> bool:
        try:
            u = urlparse(abs_url)
        except Exception:
            return True

        host = (u.netloc or "").lower()
        path = (u.path or "").strip()

        if host.endswith("darkforums.su"):
            if path.lower() in {"/upgrade", "/upgrade/"}:
                return True
        return False

    def add_href(href: str | None) -> None:
        if not href:
            return
        h = href.strip()
        if not h:
            return
        if h.startswith("javascript:"):
            return
        if h.startswith("#"):
            return
        from urllib.parse import urljoin

        abs_url = urljoin(base_url, h)
        if _is_noise_url(abs_url):
            return
        if abs_url not in urls:
            urls.append(abs_url)

    scope = first_post_scope
    if scope is None:
        return tuple()

    try:
        for a in scope.select("a[href]"):
            add_href(a.get("href"))
    except Exception:
        pass

    try:
        text = scope.get_text("\n", strip=True)
    except Exception:
        text = ""
    if text:
        for m in re.finditer(r"https?://[^\s\]\)\}\>\"\']+", text):
            add_href(m.group(0))

    return tuple(urls)


def _extract_title_first_post_created_at_author(
    html: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None, str | None, str | None, str | None]:
    soup = BeautifulSoup(html, "lxml")

    def _extract_absolute_datetime_text(node) -> str | None:
        if node is None:
            return None

        selectors = [
            "span[title]",
            ".post_date span[title]",
            ".thread-info__datetime span[title]",
        ]
        for selector in selectors:
            try:
                title_node = node.select_one(selector)
            except Exception:
                title_node = None
            if title_node is None:
                continue
            try:
                title_attr = (title_node.get("title") or "").strip()
            except Exception:
                title_attr = ""
            if title_attr:
                m = re.search(
                    r"\b(\d{2}-\d{2}-\d{2},\s*\d{1,2}:\d{2}\s*[AP]M)\b",
                    title_attr,
                    flags=re.IGNORECASE,
                )
                if m:
                    return m.group(1).strip()
                return title_attr
        return None

    title = None
    try:
        h1 = soup.select_one("h1")
        if h1:
            t = h1.get_text(" ", strip=True)
            if t:
                title = t
    except Exception:
        title = None

    if not title:
        try:
            t = (soup.title.get_text(" ", strip=True) if soup.title else "")
            if t:
                title = t
        except Exception:
            title = None

    first_post_text = None
    created_at = None
    author_name = None
    author_posts = None
    author_threads = None
    author_joined = None
    author_reputation = None
    author_contacts = None

    root = None
    try:
        root = soup.select_one("main div table tbody tr:first-of-type td div div:first-of-type")
    except Exception:
        root = None

    def parse_author_box(author_box) -> None:
        nonlocal author_name, author_posts, author_threads, author_joined, author_reputation, author_contacts

        if author_box is None:
            return

        # Name: prefer explicit username area in the post author card.
        if not author_name:
            try:
                nm_el = author_box.select_one(".post_user-profile a") or author_box.select_one(
                    ".post_user-profile"
                )
            except Exception:
                nm_el = None

            if nm_el is not None:
                try:
                    nm = (nm_el.get_text(" ", strip=True) or "").strip()
                except Exception:
                    nm = ""
                if nm:
                    author_name = nm

        if not author_name:
            try:
                for a in author_box.select("a[href]"):
                    href = (a.get("href") or "").strip()
                    nm = (a.get_text(" ", strip=True) or "").strip()
                    if not nm:
                        continue
                    if ("/user-" in href.lower()) or ("member.php" in href.lower()) or ("profile" in href.lower()):
                        author_name = nm
                        break
            except Exception:
                pass

        if not author_name:
            try:
                nm = (author_box.get_text(" ", strip=True) or "").strip()
            except Exception:
                nm = ""
            nm = (nm or "").strip()
            if nm:
                cand = nm.split(" ", 1)[0]
                if cand.lower() not in {"posts", "threads", "joined", "reputation", "hidden", "content"}:
                    author_name = cand

        try:
            box_text = author_box.get_text("\n", strip=True)
        except Exception:
            box_text = ""

        # Stats: the site uses left/right spans in .post_stats-bit.group
        try:
            for bit in author_box.select(".post_stats-bit.group"):
                left = None
                right = None
                try:
                    left_el = bit.select_one(".float_left")
                    right_el = bit.select_one(".float_right")
                    left = (left_el.get_text(" ", strip=True) if left_el else "")
                    right = (right_el.get_text(" ", strip=True) if right_el else "")
                except Exception:
                    left = ""
                    right = ""

                key = (left or "").strip().lower()
                val = (right or "").strip()
                if not key or not val:
                    continue
                if (key == "posts") and not author_posts:
                    author_posts = val
                elif (key == "threads") and not author_threads:
                    author_threads = val
                elif (key == "joined") and not author_joined:
                    author_joined = val
                elif (key == "reputation") and not author_reputation:
                    author_reputation = val
        except Exception:
            pass

        for ln in [x.strip() for x in (box_text or "").splitlines() if x.strip()]:
            m = re.search(r"^posts\s*:?\s*(\d+)\b", ln, flags=re.IGNORECASE)
            if m and not author_posts:
                author_posts = m.group(1)
            m = re.search(r"^threads\s*:?\s*(\d+)\b", ln, flags=re.IGNORECASE)
            if m and not author_threads:
                author_threads = m.group(1)
            m = re.search(r"^joined\s*:?\s*(.+)$", ln, flags=re.IGNORECASE)
            if m and not author_joined:
                author_joined = m.group(1).strip()
            m = re.search(r"^reputation\s*:?\s*([-+]?\d+)\b", ln, flags=re.IGNORECASE)
            if m and not author_reputation:
                author_reputation = m.group(1)

        contacts: list[str] = []
        try:
            for a2 in author_box.select("a[href]"):
                href = (a2.get("href") or "").strip()
                if not href:
                    continue
                if href.startswith("mailto:"):
                    contacts.append(href)
                elif "t.me/" in href or "telegram" in href.lower():
                    contacts.append(href)
                elif href.startswith("http"):
                    if "darkforums.su" not in href.lower():
                        contacts.append(href)
        except Exception:
            pass
        if contacts and not author_contacts:
            author_contacts = "\n".join(sorted(set(contacts)))

    if root is not None:
        try:
            author_box = (
                root.select_one(".post_author")
                or root.select_one(".post_author-info")
                or root.select_one(".post_author-stats")
            )
        except Exception:
            author_box = None
        parse_author_box(author_box)

        try:
            t = root.select_one("time")
            if t and t.has_attr("datetime"):
                dt = str(t.get("datetime") or "").strip()
                if dt:
                    created_at = dt
        except Exception:
            pass

        if not created_at:
            created_at = _extract_absolute_datetime_text(root)

        if not created_at:
            try:
                ts = root.select_one(".post_date")
            except Exception:
                ts = None
            if ts is not None:
                try:
                    dt = ts.get_text(" ", strip=True)
                except Exception:
                    dt = ""
                dt = (dt or "").strip()
                if dt:
                    m = re.search(
                        r"\b(\d{2}-\d{2}-\d{2},\s*\d{1,2}:\d{2}\s*[AP]M)\b",
                        dt,
                        flags=re.IGNORECASE,
                    )
                    if m:
                        created_at = m.group(1).strip()
                    else:
                        dt0 = dt.split("(", 1)[0].strip()
                        if dt0 and len(dt0) <= 80:
                            created_at = dt0

        if not created_at:
            try:
                info_dt = soup.select_one(".thread-info__datetime")
            except Exception:
                info_dt = None
            if info_dt is not None:
                created_at = _extract_absolute_datetime_text(info_dt)
        if not created_at:
            try:
                td2 = root.select_one("td:nth-child(2)")
            except Exception:
                td2 = None
            created_at = _extract_absolute_datetime_text(td2)

        if not created_at:
            try:
                info_dt = soup.select_one(".thread-info__datetime")
            except Exception:
                info_dt = None
            if info_dt is not None:
                try:
                    dt2 = info_dt.get_text(" ", strip=True)
                except Exception:
                    dt2 = ""
                dt2 = (dt2 or "").strip()
                if dt2:
                    # example: "by anisanas2 - 20-03-26, 11:41 PM"
                    if " - " in dt2:
                        dt2 = dt2.split(" - ", 1)[1].strip()
                    m2 = re.search(
                        r"\b(\d{2}-\d{2}-\d{2},\s*\d{1,2}:\d{2}\s*[AP]M)\b",
                        dt2,
                        flags=re.IGNORECASE,
                    )
                    if m2:
                        created_at = m2.group(1).strip()

        try:
            body = root.select_one(".post_body")
        except Exception:
            body = None
        if body is not None:
            try:
                txt = body.get_text("\n", strip=True)
            except Exception:
                txt = ""
        else:
            try:
                txt = root.get_text("\n", strip=True)
            except Exception:
                txt = ""

        if txt:
            lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            first_post_text = "\n".join(lines)

    if not first_post_text:
        first_post_text = html_to_text(html)

    if first_post_text:
        first_post_text = first_post_text[:20_000]

    return (
        title,
        first_post_text,
        created_at,
        author_name,
        author_posts,
        author_threads,
        author_joined,
        author_reputation,
        author_contacts,
    )


def _post_reply_if_possible(page: Page, templates: tuple[str, ...]) -> bool:
    message = random.choice(templates)
    try:
        suffixes = (
            "Thanks!",
            "Great share.",
            "Much appreciated.",
            "Nice work.",
            "Interesting.",
            "Good info.",
        )
        if random.random() < 0.85:
            message = f"{message} {random.choice(suffixes)}"
        if random.random() < 0.35:
            message = f"{message} #{random.randint(10, 999)}"
    except Exception:
        pass

    textarea_selectors = [
        "textarea[name='message']",
        "textarea[name='message_html']",
        "textarea",
    ]

    filled = False
    textarea_loc = None
    for sel in textarea_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            loc.first.fill(message)
            textarea_loc = loc.first
            filled = True
            break

    if not filled:
        return False

    submit_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Post reply')",
        "text=/post\\s*reply/i",
        "button:has-text('Reply')",
        "text=/\\breply\\b/i",
        "button:has-text('Submit')",
        "text=/submit/i",
        "button:has-text('发表')",
        "button:has-text('回复')",
    ]

    if textarea_loc is not None:
        try:
            form = textarea_loc.locator("xpath=ancestor::form[1]")
            if form.count() > 0:
                for sel in submit_selectors:
                    btn = form.first.locator(sel)
                    if btn.count() > 0:
                        b = btn.first
                        try:
                            b.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        if b.is_visible():
                            b.click(timeout=10_000)
                            page.wait_for_load_state("domcontentloaded")
                            return True
        except Exception:
            pass

    # Fallback (less reliable): any visible submit on the page
    for sel in submit_selectors:
        loc = page.locator(sel)
        if loc.count() > 0:
            b = loc.first
            try:
                b.scroll_into_view_if_needed()
            except Exception:
                pass
            if b.is_visible():
                b.click(timeout=10_000)
                page.wait_for_load_state("domcontentloaded")
                return True

    return False


def scrape_thread_text(page: Page, url: str, reply_templates: tuple[str, ...], data_dir: Path, *, may_reply: bool = True, created_at_cutoff_iso: str | None = None) -> ScrapeResult:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)

    html = page.content()
    if _is_browser_check(html):
        for _ in range(3):
            if _try_bypass_browser_check(page):
                html = page.content()
                break
        if _is_browser_check(html):
            try:
                out_dir = Path.cwd() / "data" / "debug"
                out_dir.mkdir(parents=True, exist_ok=True)
                safe = urlparse(page.url).path.strip("/") or "thread"
                safe = safe.replace("/", "_")
                (out_dir / f"browser_check_{safe}.html").write_text(html, encoding="utf-8")
            except Exception:
                pass
            raise BrowserCheckError("browser_check")

    (
        title,
        first_post_text,
        created_at,
        author_name,
        author_posts,
        author_threads,
        author_joined,
        author_reputation,
        author_contacts,
    ) = _extract_title_first_post_created_at_author(html)
    download_urls = _extract_download_urls(html, page.url)

    needs_reply = _needs_reply_from_html(html)
    within_cutoff = False
    if created_at_cutoff_iso:
        try:
            cutoff_dt = datetime.fromisoformat(created_at_cutoff_iso.replace("Z", "+00:00"))
        except Exception:
            cutoff_dt = None
        if cutoff_dt is not None:
            t = (created_at or "").strip()
            if t:
                ok = None
                try:
                    ok = datetime.fromisoformat(t.replace("Z", "+00:00"))
                except Exception:
                    ok = None
                if ok is None:
                    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
                    for fmt in ("%d-%m-%y, %I:%M %p", "%d-%m-%y, %H:%M", "%d-%m-%y"):
                        try:
                            dt = datetime.strptime(t, fmt)
                            ok = dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
                            break
                        except Exception:
                            continue
                if ok is not None and ok >= cutoff_dt:
                    within_cutoff = True
    else:
        within_cutoff = True
    did_reply = False
    if needs_reply and may_reply and within_cutoff:
        print(f"[reply] needs_reply=True -> attempting reply: {url}")
        posted = _post_reply_if_possible(page, reply_templates)
        print(f"[reply] posted={posted}: {url}")
        if posted:
            did_reply = True
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            html = page.content()
            if _is_browser_check(html):
                for _ in range(3):
                    if _try_bypass_browser_check(page):
                        html = page.content()
                        break
                if _is_browser_check(html):
                    try:
                        out_dir = Path.cwd() / "data" / "debug"
                        out_dir.mkdir(parents=True, exist_ok=True)
                        safe = urlparse(page.url).path.strip("/") or "thread"
                        safe = safe.replace("/", "_")
                        (out_dir / f"browser_check_{safe}.html").write_text(html, encoding="utf-8")
                    except Exception:
                        pass
                    raise BrowserCheckError("browser_check")

            (
                title,
                first_post_text,
                created_at,
                author_name,
                author_posts,
                author_threads,
                author_joined,
                author_reputation,
                author_contacts,
            ) = _extract_title_first_post_created_at_author(html)
            download_urls = _extract_download_urls(html, page.url)

    if not download_urls:
        try:
            out_dir = Path.cwd() / "data" / "debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = urlparse(page.url).path.strip("/") or "thread"
            safe = safe.replace("/", "_")
            (out_dir / f"no_downloads_{safe}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

    if not ((title or "").strip() and (created_at or "").strip() and (author_name or "").strip() and (first_post_text or "").strip()):
        try:
            out_dir = Path.cwd() / "data" / "debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = urlparse(page.url).path.strip("/") or "thread"
            safe = safe.replace("/", "_")
            (out_dir / f"incomplete_meta_{safe}.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

    fetched_at = datetime.now(timezone.utc).isoformat()

    screenshot_path: str | None = None
    try:
        shot_path = build_screenshot_path(data_dir, page.url, fetched_at)
        loc = page.locator(
            "xpath=/html/body/div[1]/main/div/table/tbody/tr[1]/td/div/div[1]"
        ).first
        try:
            loc.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        loc.screenshot(path=str(shot_path))
        try:
            screenshot_path = str(Path(shot_path).relative_to(data_dir))
        except Exception:
            screenshot_path = str(shot_path)
    except Exception:
        screenshot_path = None
    return ScrapeResult(
        download_urls=download_urls,
        title=title,
        first_post_text=first_post_text,
        created_at=created_at,
        author_name=author_name,
        author_posts=author_posts,
        author_threads=author_threads,
        author_joined=author_joined,
        author_reputation=author_reputation,
        author_contacts=author_contacts,
        fetched_at=fetched_at,
        screenshot_path=screenshot_path,
        did_reply=did_reply,
    )


def build_output_path(data_dir: Path, thread_url: str, fetched_at_iso: str) -> Path:
    d = datetime.fromisoformat(fetched_at_iso.replace("Z", "+00:00")).date().isoformat()
    parsed = urlparse(thread_url)
    safe = (parsed.path.strip("/") + "_" + (parsed.query or "")).strip("_")
    safe = safe.replace("/", "_")
    if not safe:
        safe = "thread"

    out_dir = data_dir / "posts" / d
    out_dir.mkdir(parents=True, exist_ok=True)

    return out_dir / (safe[:180] + ".txt")


def build_screenshot_path(data_dir: Path, thread_url: str, fetched_at_iso: str) -> Path:
    d = datetime.fromisoformat(fetched_at_iso.replace("Z", "+00:00")).date().isoformat()
    parsed = urlparse(thread_url)
    safe = (parsed.path.strip("/") + "_" + (parsed.query or "")).strip("_")
    safe = safe.replace("/", "_")
    if not safe:
        safe = "thread"

    out_dir = data_dir / "screenshots" / d
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / (safe[:180] + ".png")
