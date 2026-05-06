# darkforums_sqlite_to_ck

这是一个**独立**的数据处理程序目录，用于在另一台机器上对 `SQLite` 中的 `posts` 表做大模型处理，然后逐条写入 `ClickHouse`。

重要约束：

- 本目录**不引用**本仓库任何现有 scraper 代码（例如 `src/dark_forums/*`）。
- 只读取 `SQLite` 文件；输出写入 ClickHouse。

## 功能

- 从 SQLite 的 `posts` 表按 `scraped_at` **增量读取**
- 调用 Qwen（HTTP，compatible-mode）
  - 翻译：
    - `title` -> `title_zh`
    - `first_post_text` -> `first_post_text_zh`
  - 抽取：
    - `industry`
    - `data_volume`
    - `leaked_organization`
    - `is_china_related` (0/1)
    - `country`
    - `region`
- 逐条写入 ClickHouse 表：`data_leak_darkforums`
- 断点续跑：把最新成功处理的 `scraped_at` 和 `url` 写回配置文件的 `sqlite.start_scraped_at`、`sqlite.start_url`

## 安装

在本目录下创建虚拟环境并安装依赖：

```bash
pip install -r requirements.txt
```

## 配置

复制一份配置：

- 把 `config.example.json` 复制为 `config.json`
- 修改以下字段：

### SQLite

- `sqlite.db_path`
  - SQLite 文件路径（可以相对路径）
- `sqlite.batch_size`
  - 每次最多处理多少行
- `sqlite.start_scraped_at`
  - 断点游标（ISO 格式字符串）。空表示从最早开始
- `sqlite.start_url`
  - 与 `start_scraped_at` 组成复合游标；当多条记录拥有相同 `scraped_at` 时，用于继续从同一时间戳下的下一条 `url` 接着处理，避免遗漏

### Qwen

- `qwen.base_url`
  - 默认：`https://dashscope.aliyuncs.com/compatible-mode/v1`
- `qwen.api_key`
  - 你的 API Key
- `qwen.model`
  - 例如：`qwen-plus`
- `qwen.temperature`
  - 建议较低，例如 `0.2`，减少幻觉
- `qwen.timeout_seconds`
  - 单次 LLM 请求超时秒数；超时后会按重试策略重试，避免单条无限卡住
- `qwen.max_retries`
  - 单条记录的 LLM 最大重试次数
- `qwen.retry_backoff_seconds`
  - LLM 请求失败后的退避基础秒数；后续重试会逐步增加等待时间
- `qwen.first_post_text_max_chars`
  - 单次发送给 LLM 的 `first_post_text` 最大字符数；首轮会优先按这个上限截断超长正文
- `qwen.first_post_text_min_chars`
  - 多次重试时 `first_post_text` 允许缩短到的最小字符数，避免截得过短导致完全失真
- `qwen.first_post_text_retry_reduction_ratio`
  - 当某次 LLM 请求失败或超时，下一次重试时 `first_post_text` 的缩短比例
  - 缩短时不是只保留前缀，而是优先保留 `first_post_text` 的前段、中段、末段，以兼顾主题、正文关键信息和结尾说明

### ClickHouse

- `clickhouse.base_url`
  - 例如：`http://127.0.0.1:8123`
- `clickhouse.user` / `clickhouse.password`
- `clickhouse.database`
- `clickhouse.table`
  - 默认可填写分布式表：`data_leak_darkforums`
- `clickhouse.backfill_missing_only`
  - `true` 时，程序会先按 `url` 查询 ClickHouse；仅当 CK 中不存在该 `url` 时才调用大模型并写入，用于把 SQLite 中 CK 缺失的数据全部补齐

### 运行

- `run.dry_run`
  - `true` 时不入库，只打印处理结果是否成功
- `run.max_rows`
  - `0` 表示不限制；用于临时调试
- `run.worker_count`
  - 并发处理线程数。`1` 表示串行；大于 `1` 时会并发执行 CK 已存在检查、LLM 调用和入库请求，用于提升吞吐
- `run.log_llm_input`
  - `true` 时打印发送给 LLM 的源数据，便于排查某条原始输入为什么导致模型慢、超时或输出异常
- `run.log_llm_input_max_chars`
  - 单条 LLM 输入日志的最大字符数，超出会截断，避免正文过长把日志刷爆

## 运行

```bash
python main.py --config config.json
```

程序会：

- 从复合游标 `start_scraped_at + start_url` 之后读取 `batch_size` 条
- 每条调用一次 LLM
- 每条单独写入 ClickHouse，且使用显式列名插入，不会向 ClickHouse 显式写入 `_version`
- 如果本批次有成功写入，会把最后一条成功记录的 `scraped_at + url` 写回游标；若中途失败，后续会从失败点前最后一条成功记录继续，避免遗漏

## 速度优化建议

- 最主要瓶颈通常是 LLM HTTP 调用，而不是 SQLite 本身
- 当 `clickhouse.backfill_missing_only=true` 时，每条还会额外增加一次 CK `url` 是否存在查询，因此会更慢一些
- `batch_size` 变大不一定更快：如果瓶颈在 LLM/网络，单批堆太多任务只会造成更长排队、更高并发争用、更慢的失败恢复
- 对于超长正文或包含大量个人信息明细的帖子，程序现在会优先做快速提取；若 LLM 失败或超时，会在重试时自动按“前段 + 中段 + 末段采样”的方式缩短 `first_post_text`，直到最小长度阈值
- 可以优先尝试：
  - 降低模型延迟（换更快模型）
  - 保持较低 `temperature`（如 `0.1` ~ `0.2`）
  - 增大 `run.worker_count`（例如 `3`、`5`、`8`，按内网模型承载能力逐步提升）
- 程序现在会打印更详细的阶段日志：
  - CK exists 检查开始/返回/耗时
  - LLM 请求开始/返回/失败重试/耗时
  - CK 入库开始/返回/耗时
  - 每条记录总耗时
- 注意：并发开大后，如果某条中途失败，后面的并发任务可能已经成功写入；程序仍会按安全游标策略从失败点前继续，依赖 CK 按 `url` 去重避免重复影响结果

## 失败处理策略（重要）

- ClickHouse 写入是**逐条**写入
- 任意一条失败：
  - 会打印该条失败原因
  - 本次 run 结束后**不更新游标**（避免跳过失败记录，保证下次可重试）

## 注意

- 本程序假设 SQLite 的 `posts` 表包含字段：
  - `url,title,created_at,author_*,scraped_at,first_post_text,download_urls_json,screenshot_path`
- `created_at/scraped_at` 会被转换成 ClickHouse 需要的时间字符串格式（UTC）
- `_version` 由 ClickHouse 表端自动生成；本程序不会显式写入该列
- 目标 ClickHouse 字段包括：
  - `url,title,title_zh,created_at,author_name,author_posts,author_threads,author_joined,author_reputation,author_contacts,scraped_at,first_post_text,first_post_text_zh,is_china_related,leaked_organization,data_volume,industry,country,region,download_urls_json,screenshot_path`
