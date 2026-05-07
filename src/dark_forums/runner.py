from __future__ import annotations

from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import base64
import hashlib
from dataclasses import dataclass

from .auth import login, save_storage_state
from .browser import human_delay, new_page, start_browser, stop_browser
from .config import Settings
from .db import (
    PostRecord,
    canonicalize_thread_url,
    get_cursor,
    has_post,
    insert_post,
    iter_undelivered_posts,
    mark_extracted,
    mark_delivered,
    mark_failed,
    mark_processing,
    migrate_thread_url,
    open_db,
    prune_threads,
    set_cursor,
    upsert_discovered,
    iter_pending,
    has_reply,
    mark_replied,
    get_thread_created_at,
)
from .discover import discover_today_threads
from .feishu import FeishuClient, FeishuConfig
from .dingtalk import DingTalkClient, DingTalkConfig
from .scrape import BrowserCheckError, ScrapeResult, scrape_thread_text


@dataclass(frozen=True)
class _PendingThread:
    url: str
    may_reply: bool
    created_at_cutoff_iso: str | None


@dataclass(frozen=True)
class _WorkerOutcome:
    url: str
    result: ScrapeResult | None = None
    browser_check: bool = False
    error: str | None = None


def _scrape_thread_worker(
    settings: Settings,
    storage_state_path: Path,
    task: _PendingThread,
) -> _WorkerOutcome:
    session = start_browser(
        headless=settings.headless,
        storage_state_path=storage_state_path,
        proxy_server=settings.proxy_server,
    )
    try:
        page = new_page(session)
        result = scrape_thread_text(
            page,
            task.url,
            settings.reply_templates,
            settings.data_dir,
            may_reply=task.may_reply,
            created_at_cutoff_iso=task.created_at_cutoff_iso,
        )
        return _WorkerOutcome(url=task.url, result=result)
    except BrowserCheckError:
        return _WorkerOutcome(url=task.url, browser_check=True, error="browser_check")
    except Exception as e:
        return _WorkerOutcome(url=task.url, error=repr(e))
    finally:
        stop_browser(session)


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, s: str) -> int:
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self) -> None:
        for st in self._streams:
            st.flush()

    def isatty(self) -> bool:
        for st in self._streams:
            try:
                if st.isatty():
                    return True
            except Exception:
                continue
        return False


def _deliver_to_feishu(conn, settings: Settings) -> None:
    if not settings.feishu_enabled:
        return

    cfg = FeishuConfig(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        chat_id=settings.feishu_chat_id,
    )
    client = FeishuClient(cfg)

    remaining = 0
    try:
        remaining = conn.execute(
            """
            SELECT COUNT(*)
            FROM posts p
            LEFT JOIN deliveries d
              ON d.post_url = p.url AND d.provider = 'feishu'
            WHERE d.post_url IS NULL
            """
        ).fetchone()[0]
    except Exception:
        remaining = 0

    max_success = max(1, int(settings.feishu_max_posts_per_run))
    delivered = 0
    attempted = 0
    failed = 0
    fetch_limit = max_success * 5
    if remaining <= 0:
        print(f"[feishu] summary limit={max_success} attempted=0 delivered=0 failed=0 remaining=0")
        return
    for row in iter_undelivered_posts(conn, provider="feishu", limit=fetch_limit):
        if delivered >= max_success:
            break
        attempted += 1
        post_url = str(row["url"])
        title = str(row["title"])
        created_at = str(row["created_at"])
        author_name = str(row["author_name"])
        first_post_text = str(row["first_post_text"])
        download_urls_json = str(row["download_urls_json"])
        screenshot_path = row["screenshot_path"]

        try:
            download_urls = json.loads(download_urls_json)
            if not isinstance(download_urls, list):
                download_urls = []
        except Exception:
            download_urls = []

        content: list[list[dict]] = []
        content.append(
            [
                {"tag": "text", "text": f"URL: {post_url}\n"},
                {"tag": "a", "text": "打开帖子", "href": post_url},
            ]
        )
        content.append([{"tag": "text", "text": f"作者: {author_name}    发布时间: {created_at}"}])

        if download_urls:
            content.append([{"tag": "text", "text": "下载链接:"}])
            for u in download_urls[:20]:
                if isinstance(u, str) and u.strip():
                    content.append([
                        {"tag": "a", "text": u.strip()[:120], "href": u.strip()},
                    ])

        if first_post_text:
            excerpt = first_post_text.strip()
            if len(excerpt) > 1200:
                excerpt = excerpt[:1200] + "..."
            content.append([{"tag": "text", "text": f"\n1楼内容摘要:\n{excerpt}"}])

        if screenshot_path:
            try:
                abs_path = (settings.data_dir / str(screenshot_path)).resolve()
                if abs_path.exists():
                    image_key = client.upload_image(abs_path)
                    content.append([{"tag": "img", "image_key": image_key}])
            except Exception:
                pass

        try:
            message_id = client.send_post_message(cfg.chat_id, title=title, content=content)
            mark_delivered(conn, post_url=post_url, provider="feishu", delivered_at=datetime.utcnow().isoformat(), message_id=message_id)
            delivered += 1
            print(f"[feishu] delivered: {post_url}")
        except Exception as e:
            failed += 1
            print(f"[feishu] failed: {post_url} -> {repr(e)}")

    try:
        remaining = conn.execute(
            """
            SELECT COUNT(*)
            FROM posts p
            LEFT JOIN deliveries d
              ON d.post_url = p.url AND d.provider = 'feishu'
            WHERE d.post_url IS NULL
            """
        ).fetchone()[0]
    except Exception:
        remaining = remaining

    if attempted or delivered or failed:
        print(
            f"[feishu] summary limit={max_success} attempted={attempted} delivered={delivered} failed={failed} remaining={remaining}"
        )
    else:
        print(f"[feishu] summary limit={max_success} attempted=0 delivered=0 failed=0 remaining={remaining}")


