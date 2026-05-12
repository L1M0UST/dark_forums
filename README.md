# dark_forums

`dark_forums` is a Playwright-based scraper for the DarkForums site. It logs in with a real account, discovers fresh threads from configured forum pages, optionally posts a reply to unlock hidden content, extracts the first post plus download links, stores the result in SQLite, and can push new items to DingTalk or Feishu.

## What It Does

- Logs in with persisted browser state in `data/storage_state.json`
- Discovers thread URLs from one or more forum listing pages
- Supports incremental crawling with forum cursors
- Optionally replies to threads that require a reply before content becomes visible
- Extracts title, created time, author info, first-post text, download links, and screenshots
- Stores crawl state and final records in `data/db.sqlite`
- Pushes undelivered posts to DingTalk or Feishu
- Writes daily logs to `logs/YYYY-MM-DD.log`

## Project Layout

```text
dark_forums/
├─ run.py
├─ requirements.txt
├─ .env.example
├─ PROJECT_MEMORY.md
├─ src/dark_forums/
│  ├─ __main__.py
│  ├─ auth.py
│  ├─ browser.py
│  ├─ config.py
│  ├─ db.py
│  ├─ dingtalk.py
│  ├─ discover.py
│  ├─ feishu.py
│  ├─ runner.py
│  ├─ scrape.py
│  └─ text_extract.py
├─ data/
└─ logs/
```

## Requirements

- Python 3.12
- Playwright Chromium runtime
- Network access to the target site
- Optional local proxy if the target site requires it

Install dependencies:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## Configuration

The repository includes a tracked `.env` file with keys only and no values.

Recommended setup:

- Keep `.env` as the shared template
- Put your real local secrets in `.env.local`
- `.env.local` is ignored by Git and overrides `.env` at runtime

Key variables:

- `DARKFORUMS_BASE_URL`: site entry URL, usually `https://darkforums.su/index.php`
- `DARKFORUMS_USERNAME`
- `DARKFORUMS_PASSWORD`
- `DARKFORUMS_FORUM_URLS`: forum URLs separated by `|`
- `DARKFORUMS_PROXY_SERVER`: optional proxy, leave empty if unused
- `DARKFORUMS_LATEST_PAGE_ONLY`: `1` to crawl only the newest page of each forum
- `DARKFORUMS_FULL_SITE_MODE`: `1` for broad scanning without the usual time-window limits
- `DARKFORUMS_MAX_AGE_HOURS`: age cutoff for normal incremental crawling
- `DARKFORUMS_REPLY_TEMPLATES`: reply templates separated by `|`
- `DARKFORUMS_HEADLESS`: `1` for headless mode, `0` for visible browser mode
- `DARKFORUMS_SCRAPE_WORKERS`: number of concurrent scraping workers for thread-content fetches
- `DINGTALK_ENABLED` / `DINGTALK_WEBHOOK` / `DINGTALK_SECRET`
- `MIMO_BASE_URL` / `MIMO_API_KEY` / `MIMO_MODEL` / `MIMO_PROXY_SERVER`: translate DingTalk messages to Simplified Chinese before sending
- `FEISHU_ENABLED` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID`

Do not commit real secrets. Use `.env.local` for private values.

## How To Run

Run from the project root:

```bash
python run.py
```

Alternative package entrypoint:

```bash
python -m dark_forums
```

## Verified Run

Verified locally on May 6, 2026 by running `python run.py` outside the sandbox so Playwright could launch Chromium.

Observed result:

- Login succeeded
- Discovery ran against 1 configured forum
- 10 threads were discovered and inserted
- 10 threads were scraped successfully
- 2 threads triggered auto-reply before extraction
- 41 download URLs were extracted in total
- 5 DingTalk notifications were delivered successfully
- SQLite summary after the run: `threads_total=441`, `posts_total=80`, `pending=76`
- Old thread queue entries were pruned: `pruned_old_threads=424`

Generated artifacts included:

- [data/db.sqlite](/E:/code/py/dark_forums/data/db.sqlite)
- [data/storage_state.json](/E:/code/py/dark_forums/data/storage_state.json)
- [logs/2026-05-06.log](/E:/code/py/dark_forums/logs/2026-05-06.log)

## Data Model

Main SQLite tables:

- `threads`: discovered URLs plus queue state
- `posts`: final extracted thread records
- `cursors`: per-forum incremental crawl cursor
- `deliveries`: notification deduplication state

`posts` stores one row per thread, including:

- `url`
- `title`
- `created_at`
- `author_name`
- `author_posts`
- `author_threads`
- `author_joined`
- `author_reputation`
- `author_contacts`
- `scraped_at`
- `first_post_text`
- `download_urls_json`
- `screenshot_path`

## Scheduling

Typical usage is to run the script once per hour.

Windows Task Scheduler example:

```text
Program/script: python
Arguments: E:\code\py\dark_forums\run.py
Start in: E:\code\py\dark_forums
```

## Notes

- The browser profile is reused through `data/storage_state.json`, which helps avoid repeated full logins.
- `latest_page_only` is useful for lightweight hourly polling.
- A small `scrape_workers` value such as `2` to `4` usually gives the best speed/anti-bot balance.
- The same worker count is also used to parallelize forum discovery.
- `full_site_mode` can generate much larger workloads and disables the usual freshness gating.
- If Playwright cannot launch in a restricted shell, run it in a normal local shell.
- Runtime config is loaded from `.env`, then `.env.local` if present.
- DingTalk delivery is filtered to China-related posts only, based on URL/title/content keyword matching.
- If MiMo translation is configured, DingTalk titles and markdown bodies are translated to Simplified Chinese before sending.

## Security

- Rotate any secrets that were ever stored in a tracked config file before publishing this repository.
- Keep `.env.local`, SQLite data, screenshots, logs, and browser state out of Git.
- Review the target site's terms, account risk, and local legal constraints before automated use.
