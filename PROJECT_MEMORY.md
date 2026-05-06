# 项目记忆文件（交接/总结/进度）

## 目标与范围
- 目标：每天自动从目标站点获取“需要登录且需要回帖后才能看到/继续翻页获取”的帖子链接，并抓取帖子原内容落地到本地。
- 输出：
  - 链接列表（含抓取时间、状态、来源版块/关键词等）
  - 帖子内容（HTML/清洗后的文本/附件信息等，按帖子ID或URL哈希组织）
  - 运行日志（成功/失败原因、重试次数、被风控/验证码提示等）

## 持续化运行（每小时）
- 运行方式：脚本不需要常驻，建议每小时触发一次执行。
- 每次执行：
  - 发现近 N 小时内的新帖（默认 24h，`DARKFORUMS_MAX_AGE_HOURS`）并写入 `threads`
  - 从 `threads` 队列抓取并写入 `posts`
  - 对未推送的 `posts` 执行飞书推送（可选）

## 运行环境（Python 版本）
- Python：建议 3.12（当前开发/运行环境为 conda `py3129`）。

## 技术栈与依赖
- 自动化浏览器：Playwright（Chromium）
- HTML 解析：BeautifulSoup4 + lxml
- 配置：python-dotenv（读取 `.env`）
- 存储：SQLite（`data/db.sqlite`）
- 推送：飞书机器人（开放平台 App，走 HTTP API）

## 关键难点（必须优先验证）
- 登录流程：是否有验证码/2FA/邮箱验证/滑块等；是否支持 cookie 复用。
- “回帖解锁”：
  - 解锁条件是“回复一次”还是“每页/每帖都要回复”？
  - 回帖内容是否有频率限制、敏感词、审核延迟。
- 反爬与风控：请求频率、UA/指纹、IP限制、Cloudflare/JS Challenge。

## 推荐总体架构（可长期运行）
- 建议用 Playwright 做“登录 + 回帖 + 打开帖子 + 获取完整 DOM”，原因：
  - 这类站点往往有动态渲染、JS校验、跳转、token、回帖表单隐藏字段。
  - Playwright 更容易稳定复现“真人浏览器”行为。
- 抓取流程拆分：
  1) 发现阶段：从版块/搜索页/订阅页持续滚动/翻页，收集帖子链接。
  2) 解锁阶段：进入帖子，若提示“回复可见”，执行回帖并等待可见内容出现。
  3) 抓取阶段：提取正文/楼层/图片/附件链接，保存原始HTML与结构化字段。
  4) 去重与增量：以 URL 或帖子ID 做唯一键；按最后更新时间/楼层数做增量更新。
- 存储建议：
  - 元数据：SQLite（简单、可移植、够用）
  - 内容：文件系统（按日期/版块/帖子ID分目录），并保留原始HTML

## 当前实现的整体框架（代码结构与职责）
- 入口：
  - `run.py`：本地直接运行入口
  - `src/dark_forums/__main__.py`：`python -m dark_forums` 入口
- 配置：
  - `src/dark_forums/config.py`：读取 `.env` 并构造 `Settings`
- 浏览器与登录：
  - `src/dark_forums/browser.py`：启动/关闭浏览器、创建 page、随机延迟
  - `src/dark_forums/auth.py`：登录、存储/复用 `storage_state.json`，并对 browser_check 做检测/重试
- 发现：
  - `src/dark_forums/discover.py`：从 forum 列表页翻页发现 thread URL（支持“只抓今天”策略）
- 抓取与解析：
  - `src/dark_forums/scrape.py`：
    - 打开帖子
    - 检测“回复可见”并自动回帖解锁后重试提取
    - 提取：标题、创建时间、#1 楼正文、作者信息、download 链接
    - 检测 browser_check 并尝试 mouse/keypress 绕过；失败则保存 debug HTML
  - `src/dark_forums/text_extract.py`：HTML -> 纯文本辅助