def _deliver_to_dingtalk(conn, settings: Settings) -> None:
    if not settings.dingtalk_enabled:
        return

    cfg = DingTalkConfig(webhook=settings.dingtalk_webhook, secret=(settings.dingtalk_secret or None))
    client = DingTalkClient(cfg)

    remaining = 0
    try:
        remaining = conn.execute(
            """
            SELECT COUNT(*)
            FROM posts p
            LEFT JOIN deliveries d
              ON d.post_url = p.url AND d.provider = 'dingtalk'
            WHERE d.post_url IS NULL
            """
        ).fetchone()[0]
    except Exception:
        remaining = 0

    max_success = max(1, int(settings.dingtalk_max_posts_per_run))
    delivered = 0
    attempted = 0
    failed = 0
    fetch_limit = max_success * 5
    if remaining <= 0:
        print(f"[dingtalk] summary limit={max_success} attempted=0 delivered=0 failed=0 remaining=0")
        return

    for row in iter_undelivered_posts(conn, provider="dingtalk", limit=fetch_limit):
        if delivered >= max_success:
            break
        attempted += 1
        post_url = str(row["url"])
        title = str(row["title"]) or "新帖子"
        created_at = str(row["created_at"]) or ""
        author_name = str(row["author_name"]) or ""
        first_post_text = str(row["first_post_text"]) or ""
        download_urls_json = str(row["download_urls_json"]) or "[]"
        screenshot_path = row["screenshot_path"]

        try:
            download_urls = json.loads(download_urls_json)
            if not isinstance(download_urls, list):
                download_urls = []
        except Exception:
            download_urls = []

        # Build markdown
        parts: list[str] = []
        link_title = f"[{title}]({post_url})"
        parts.append(link_title)
        meta_line = []
        if author_name:
            meta_line.append(f"作者: {author_name}")
        if created_at:
            meta_line.append(f"时间: {created_at}")
        if meta_line:
            parts.append("\n" + "    ".join(meta_line))
        if download_urls:
            parts.append("\n下载链接:")
            for u in download_urls[:20]:
                if isinstance(u, str) and u.strip():
                    parts.append(f"- {u.strip()}")
        if first_post_text:
            excerpt = first_post_text.strip()
            if len(excerpt) > 800:
                excerpt = excerpt[:800] + "..."
            parts.append("\n> 1楼内容摘要:\n> " + excerpt.replace("\n", "\n> "))

        text = "\n".join(parts)

        try:
            client.send_markdown(title=title[:128], text=text)
            mark_delivered(conn, post_url=post_url, provider="dingtalk", delivered_at=datetime.utcnow().isoformat(), message_id=None)
            delivered += 1
            print(f"[dingtalk] delivered: {post_url}")
        except Exception as e:
            failed += 1
            print(f"[dingtalk] failed: {post_url} -> {repr(e)}")
            continue

        # Try send image after markdown if exists
        if screenshot_path:
            try:
                abs_path = (settings.data_dir / str(screenshot_path)).resolve()
                if abs_path.exists():
                    with abs_path.open("rb") as fp:
                        b = fp.read()
                    b64 = base64.b64encode(b).decode("utf-8")
                    md5_hex = hashlib.md5(b).hexdigest()
                    client.send_image(b64, md5_hex)
            except Exception:
                pass

    try:
        remaining = conn.execute(
            """
            SELECT COUNT(*)
            FROM posts p
            LEFT JOIN deliveries d
              ON d.post_url = p.url AND d.provider = 'dingtalk'
            WHERE d.post_url IS NULL
            """
        ).fetchone()[0]
    except Exception:
        remaining = remaining

    if attempted or delivered or failed:
        print(
            f"[dingtalk] summary limit={max_success} attempted={attempted} delivered={delivered} failed={failed} remaining={remaining}"
        )
    else:
        print(f"[dingtalk] summary limit={max_success} attempted=0 delivered=0 failed=0 remaining={remaining}")


