# dark_forums

`dark_forums` 是一个基于 Playwright 的 DarkForums 自动抓取项目。它会使用真实账号登录站点，从配置好的 forum 列表页发现最新帖子，按需自动回帖解锁隐藏内容，提取首楼正文和下载链接，保存到 SQLite，并将符合条件的内容推送到钉钉或飞书。

## 功能概览

- 复用 `data/storage_state.json` 中的登录态，减少重复登录
- 从多个 forum 列表页发现帖子 URL
- 使用 SQLite 游标和去重机制做增量抓取
- 对“回复可见”的帖子自动回帖后再次提取
- 提取标题、发布时间、作者信息、首楼正文、下载链接和截图
- 将抓取结果保存到 `data/db.sqlite`
- 支持钉钉、飞书消息推送
- 每日运行日志写入 `logs/YYYY-MM-DD.log`

## 目录结构

```text
dark_forums/
├─ run.py
├─ requirements.txt
├─ .env
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
│  ├─ openai_compat.py
│  ├─ runner.py
│  ├─ scrape.py
│  └─ text_extract.py
├─ data/
└─ logs/
```

## 环境要求

- Python 3.12
- Playwright Chromium 运行时
- 能访问目标站点的网络环境
- 如果目标站点需要代理，准备好本地代理

安装依赖：

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 配置说明

仓库中提交的 `.env` 只保留键名，不放真实值。

推荐做法：

- `.env` 作为公共模板
- 本机真实配置写到 `.env.local`
- 运行时先加载 `.env`，再加载 `.env.local`
- `.env.local` 已被 Git 忽略，不会推送到仓库

常用配置项：

- `DARKFORUMS_BASE_URL`：站点入口地址
- `DARKFORUMS_USERNAME`
- `DARKFORUMS_PASSWORD`
- `DARKFORUMS_FORUM_URLS`：多个 forum URL 用 `|` 分隔
- `DARKFORUMS_PROXY_SERVER`：站点抓取代理，不需要可留空
- `DARKFORUMS_LATEST_PAGE_ONLY`：是否只抓每个 forum 最新一页
- `DARKFORUMS_FULL_SITE_MODE`：全站宽范围扫描模式
- `DARKFORUMS_MAX_AGE_HOURS`：常规增量抓取时间窗口
- `DARKFORUMS_REPLY_TEMPLATES`：自动回帖模板，使用 `|` 分隔
- `DARKFORUMS_HEADLESS`：是否无头运行
- `DARKFORUMS_SCRAPE_WORKERS`：并发 worker 数，既用于发现阶段，也用于帖子内容抓取阶段
- `DINGTALK_ENABLED` / `DINGTALK_WEBHOOK` / `DINGTALK_SECRET`
- `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_MODEL` / `OPENAI_COMPAT_PROXY_SERVER`
- `OPENAI_COMPAT_USE_PROXY`：是否让翻译模型请求走代理，`1` 为启用，`0` 为关闭
- `MIMO_BASE_URL` / `MIMO_API_KEY` / `MIMO_MODEL` / `MIMO_PROXY_SERVER`
说明：`MIMO_*` 目前作为向后兼容别名保留，优先推荐使用 `OPENAI_COMPAT_*`
- `FEISHU_ENABLED` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID`

请不要把真实密钥提交到仓库，私有值统一放到 `.env.local`。

## 运行方式

在项目根目录执行：

```bash
python run.py
```

或者使用包入口：

```bash
python -m dark_forums
```

## 当前抓取逻辑

- 每次优先访问配置好的 forum 列表页
- 可配置为“每个 forum 只抓最新一页”
- 发现到的线程先写入 SQLite 去重
- 已成功提取并入库的线程不会重复抓取
- `failed` 线程只会再重试一次
- 提取成功后会将线程状态标记为 `done`

## 推送逻辑

### 钉钉

- 仅推送与中国相关的内容
- 关键词匹配范围包括中国、港澳台、四个直辖市、各省和一批重点城市
- 发送前会调用 OpenAI 兼容大模型接口，把标题和 markdown 正文翻译为简体中文
- 默认已接通 MiMo，可随时切换为其他 OpenAI 兼容模型服务
- 如果翻译失败，会把失败原因写进钉钉消息，再附原文发送，避免静默失败

### 飞书

- 支持把未投递的帖子推送到飞书群聊
- 可附带首楼截图

## SQLite 数据结构

主要表如下：

- `threads`：发现到的线程队列与状态
- `posts`：最终提取结果，一帖一行
- `cursors`：每个 forum 的增量游标
- `deliveries`：消息投递去重记录
- `replies`：自动回帖记录

`posts` 表核心字段：

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

## 调度建议

典型部署方式是每小时运行一次。

Windows 任务计划示例：

```text
Program/script: python
Arguments: E:\code\py\dark_forums\run.py
Start in: E:\code\py\dark_forums
```

## 使用建议

- `latest_page_only=1` 适合高频轮询
- `scrape_workers` 建议从 `2` 到 `4` 开始，速度和风控更平衡
- 同一个 worker 数同时用于 forum 发现并发和帖子抓取并发
- `full_site_mode` 会显著增加抓取量，建议谨慎开启
- 如果浏览器拉起受限，优先在正常本地 shell 中运行
- OpenAI 兼容翻译可以通过 `OPENAI_COMPAT_USE_PROXY=1/0` 独立控制是否走代理
- 在这台机器上，翻译模型通常走 `OPENAI_COMPAT_PROXY_SERVER` 或 `MIMO_PROXY_SERVER` 更稳定

## 安全说明

- 如果历史上曾把真实密钥写入可追踪文件，请先轮换密钥
- `.env.local`、SQLite 数据、截图、日志、浏览器登录态都不要提交到 Git
- 使用前请自行评估目标站点条款、账号风险和当地法律要求