- 存储：
  - `src/dark_forums/db.py`：SQLite schema 初始化与写入（最终表为 `posts`，一帖一行，仅成功时写入）
- 任务编排：
  - `src/dark_forums/runner.py`：
    - 登录
    - 发现 URL 写入 `threads` 队列
    - 逐个抓取并做“完整性校验”
    - 仅在必填字段齐全且 download 链接非空时写入 `posts`
    - 可选：将未推送的 `posts` 推送到飞书，并在 `deliveries` 中做去重记录

- 推送：
  - `src/dark_forums/feishu.py`：
    - 获取 `tenant_access_token`
    - 上传 1 楼截图（如果存在）
    - 发送群消息（post message）

## 目录结构建议（草案）
- src/
  - config.py（加载配置/环境变量）
  - auth.py（登录、cookie持久化）
  - discover.py（列表页抓取与链接收集）
  - unlock.py（回帖解锁策略）
  - scrape.py（帖子内容解析与落地）
  - store.py（SQLite + 文件存储封装）
  - scheduler.py（每日任务入口）
- data/
  - db.sqlite
  - raw_html/...
  - posts/...
- logs/...

## 数据落库结构（最终结果：一帖一行）
- SQLite：`data/db.sqlite`
- 最终表：`posts`
  - `url`：帖子 URL（主键）
  - `title`：帖子标题
  - `created_at`：帖子创建时间（优先取 `<time datetime>`，否则为页面文本）
  - `author_name`：作者名（必填）
  - `author_posts` / `author_threads` / `author_joined` / `author_reputation` / `author_contacts`：作者信息（可空）
  - `scraped_at`：抓取时间（UTC ISO）
  - `first_post_text`：#1 楼正文（纯文本）
  - `download_urls_json`：下载链接数组 JSON（必填且非空）
- 写入规则：只有当 `title/created_at/author_name/first_post_text` 非空且 `download_urls_json` 非空时才写入 `posts`。

## SQLite 三个表（threads / downloads / posts）与字段含义

### 说明
- 目前仅使用 `threads` / `posts` / `cursors` / `deliveries`。
- `downloads` 为旧结构已移除。

### 1) `threads`（抓取队列表：发现 URL、状态机、错误原因）
- `url`：帖子 URL（主键，去重键）
- `discovered_at`：发现该 URL 的时间（ISO 字符串）
- `status`：当前状态
  - `new`：新发现，待抓取
  - `processing`：正在抓取（运行中置为该状态）
  - `failed`：抓取失败（见 `last_error`）
  - `done`：抓取完成（成功写入 `posts` 后置为 done）
- `last_error`：最近一次失败原因（如 `no_download_links` / `incomplete_meta` / `browser_check` / 异常 repr）
- `last_fetched_at`：最近一次打开/抓取该线程页面的时间（用于调试/追踪；旧链路也会写）
- `content_path`：旧链路文本落地路径（当前以 `posts` 单表为准时可不依赖该字段）
- `extracted_at`：提取完成时间（成功写入 `posts` 后写入；用于断点续跑与筛选 pending）
- `downloads_count`：本次提取到的下载链接数量（成功后写入）

### 2) `posts`（最终结果：一帖一行，原子落库）
- `url`：帖子 URL（主键）
- `title`：帖子标题（必填）
- `created_at`：帖子创建时间（必填；优先取 `<time datetime>`，否则为页面文本）
- `author_name`：作者名（必填）
- `author_posts`：作者 Posts 数（可空）
- `author_threads`：作者 Threads 数（可空）
- `author_joined`：作者 Joined 时间（可空）
- `author_reputation`：作者 Reputation（可空）
- `author_contacts`：作者联系方式/外链（可空，按行拼接）
- `scraped_at`：抓取时间（UTC ISO，必填）
- `first_post_text`：#1 楼正文纯文本（必填）
- `download_urls_json`：下载链接数组 JSON（必填且非空）
- `screenshot_path`：1 楼截图相对路径（相对 `data/`，可空）