def run_daily(settings: Settings, project_root: Path) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)

    log_date = datetime.now().date().isoformat()
    log_path = settings.logs_dir / f"{log_date}.log"
    log_fp = log_path.open("a", encoding="utf-8")
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    sys.stdout = _Tee(orig_stdout, log_fp)
    sys.stderr = _Tee(orig_stderr, log_fp)

    db_path = settings.data_dir / "db.sqlite"
    storage_state_path = settings.data_dir / "storage_state.json"

    conn = open_db(db_path)

    session = start_browser(
        headless=settings.headless,
        storage_state_path=storage_state_path,
        proxy_server=settings.proxy_server,
    )
    try:
        page = new_page(session)
        print("[1/4] Logging in...")
        login(page, settings.base_url, settings.username, settings.password)
        save_storage_state(page, storage_state_path)
        print("[1/4] Logged in.")

        if settings.forum_urls:
            print(f"[2/4] Discovering threads from {len(settings.forum_urls)} forum(s)...")
            if getattr(settings, 'latest_page_only', False):
                print(f"[2/4] latest_page_only enabled: each forum will fetch only the newest 1 page using sort_query={settings.forum_sort_query!r}")
            discovered = 0
            inserted = 0
            sample_urls: list[str] = []
            for forum_url in settings.forum_urls:
                cursor_key = f"forum:{forum_url}"
                cursor_iso = get_cursor(conn, cursor_key)
                max_started_at: str | None = None
                # In latest_page_only mode, we only fetch the first page per forum
                for th in discover_today_threads(
                    page,
                    forum_url,
                    max_pages=(1 if getattr(settings, 'latest_page_only', False) else settings.max_pages_per_forum),
                    only_today=(False if (getattr(settings, 'full_site_mode', False) or getattr(settings, 'latest_page_only', False)) else settings.only_today),
                    max_age_hours=(settings.max_age_hours if not (getattr(settings, 'full_site_mode', False) or getattr(settings, 'latest_page_only', False)) else 24 * 365 * 100),
                    sort_query=settings.forum_sort_query,
                    cursor_iso=(None if getattr(settings, 'latest_page_only', False) else cursor_iso),
                ):
                    if len(sample_urls) < 5:
                        sample_urls.append(th.url)
                    if upsert_discovered(conn, th.url, th.discovered_at, th.started_at):
                        inserted += 1
                    discovered += 1
                    if th.started_at:
                        if (max_started_at is None) or (th.started_at > max_started_at):
                            max_started_at = th.started_at
                if max_started_at:
                    set_cursor(conn, cursor_key, max_started_at)
            print("[2/4] Discovery done.")
            print(f"[2/4] Discovered (pre-dedup yield count): {discovered}")
            print(f"[2/4] Inserted into SQLite: {inserted}")
            if sample_urls:
                print("[2/4] Sample discovered URLs:")
                for u in sample_urls:
                    print(f"  - {u}")

        print("[3/4] Scraping pending threads...")
        processed = 0
        charged = 0
        skipped_browser_check = 0
        ok_threads = 0
        failed_threads = 0
        total_download_urls = 0
        inserted_download_rows = 0
        if settings.max_threads_per_day > 0:
            fetch_limit = max(1, int(settings.max_threads_per_day) * 5)
            max_browser_check_skips = max(10, int(settings.max_threads_per_day) * 3)
        else:
            fetch_limit = 100_000
            max_browser_check_skips = 10_000
        seen_canonical: set[str] = set()
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=int(settings.max_age_hours))
        def _parse_localized_dt(s: str) -> datetime | None:
            if not s:
                return None
            t = s.strip()
            if not t:
                return None
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                pass
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            for fmt in ("%d-%m-%y, %I:%M %p", "%d-%m-%y, %H:%M", "%d-%m-%y"):
                try:
                    dt = datetime.strptime(t, fmt)
                    return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
                except Exception:
                    continue
            return None

        pending_tasks: list[_PendingThread] = []
        for url in iter_pending(conn, fetch_limit):
            try:
                canon = canonicalize_thread_url(url)
                if canon != url:
                    try:
                        migrate_thread_url(conn, url, canon)
                    except Exception:
                        pass
                    url = canon

                if url in seen_canonical:
                    continue
                seen_canonical.add(url)

                if has_post(conn, url):
                    continue
                # Hard cutoff pre-check using discovery-time started_at stored in threads.created_at
                try:
                    created_iso = get_thread_created_at(conn, url)
                except Exception:
                    created_iso = None
                if created_iso and not (getattr(settings, 'full_site_mode', False) or getattr(settings, 'latest_page_only', False)):
                    try:
                        dt0 = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
                        if dt0 < cutoff_dt:
                            mark_failed(conn, url, "older_than_cutoff")
                            print(f"[skip] {url} -> older_than_cutoff created_at={created_iso}")
                            processed += 1
                            failed_threads += 1
                            charged += 1
                            continue
                    except Exception:
                        pass
                mark_processing(conn, url)
                base_may_reply = not has_reply(conn, url)
                allow_reply = False
                if getattr(settings, 'full_site_mode', False):
                    allow_reply = False
                elif getattr(settings, 'latest_page_only', False):
                    # In latest-page-only mode, always allow reply (no time cutoff gating)
                    allow_reply = base_may_reply
                elif created_iso:
                    try:
                        dt0 = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
                        allow_reply = dt0 >= cutoff_dt and base_may_reply
                    except Exception:
                        allow_reply = False
                pending_tasks.append(
                    _PendingThread(
                        url=url,
                        may_reply=(False if getattr(settings, 'full_site_mode', False) else allow_reply),
                        created_at_cutoff_iso=(
                            None
                            if (getattr(settings, 'full_site_mode', False) or getattr(settings, 'latest_page_only', False))
                            else cutoff_dt.isoformat()
                        ),
                    )
                )
            except Exception as e:
                mark_failed(conn, url, repr(e))
                print(f"[failed] {url} -> {repr(e)}")
                processed += 1
                failed_threads += 1
                charged += 1
            if settings.max_threads_per_day > 0 and len(pending_tasks) >= settings.max_threads_per_day:
                break

        if pending_tasks:
            worker_count = max(1, int(getattr(settings, "scrape_workers", 1)))
            worker_count = min(worker_count, len(pending_tasks))
            print(f"[3/4] queued={len(pending_tasks)} scrape_workers={worker_count}")
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {}
                for task in pending_tasks:
                    future = executor.submit(_scrape_thread_worker, settings, storage_state_path, task)
                    future_map[future] = task.url

                for future in as_completed(future_map):
                    url = future_map[future]
                    try:
                        outcome = future.result()
                    except Exception as e:
                        mark_failed(conn, url, repr(e))
                        print(f"[failed] {url} -> {repr(e)}")
                        processed += 1
                        failed_threads += 1
                        charged += 1
                        continue

                    if outcome.browser_check:
                        mark_failed(conn, url, "browser_check")
                        print(f"[skip] {url} -> browser_check")
                        processed += 1
                        failed_threads += 1
                        skipped_browser_check += 1
                        if skipped_browser_check >= max_browser_check_skips:
                            print(f"[skip] hit browser_check skip cap: {skipped_browser_check}")
                            break
                        continue

                    if outcome.error is not None or outcome.result is None:
                        err = outcome.error or "unknown_worker_error"
                        mark_failed(conn, url, err)
                        print(f"[failed] {url} -> {err}")
                        processed += 1
                        failed_threads += 1
                        charged += 1
                        continue

                    result = outcome.result
                    title = (result.title or "").strip()
                    created_at = (result.created_at or "").strip()
                    author_name = (result.author_name or "").strip()
                    first_post_text = (result.first_post_text or "").strip()
                    download_urls = tuple(u.strip() for u in (result.download_urls or ()) if u and u.strip())

                    print(f"[meta] created_at={created_at or '(missing)'} url={url}")

                    if not (getattr(settings, 'full_site_mode', False) or getattr(settings, 'latest_page_only', False)):
                        parsed_dt = _parse_localized_dt(created_at)
                        if parsed_dt is None:
                            mark_failed(conn, url, "created_at_unparseable")
                            print(f"[failed] {url} -> created_at_unparseable")
                            processed += 1
                            failed_threads += 1
                            charged += 1
                            continue
                        if parsed_dt < cutoff_dt:
                            mark_failed(conn, url, "older_than_cutoff")
                            print(f"[skip] {url} -> older_than_cutoff created_at={created_at}")
                            processed += 1
                            failed_threads += 1
                            charged += 1
                            continue

                    if getattr(result, 'did_reply', False):
                        try:
                            mark_replied(conn, url, result.fetched_at)
                        except Exception:
                            pass

                    if not created_at:
                        created_at = result.fetched_at
                    rec = PostRecord(
                        url=url,
                        title=title or "",
                        created_at=created_at or "",
                        author_name=author_name or "",
                        author_posts=(result.author_posts or None),
                        author_threads=(result.author_threads or None),
                        author_joined=(result.author_joined or None),
                        author_reputation=(result.author_reputation or None),
                        author_contacts=(result.author_contacts or None),
                        scraped_at=result.fetched_at,
                        first_post_text=first_post_text or "",
                        download_urls_json=json.dumps(list(download_urls), ensure_ascii=False),
                        screenshot_path=(result.screenshot_path or None),
                    )
                    insert_post(conn, rec)
                    mark_extracted(conn, url, result.fetched_at, downloads_count=len(download_urls))
                    print(f"[ok] {url} -> inserted post (downloads {len(download_urls)})")
                    ok_threads += 1
                    total_download_urls += len(download_urls)
                    inserted_download_rows += 1
                    processed += 1
                    charged += 1
                    if settings.max_threads_per_day > 0 and charged >= settings.max_threads_per_day:
                        break

        # Deliver notifications
        if getattr(settings, 'dingtalk_enabled', False):
            print("[4/4] Delivering to DingTalk...")
            try:
                _deliver_to_dingtalk(conn, settings)
            except Exception as e:
                print(f"[dingtalk] deliver step failed -> {repr(e)}")
        elif getattr(settings, 'feishu_enabled', False):
            print("[4/4] Delivering to Feishu...")
            try:
                _deliver_to_feishu(conn, settings)
            except Exception as e:
                print(f"[feishu] deliver step failed -> {repr(e)}")

        print("[4/4] Done.")

        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE status IN ('new','failed') AND extracted_at IS NULL"
            ).fetchone()[0]
            threads_total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            posts_total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            print(
                "[run] processed=%d charged=%d skipped_browser_check=%d ok=%d failed=%d download_urls=%d downloads_new=%d"
                % (
                    processed,
                    charged,
                    skipped_browser_check,
                    ok_threads,
                    failed_threads,
                    total_download_urls,
                    inserted_download_rows,
                )
            )
            print(
                "[db] threads_total=%d posts_total=%d pending=%d"
                % (threads_total, posts_total, pending)
            )
        except Exception:
            pass

        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM threads GROUP BY status")
            stats = {row[0]: int(row[1]) for row in cur.fetchall()}
            print(f"[stats] {stats}")
        except Exception:
            pass

        try:
            pruned = prune_threads(conn, retention_days=30)
            if pruned:
                print(f"[db] pruned_old_threads={pruned}")
        except Exception:
            pass

    finally:
        stop_browser(session)
        conn.close()
        try:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr
            log_fp.close()
        except Exception:
            pass