### 3) `cursors`（按 forum 维度的增量游标）
- `key`：如 `forum:<forum_url>`
- `value`：该版块最近一次发现到的最大 `started_at`（ISO）
- `updated_at`：更新时间

### 4) `deliveries`（推送去重与断点续跑）
- 主键：`(post_url, provider)`
- `post_url`：对应 `posts.url`
- `provider`：推送渠道（当前使用 `feishu`）
- `delivered_at`：推送成功时间
- `message_id`：飞书消息 ID（可空）

### 写入/更新时机（简述）
- **发现阶段**：`discover.py` 发现 URL 后写入 `threads(url, discovered_at, status='new')`（已存在则忽略）。
- **抓取开始**：处理某个 URL 前，将 `threads.status` 置为 `processing`。
- **抓取失败**：将 `threads.status` 置为 `failed`，并写入 `last_error`。
- **抓取成功**：当且仅当抓取字段完整且下载链接非空时：
  - 插入 `posts`（一帖一行，完整数据）
  - 同时将 `threads.status` 置为 `done`，写入 `extracted_at` 与 `downloads_count`。

## 输出位置
- `data/db.sqlite`：SQLite 数据库（含 `threads` 队列表与最终 `posts` 表）
- `logs/YYYY-MM-DD.log`：每日运行日志（同时打印到控制台）
- `data/debug/`：异常/风控/无下载时的 HTML 快照（便于调试）
- `data/screenshots/YYYY-MM-DD/*.png`：1 楼截图

## 运行与调度建议
- Windows（任务计划程序）：每小时触发一次执行 `python .\run.py` 或 `python -m dark_forums`。
- Linux（cron）：每小时执行一次。
- Linux（systemd timer）：建议生产环境使用 timer（可自动重启、日志可控）。
- 需要监控：
  - 失败告警（邮件/Telegram/企业微信任选）
  - 指标（当日新增链接数、成功抓取数、验证码/风控出现次数）

## 合规与账号安全注意
- 不要在代码库硬编码账号/密码；用 `.env` 或系统凭据管理。
- 控制频率、随机等待、遵守站点条款与当地法律。

## 飞书推送配置（可选）
- 环境变量：
  - `FEISHU_ENABLED`：`1` 开启，默认关闭
  - `FEISHU_APP_ID`
  - `FEISHU_APP_SECRET`
  - `FEISHU_CHAT_ID`
  - `FEISHU_MAX_POSTS_PER_RUN`：每次执行最多推送多少条（默认 20）
- 推送策略：
  - 仅推送 `posts` 表中未在 `deliveries(provider='feishu')` 记录过的帖子
  - 推送失败不会影响抓取与入库，下次执行会自动重试未推送的帖子

## 当前进度
- [x] 明确目标站点与登录/回帖规则
- [x] 确定抓取字段与落地格式（纯文本）
- [x] 依赖与配置样例（.env.example / requirements.txt）
- [x] 实现最小可用链路（登录->leaks发现->回帖解锁->保存->SQLite去重）
- [x] 发现入口支持多个 Forum 列表页（通过 DARKFORUMS_FORUM_URLS 配置）
- [x] 每帖保存 1 楼截图到本地，并在 `posts.screenshot_path` 记录相对路径
- [x] 新增 `deliveries` 用于推送去重与断点续跑
- [ ] 加入更稳健的“当天新增”判定（基于 time[datetime]，必要时适配站点展示）
- [ ] 加入风控/验证码检测与告警

## 待你补充的信息（拿到后即可开工实现）
- 如需变更抓取范围：更新 DARKFORUMS_FORUM_URLS（用 | 分隔多个 Forum URL）

## 站点与账号（本地保存，不提交仓库）
- 站点：https://darkforums.su/index.php
- 登录：账号密码
- 注意：账号密码仅放在本地 `.env`，不要写入代码/README/提交到仓库


## ClickHouse 表结构
-- 1. 本地表 (每个节点都创建，支持副本)
CREATE TABLE IF NOT EXISTS data_leak_darkforums_local ON CLUSTER clickhouse_cluster
(
    url                 String        COMMENT '帖子原始URL',
    title               String        COMMENT '帖子标题（原文）',
    title_zh            String        COMMENT '帖子标题（中文翻译）',
    created_at          DateTime      COMMENT '帖子发布时间',
    author_name         String        COMMENT '发帖人用户名',
    author_posts        Int64         COMMENT '发帖人累计发帖数',
    author_threads      Int64         COMMENT '发帖人累计发起线程数',
    author_joined       Date          COMMENT '发帖人注册日期',
    author_reputation   Int64         COMMENT '发帖人信誉积分',
    author_contacts     String        COMMENT '发帖人联系方式（如Telegram链接）',
    scraped_at          DateTime64(6, 'UTC') COMMENT '数据抓取时间（UTC，精确到微秒）',
    first_post_text     String        COMMENT '帖子正文内容（原文）',
    first_post_text_zh  String        COMMENT '帖子正文内容（中文翻译）',
    is_china_related    UInt8         COMMENT '是否与中国相关：0否 1是',
    leaked_organization String        COMMENT '被泄露的单位名称',
    data_volume         String        COMMENT '泄露数据量描述（如 12K++、1.2M）',
    industry            String        COMMENT '所属行业（如 金融、医疗、政府、教育）',
    country             String        COMMENT '数据归属国家（如 China、India、USA）',
    region              String        COMMENT '数据归属区域（如 Asia、Europe、North America）',
    download_urls_json  String        COMMENT '下载链接列表，JSON数组格式',
    screenshot_path     String        COMMENT '帖子截图本地存储路径',

    _version            UInt64 MATERIALIZED toUnixTimestamp64Micro(scraped_at)
                                      COMMENT '去重版本号，自动生成，保留最新抓取记录'
)
ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/{shard}/data_leak_darkforums_local', '{replica}', _version)
PARTITION BY toYYYYMM(created_at)
ORDER BY (url)
SETTINGS index_granularity = 8192;


-- 2. 分布式表
CREATE TABLE IF NOT EXISTS data_leak_darkforums ON CLUSTER clickhouse_cluster
(
    url                 String        COMMENT '帖子原始URL',
    title               String        COMMENT '帖子标题（原文）',
    title_zh            String        COMMENT '帖子标题（中文翻译）',
    created_at          DateTime      COMMENT '帖子发布时间',
    author_name         String        COMMENT '发帖人用户名',
    author_posts        Int64         COMMENT '发帖人累计发帖数',
    author_threads      Int64         COMMENT '发帖人累计发起线程数',
    author_joined       Date          COMMENT '发帖人注册日期',
    author_reputation   Int64         COMMENT '发帖人信誉积分',
    author_contacts     String        COMMENT '发帖人联系方式（如Telegram链接）',
    scraped_at          DateTime64(6, 'UTC') COMMENT '数据抓取时间（UTC，精确到微秒）',
    first_post_text     String        COMMENT '帖子正文内容（原文）',
    first_post_text_zh  String        COMMENT '帖子正文内容（中文翻译）',
    is_china_related    UInt8         COMMENT '是否与中国相关：0否 1是',
    leaked_organization String        COMMENT '被泄露的单位名称',
    data_volume         String        COMMENT '泄露数据量描述（如 12K++、1.2M）',
    industry            String        COMMENT '所属行业（如 金融、医疗、政府、教育）',
    country             String        COMMENT '数据归属国家（如 China、India、USA）',
    region              String        COMMENT '数据归属区域（如 Asia、Europe、North America）',
    download_urls_json  String        COMMENT '下载链接列表，JSON数组格式',
    screenshot_path     String        COMMENT '帖子截图本地存储路径',
    _version            UInt64        COMMENT '去重版本号'
)
ENGINE = Distributed(clickhouse_cluster, default, data_leak_darkforums_local, xxHash64(url));