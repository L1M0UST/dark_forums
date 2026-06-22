from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .auth import login, save_storage_state
from .browser import new_page, start_browser, stop_browser
from .config import Settings
from .db import (
    PostRecord,
    canonicalize_thread_url,
    get_cursor,
    get_thread_created_at,
    has_post,
    has_reply,
    insert_post,
    iter_pending,
    iter_undelivered_posts,
    mark_delivered,
    mark_extracted,
    mark_failed,
    mark_processing,
    mark_replied,
    migrate_thread_url,
    open_db,
    prune_threads,
    set_cursor,
    upsert_discovered,
)
from .dingtalk import DingTalkClient, DingTalkConfig
from .discover import discover_today_threads
from .feishu import FeishuClient, FeishuConfig
from .openai_compat import OpenAICompatConfig, OpenAICompatTranslator
from .scrape import BrowserCheckError, ScrapeResult, scrape_thread_text

_CHINA_KEYWORDS = (
    "中国", "中國", "china", "prc", "cn",
    "北京", "上海", "天津", "重庆", "重慶",
    "香港", "澳门", "澳門", "台湾", "台灣",
    "河北", "山西", "辽宁", "遼寧", "吉林", "黑龙江", "黑龍江",
    "江苏", "江蘇", "浙江", "安徽", "福建", "江西", "山东", "山東",
    "河南", "湖北", "湖南", "广东", "廣東", "海南", "四川", "贵州", "貴州",
    "云南", "雲南", "陕西", "陝西", "甘肃", "甘肅", "青海",
    "内蒙古", "內蒙古", "广西", "廣西", "西藏", "宁夏", "寧夏", "新疆",
    "深圳", "广州", "廣州", "杭州", "南京", "苏州", "蘇州", "武汉", "武漢",
    "成都", "西安", "长沙", "長沙", "青岛", "青島", "厦门", "廈門", "宁波", "寧波",
)

_CHONGQING_KEYWORDS = ("重庆", "重慶", "chongqing")
_REGION_GROUPS = (
    ("重庆", ("重庆", "重慶", "chongqing")),
    ("北京", ("北京", "beijing")),
    ("上海", ("上海", "shanghai")),
    ("天津", ("天津", "tianjin")),
    ("香港", ("香港", "hong kong", "hongkong")),
    ("澳门", ("澳门", "澳門", "macao", "macau")),
    ("台湾", ("台湾", "台灣", "taiwan")),
    ("河北", ("河北",)),
    ("山西", ("山西",)),
    ("辽宁", ("辽宁", "遼寧", "liaoning")),
    ("吉林", ("吉林", "jilin")),
    ("黑龙江", ("黑龙江", "黑龍江", "heilongjiang")),
    ("江苏", ("江苏", "江蘇", "jiangsu")),
    ("浙江", ("浙江", "zhejiang")),
    ("安徽", ("安徽", "anhui")),
    ("福建", ("福建", "fujian")),
    ("江西", ("江西", "jiangxi")),
    ("山东", ("山东", "山東", "shandong")),
    ("河南", ("河南", "henan")),
    ("湖北", ("湖北", "hubei")),
    ("湖南", ("湖南", "hunan")),
    ("广东", ("广东", "廣東", "guangdong")),
    ("广西", ("广西", "廣西", "guangxi")),
    ("海南", ("海南", "hainan")),
    ("四川", ("四川", "sichuan")),
    ("贵州", ("贵州", "貴州", "guizhou")),
    ("云南", ("云南", "雲南", "yunnan")),
    ("陕西", ("陕西", "陝西", "shanxi")),
    ("甘肃", ("甘肃", "甘肅", "gansu")),
    ("青海", ("青海", "qinghai")),
    ("内蒙古", ("内蒙古", "內蒙古", "inner mongolia")),
    ("西藏", ("西藏", "tibet")),
    ("宁夏", ("宁夏", "寧夏", "ningxia")),
    ("新疆", ("新疆", "xinjiang")),
    ("中国", ("中国", "中國", "china", "prc")),
)


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


@dataclass(frozen=True)
class _DiscoverOutcome:
    forum_url: str
    threads: tuple = ()
    error: str | None = None


class _Tee:
    def __init__(self, *streams):
        self._streams = streams
        self._buffer = ""

    @staticmethod
    def _safe_write_stream(st, text: str) -> None:
        try:
            st.write(text)
            return
        except UnicodeEncodeError:
            pass

        try:
            enc = getattr(st, "encoding", None) or "utf-8"
            safe_text = text.encode(enc, errors="replace").decode(enc, errors="replace")
            st.write(safe_text)
            return
        except Exception:
            pass

        try:
            st.write(text.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))
        except Exception:
            pass

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buffer += s
        while True:
            idx = self._buffer.find("\n")
            if idx < 0:
                break
            line = self._buffer[:idx]
            self._buffer = self._buffer[idx + 1 :]
            prefix = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            rendered = f"{prefix}{line}\n"
            for st in self._streams:
                self._safe_write_stream(st, rendered)
        return len(s)

    def flush(self) -> None:
        if self._buffer:
            prefix = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
            rendered = f"{prefix}{self._buffer}"
            for st in self._streams:
                self._safe_write_stream(st, rendered)
            self._buffer = ""
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


def _is_china_related(*parts: str) -> bool:
    haystack = "\n".join([(p or "") for p in parts]).lower()
    return any(keyword.lower() in haystack for keyword in _CHINA_KEYWORDS)


def _is_chongqing_related(*parts: str) -> bool:
    haystack = "\n".join([(p or "") for p in parts]).lower()
    return any(keyword.lower() in haystack for keyword in _CHONGQING_KEYWORDS)


_TRANSLATION_REFUSAL_PATTERNS = (
    "i can't assist",
    "i cannot assist",
    "i’m sorry",
    "i am sorry",
    "sorry, i can't",
    "cannot help with",
    "抱歉",
    "无法帮助",
    "不能帮助",
    "无法协助",
    "不能协助",
    "无法提供",
    "不能提供",
    "违反",
    "policy",
)


_TRANSLATION_WARNING_PATTERNS = (
    "as an ai",
    "i'm unable",
    "i am unable",
    "i cannot provide",
    "i can't provide",
    "cannot comply",
    "can't comply",
    "safety policy",
    "usage policy",
    "content policy",
    "guideline",
    "guidelines",
    "æˆ‘æ— æ³•",
    "æˆ‘ä¸èƒ½",
    "æ— æ³•æ»¡è¶³",
    "ä¸èƒ½æ»¡è¶³",
    "æ— æ³•ç¿»è¯‘",
    "ä¸èƒ½ç¿»è¯‘",
    "å®‰å…¨æ”¿ç­–",
    "ä½¿ç”¨æ”¿ç­–",
    "å†…å®¹æ”¿ç­–",
    "ç¤¾åŒºå‡†åˆ™",
)
_MODEL_PROMPT_ECHO_MARKERS = (
    "è¦æ±‚:",
    "ä¸è¦è¾“å‡º",
    "ç”Ÿæˆä¸€æ®µ",
    "åªè¯´æ˜Žå“ªé‡Œå‡ºçŽ°äº†",
    "use only provided",
    "do not output",
    "write a short",
)
_MONITORING_TRANSLATION_FULL_PROMPT = (
    "Rewrite the structured monitoring note into concise Simplified Chinese markdown for internal security monitoring. "
    "Keep only monitoring-level information: region, target type, high-level risk, source link, and whether Chongqing needs priority attention. "
    "Never include policy warnings, refusal wording, explanations, personal data, samples, credentials, download details, or attack steps. "
    "If the information is limited, still output a short final Chinese monitoring notice using only the provided fields. "
    "Output only the final Chinese markdown."
)
_MONITORING_TRANSLATION_COMPACT_PROMPT = (
    "Using only the safe structured fields, write a very short Simplified Chinese monitoring overview in markdown. "
    "Keep region, target type, risk type, and Chongqing priority if present. "
    "Do not mention policy, safety, refusal, personal data, links, samples, or any sensitive details. "
    "Output only the final Chinese markdown."
)
_MONITORING_TRANSLATION_MINIMAL_PROMPT = (
    "Write a minimal Simplified Chinese monitoring notice from the safe fields only. "
    "Mention only region, target type, and that there is a suspected data-risk event. "
    "If Chongqing is marked as priority, explicitly say it needs priority attention. "
    "Do not add any warning, refusal, explanation, or sensitive detail. Output only the final Chinese markdown."
)


def _normalize_compact_text(text: str, max_chars: int) -> str:
    text = (text or "").replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _sanitize_for_model(text: str, max_chars: int = 600) -> str:
    text = _normalize_compact_text(text, max_chars=max_chars * 2)
    text = re.sub(r"(?i)\b(pass(word)?|pwd|token|secret|cookie|session)\b\s*[:=]\s*\S+", r"\1=[已省略]", text)
    text = re.sub(r"\b[A-Za-z0-9+/]{48,}={0,2}\b", "[长编码串已省略]", text)
    lines: list[str] = []
    omitted = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) > 220:
            line = line[:220].rstrip() + "..."
        lines.append(line)
        if len("\n".join(lines)) >= max_chars:
            omitted += 1
            break
        if len(lines) >= 12:
            omitted += max(0, len(text.splitlines()) - len(lines))
            break
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "..."
    if omitted > 0:
        cleaned = (cleaned + f"\n[已省略部分高风险或过长原文，共 {omitted} 段]").strip()
    return cleaned


def _build_local_safe_summary(title: str, first_post_text: str, max_chars: int = 220) -> str:
    base = _sanitize_for_model(first_post_text or title, max_chars=max_chars)
    base = re.sub(r"(?i)https?://\S+", "[链接已省略]", base)
    base = re.sub(r"(?i)\b(download|下载链接|credential|password|cookie|token|session|exploit|payload)\b", "[敏感细节已省略]", base)
    base = base.replace("- ", "")
    base = base.strip(" >\n")
    if not base:
        base = "检测到一条与中国相关的疑似泄露或风险信息，具体敏感细节已省略。"
    return base[:max_chars].rstrip() + ("..." if len(base) > max_chars else "")


def _extract_region_labels(*parts: str, limit: int = 3) -> list[str]:
    haystack = "\n".join([(p or "") for p in parts]).lower()
    found: list[str] = []
    for label, keywords in _REGION_GROUPS:
        if any(keyword.lower() in haystack for keyword in keywords):
            found.append(label)
        if len(found) >= limit:
            break
    if len(found) > 1 and "中国" in found:
        found = [label for label in found if label != "中国"]
    return found or ["中国"]


def _infer_target_scope(*parts: str) -> str:
    haystack = "\n".join([(p or "") for p in parts]).lower()
    scope_rules = (
        ("政府或公共机构", ("政府", "公安", "法院", "税务", "gov", "government", "ministry", "bureau")),
        ("教育机构", ("大学", "学院", "学校", "教育", "university", "college", "school", "campus")),
        ("医疗机构", ("医院", "医疗", "clinic", "hospital", "medical", "healthcare")),
        ("金融机构", ("银行", "证券", "保险", "支付", "bank", "finance", "financial", "payment")),
        ("通信服务", ("运营商", "通信", "telecom", "isp", "mobile", "broadband")),
        ("互联网平台", ("平台", "电商", "网站", "app", "platform", "ecommerce", "saas")),
        ("企业机构", ("企业", "公司", "集团", "厂商", "company", "corp", "corporation", "enterprise")),
    )
    for label, keywords in scope_rules:
        if any(keyword.lower() in haystack for keyword in keywords):
            return label
    return "未知机构"


def _build_monitoring_overview(*, title: str, first_post_text: str, chongqing_related: bool) -> str:
    regions = "、".join(_extract_region_labels(title, first_post_text))
    target_scope = _infer_target_scope(title, first_post_text)
    lines = []
    if chongqing_related:
        lines.append("## 重庆相关重点提示")
        lines.append("该条内容命中了重庆相关关键词，请优先关注。")
        lines.append("")
    lines.append("## 监控概览")
    lines.append(f"- 关联地区: {regions}")
    lines.append(f"- 关联对象: {target_scope}")
    lines.append("- 风险类型: 疑似信息外泄或数据风险")
    lines.append("- 说明: 仅保留监控所需概览信息，个人隐私、样本、字段、下载方式及其他敏感细节均已省略，请进入原帖人工核验。")
    return "\n".join(lines)


def _build_notification_title(*, title: str, first_post_text: str, chongqing_related: bool) -> str:
    prefix = "重庆相关监控通报" if chongqing_related else "中国相关监控通报"
    return prefix[:128]


def _looks_like_translation_refusal(text: str) -> bool:
    haystack = (text or "").strip().lower()
    if not haystack:
        return True
    return any(pattern in haystack for pattern in _TRANSLATION_REFUSAL_PATTERNS)


def _count_han_chars(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _extract_field_value(source_text: str, field_name: str) -> str:
    for line in (source_text or "").replace("\r", "").splitlines():
        if line.startswith(f"{field_name}:"):
            return line.split(":", 1)[1].strip()
    return ""


def _build_translation_attempts(model_input_text: str, *, chongqing_related: bool) -> list[tuple[str, str, str]]:
    lines = [line.strip() for line in (model_input_text or "").replace("\r", "").splitlines() if line.strip()]

    def _value_from_line(line: str, default: str = "") -> str:
        if ":" not in line:
            return default
        return line.split(":", 1)[1].strip() or default

    post_url = _value_from_line(lines[0], "") if len(lines) >= 1 else ""
    created_at = _value_from_line(lines[1], "") if len(lines) >= 6 else ""
    region = _value_from_line(lines[-5], "ä¸­å›½") if len(lines) >= 5 else "ä¸­å›½"
    target_scope = _value_from_line(lines[-4], "æœªçŸ¥æœºæž„") if len(lines) >= 4 else "æœªçŸ¥æœºæž„"
    risk_type = _value_from_line(lines[-3], "ç–‘ä¼¼ä¿¡æ¯å¤–æ³„æˆ–æ•°æ®é£Žé™©") if len(lines) >= 3 else "ç–‘ä¼¼ä¿¡æ¯å¤–æ³„æˆ–æ•°æ®é£Žé™©"
    priority = "æ˜¯" if chongqing_related else "å¦"

    compact_lines = [
        f"å…³è”åœ°åŒº: {region}",
        f"å…³è”å¯¹è±¡: {target_scope}",
        f"é£Žé™©ç±»åž‹: {risk_type}",
        f"é‡åº†é‡ç‚¹æç¤º: {priority}",
    ]
    minimal_lines = [
        f"åœ°åŒº: {region}",
        f"å¯¹è±¡: {target_scope}",
        "äº‹ä»¶: ç–‘ä¼¼æ•°æ®é£Žé™©",
        f"é‡ç‚¹å…³æ³¨é‡åº†: {priority}",
    ]
    if post_url:
        compact_lines.insert(0, f"å¸–å­é“¾æŽ¥: {post_url}")
    if created_at:
        compact_lines.insert(1 if post_url else 0, f"æ—¶é—´: {created_at}")

    return [
        ("full", _MONITORING_TRANSLATION_FULL_PROMPT, model_input_text.strip()),
        ("compact", _MONITORING_TRANSLATION_COMPACT_PROMPT, "\n".join(compact_lines).strip()),
        ("minimal", _MONITORING_TRANSLATION_MINIMAL_PROMPT, "\n".join(minimal_lines).strip()),
    ]


def _evaluate_translation_result(raw_text: str, cleaned_text: str) -> list[str]:
    reasons: list[str] = []
    raw = (raw_text or "").strip()
    cleaned = (cleaned_text or "").strip()
    raw_lower = raw.lower()
    cleaned_lower = cleaned.lower()

    if not raw:
        reasons.append("æ¨¡åž‹è¿”å›žä¸ºç©º")
    if _looks_like_translation_refusal(raw) or _looks_like_translation_refusal(cleaned):
        reasons.append("å‘½ä¸­æ‹’ç­”ç‰¹å¾")
    if any(pattern in raw_lower or pattern in cleaned_lower for pattern in _TRANSLATION_WARNING_PATTERNS):
        reasons.append("å‘½ä¸­æ¨¡åž‹è­¦å‘Šç‰¹å¾")
    echo_hits = sum(1 for marker in _MODEL_PROMPT_ECHO_MARKERS if marker in raw_lower or marker in raw)
    if echo_hits >= 2:
        reasons.append("ç–‘ä¼¼å›žæ˜¾æç¤ºè¯")
    if not cleaned:
        reasons.append("æ¸…æ´—åŽå†…å®¹ä¸ºç©º")

    han_count = _count_han_chars(cleaned)
    ascii_alpha_count = len(re.findall(r"[A-Za-z]", cleaned))
    if han_count < 8:
        reasons.append("ä¸­æ–‡å†…å®¹è¿‡å°‘")
    if ascii_alpha_count > max(12, han_count * 2):
        reasons.append("è‹±æ–‡å æ¯”è¿‡é«˜")

    success_markers = ("ç›‘æŽ§æ¦‚è§ˆ", "å…³è”åœ°åŒº", "å…³è”å¯¹è±¡", "é£Žé™©ç±»åž‹", "é‡ç‚¹æç¤º", "ä¼˜å…ˆå…³æ³¨")
    if not any(marker in cleaned for marker in success_markers):
        reasons.append("ç¼ºå°‘ç›‘æŽ§æ¦‚è§ˆå…³é”®å­—æ®µ")

    return reasons


def _clean_translated_notification_text(text: str, *, original_title: str, translated_title: str, chongqing_related: bool) -> str:
    lines = [(line or "").strip() for line in (text or "").replace("\r", "").splitlines()]
    lines = [line for line in lines if line]

    start_idx = 0
    field_markers = (
        "帖子链接:",
        "post link:",
        "作者:",
        "author:",
        "时间:",
        "time:",
        "下载链接:",
        "download link:",
        "首楼摘要:",
        "summary:",
        "提示:",
        "note:",
    )
    for idx, line in enumerate(lines):
        low = line.lower()
        if low.startswith(field_markers):
            start_idx = idx
            break
    cleaned = lines[start_idx:] if lines else []

    filtered: list[str] = []
    for line in cleaned:
        low = line.lower()
        if line == original_title or line == translated_title:
            continue
        if low.startswith("the title mentions "):
            continue
        if low.startswith("the post link "):
            continue
        if low.startswith("there's a hint "):
            continue
        if low.startswith("the summary mentions "):
            continue
        if low.startswith("this appears to be "):
            continue
        if low.startswith("the user wants me "):
            continue
        if low.startswith("let me translate "):
            continue
        if low.startswith("提示:") or low.startswith("note:"):
            continue
        filtered.append(line)

    body = "\n".join(filtered).strip()
    if chongqing_related:
        highlight = "## 重庆相关重点提示\n该条内容命中了重庆相关关键词，请优先关注。"
        if body:
            body = f"{highlight}\n\n{body}"
        else:
            body = highlight
    return body


def _build_dingtalk_raw_markdown(
    *,
    post_url: str,
    title: str,
    created_at: str,
    author_name: str,
    first_post_text: str,
    download_urls: list[str],
    chongqing_related: bool,
) -> str:
    parts: list[str] = ["[监控原帖链接](" + post_url + ")"]
    if created_at:
        parts.append(f"\n时间: {created_at}")
    parts.append("")
    parts.append(
        _build_monitoring_overview(
            title=title,
            first_post_text=first_post_text,
            chongqing_related=chongqing_related,
        )
    )
    return "\n".join(parts)


def _build_translation_input(
    *,
    post_url: str,
    title: str,
    created_at: str,
    author_name: str,
    first_post_text: str,
    download_urls: list[str],
    chongqing_related: bool,
) -> str:
    regions = "、".join(_extract_region_labels(title, first_post_text))
    target_scope = _infer_target_scope(title, first_post_text)
    parts: list[str] = []
    parts.append(f"帖子链接: {post_url}")
    if created_at:
        parts.append(f"时间: {created_at}")
    parts.append(f"关联地区: {regions}")
    parts.append(f"关联对象: {target_scope}")
    parts.append("风险类型: 疑似信息外泄或数据风险")
    parts.append(f"重庆重点提示: {'是' if chongqing_related else '否'}")
    parts.append("要求: 生成一段简洁、合规、易懂的中文监控概览，只说明哪里出现了疑似泄露风险以及需要关注的对象类型；不要输出任何个人隐私、姓名、账号、邮箱、手机号、身份证号、下载链接、数据字段、样本内容、凭证、口令、Cookie、Token、利用步骤或其他敏感细节。")
    return "\n".join(parts).strip()


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


def _discover_forum_worker(
    settings: Settings,
    storage_state_path: Path,
    forum_url: str,
    cursor_iso: str | None,
) -> _DiscoverOutcome:
    session = start_browser(
        headless=settings.headless,
        storage_state_path=storage_state_path,
        proxy_server=settings.proxy_server,
    )
    try:
        page = new_page(session)
        threads = tuple(
            discover_today_threads(
                page,
                forum_url,
                max_pages=(1 if settings.latest_page_only else settings.max_pages_per_forum),
                only_today=(False if (settings.full_site_mode or settings.latest_page_only) else settings.only_today),
                max_age_hours=(settings.max_age_hours if not (settings.full_site_mode or settings.latest_page_only) else 24 * 365 * 100),
                sort_query=settings.forum_sort_query,
                cursor_iso=(None if settings.latest_page_only else cursor_iso),
            )
        )
        return _DiscoverOutcome(forum_url=forum_url, threads=threads)
    except Exception as e:
        return _DiscoverOutcome(forum_url=forum_url, error=repr(e))
    finally:
        stop_browser(session)


def _translate_for_dingtalk(
    translator: OpenAICompatTranslator | None,
    notification_title: str,
    model_input_text: str,
    raw_fallback_text: str,
    *,
    chongqing_related: bool,
) -> tuple[str, str, str | None]:
    send_title = notification_title[:128]
    send_text = raw_fallback_text
    if translator is None:
        if chongqing_related and "## 重庆相关重点提示" not in send_text:
            send_text = "## 重庆相关重点提示\n该条内容命中了重庆相关关键词，请优先关注。\n\n" + send_text
        return send_title, send_text, None
    try:
        translated_text = translator.translate_markdown_to_zh(model_input_text).strip()
        if _looks_like_translation_refusal(translated_text):
            raise RuntimeError(f"正文翻译疑似拒答：{translated_text}")
        send_text = _clean_translated_notification_text(
            translated_text,
            original_title=notification_title,
            translated_title=notification_title,
            chongqing_related=chongqing_related,
        )
        if not send_text.strip():
            raise RuntimeError(f"正文翻译清洗后为空：{translated_text}")
        return send_title, send_text, None
    except Exception as e:
        err = repr(e)
        fallback_title = f"[翻译失败] {notification_title}"[:128]
        fallback_text = (
            "## 翻译失败\n"
            "模型翻译失败或触发风控，已回退发送本地整理后的原文内容。\n\n"
            f"- 模型: `{translator.model_name}`\n"
            f"- 原因: `{err}`\n\n"
            "---\n\n"
            f"{('## 重庆相关重点提示\\n该条内容命中了重庆相关关键词，请优先关注。\\n\\n' if chongqing_related and '## 重庆相关重点提示' not in raw_fallback_text else '')}{raw_fallback_text}"
        )
        return fallback_title, fallback_text, err


def _build_translation_attempts(model_input_text: str, *, chongqing_related: bool) -> list[tuple[str, str, str]]:
    lines = [line.strip() for line in (model_input_text or "").replace("\r", "").splitlines() if line.strip()]

    def _value_from_line(line: str, default: str = "") -> str:
        if ":" not in line:
            return default
        return line.split(":", 1)[1].strip() or default

    post_url = _value_from_line(lines[0], "") if len(lines) >= 1 else ""
    created_at = _value_from_line(lines[1], "") if len(lines) >= 6 else ""
    region = _value_from_line(lines[-5], "China") if len(lines) >= 5 else "China"
    target_scope = _value_from_line(lines[-4], "organization") if len(lines) >= 4 else "organization"
    risk_type = _value_from_line(lines[-3], "suspected data risk") if len(lines) >= 3 else "suspected data risk"
    priority = "yes" if chongqing_related else "no"

    compact_lines = [
        f"Region: {region}",
        f"Target type: {target_scope}",
        f"Risk type: {risk_type}",
        f"Chongqing priority: {priority}",
    ]
    minimal_lines = [
        f"Region: {region}",
        f"Target: {target_scope}",
        "Event: suspected data risk",
        f"Priority Chongqing: {priority}",
    ]
    if post_url:
        compact_lines.insert(0, f"Source link: {post_url}")
    if created_at:
        compact_lines.insert(1 if post_url else 0, f"Time: {created_at}")

    return [
        ("full", _MONITORING_TRANSLATION_FULL_PROMPT, model_input_text.strip()),
        ("compact", _MONITORING_TRANSLATION_COMPACT_PROMPT, "\n".join(compact_lines).strip()),
        ("minimal", _MONITORING_TRANSLATION_MINIMAL_PROMPT, "\n".join(minimal_lines).strip()),
    ]


def _evaluate_translation_result(raw_text: str, cleaned_text: str) -> list[str]:
    reasons: list[str] = []
    raw = (raw_text or "").strip()
    cleaned = (cleaned_text or "").strip()
    raw_lower = raw.lower()
    cleaned_lower = cleaned.lower()

    if not raw:
        reasons.append("模型返回为空")
    if _looks_like_translation_refusal(raw) or _looks_like_translation_refusal(cleaned):
        reasons.append("命中拒答特征")
    if any(pattern in raw_lower or pattern in cleaned_lower for pattern in _TRANSLATION_WARNING_PATTERNS):
        reasons.append("命中模型警告特征")
    echo_hits = sum(1 for marker in _MODEL_PROMPT_ECHO_MARKERS if marker in raw_lower or marker in raw)
    if echo_hits >= 2:
        reasons.append("疑似回显提示词")
    if not cleaned:
        reasons.append("清洗后内容为空")

    han_count = _count_han_chars(cleaned)
    ascii_alpha_count = len(re.findall(r"[A-Za-z]", cleaned))
    if han_count < 8:
        reasons.append("中文内容过少")
    if ascii_alpha_count > max(12, han_count * 2):
        reasons.append("英文占比过高")

    has_structure = "##" in cleaned or "- " in cleaned or "\n" in cleaned
    if not has_structure and han_count < 20:
        reasons.append("缺少结构化监控概览")

    return reasons


def _translate_for_dingtalk_v2(
    translator: OpenAICompatTranslator | None,
    notification_title: str,
    model_input_text: str,
    raw_fallback_text: str,
    *,
    chongqing_related: bool,
) -> tuple[str, str, str | None]:
    send_title = notification_title[:128]
    send_text = raw_fallback_text
    if translator is None:
        if chongqing_related and "重点提示" not in send_text:
            send_text = "## 重庆相关重点提示\n该条内容命中了重庆相关关键词，请优先关注。\n\n" + send_text
        return send_title, send_text, None

    attempt_errors: list[str] = []
    attempts = _build_translation_attempts(model_input_text, chongqing_related=chongqing_related)
    for idx, (strategy_name, system_prompt, attempt_input) in enumerate(attempts, start=1):
        print(f"[翻译] 开始策略 {idx}/{len(attempts)}：{strategy_name}，输入长度={len(attempt_input)}")
        try:
            translated_text = translator.complete(system_prompt, attempt_input).strip()
            send_text = _clean_translated_notification_text(
                translated_text,
                original_title=notification_title,
                translated_title=notification_title,
                chongqing_related=chongqing_related,
            )
            failed_reasons = _evaluate_translation_result(translated_text, send_text)
            if failed_reasons:
                err = f"策略 {strategy_name} 未通过校验：{'；'.join(failed_reasons)}"
                print(f"[翻译] {err}")
                attempt_errors.append(err)
                continue
            print(f"[翻译] 策略 {strategy_name} 成功，已通过翻译校验。")
            return send_title, send_text, None
        except Exception as e:
            err = f"策略 {strategy_name} 请求失败：{repr(e)}"
            print(f"[翻译] {err}")
            attempt_errors.append(err)

    err = " | ".join(attempt_errors) if attempt_errors else "模型未返回可用翻译结果"
    fallback_text = raw_fallback_text + "\n\n> 说明: 模型翻译未通过成功校验，已自动回退为本地生成的中文监控概览。"
    return send_title, fallback_text, err


def _prepare_post_content_for_translation_v3(first_post_text: str, *, max_chars: int = 1800) -> str:
    text = (first_post_text or "").replace("\r", "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _reduce_post_content_for_retry_v3(first_post_text: str, *, max_chars: int = 1200) -> str:
    text = (first_post_text or "").replace("\r", "").strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        return _prepare_post_content_for_translation_v3(first_post_text, max_chars=max_chars)
    if len(paragraphs) == 1:
        reduced = paragraphs[0]
    else:
        reduced = paragraphs[0]
        if paragraphs[-1] != paragraphs[0]:
            reduced = reduced + "\n\n" + paragraphs[-1]
    reduced = re.sub(r"\n{3,}", "\n\n", reduced)
    if len(reduced) > max_chars:
        reduced = reduced[:max_chars].rstrip() + "..."
    return reduced


def _looks_like_failed_component_translation_v3(
    translated_text: str,
    *,
    original_text: str,
    component_name: str,
) -> list[str]:
    reasons: list[str] = []
    raw = (translated_text or "").strip()
    raw_lower = raw.lower()
    original = (original_text or "").strip()
    original_lower = original.lower()

    if not raw:
        reasons.append(f"{component_name}返回为空")
        return reasons
    if _looks_like_translation_refusal(raw):
        reasons.append(f"{component_name}命中拒答特征")
    if any(pattern in raw_lower for pattern in _TRANSLATION_WARNING_PATTERNS):
        reasons.append(f"{component_name}命中模型警告特征")
    if sum(1 for marker in _MODEL_PROMPT_ECHO_MARKERS if marker in raw_lower or marker in raw) >= 2:
        reasons.append(f"{component_name}疑似回显提示词")

    han_count = _count_han_chars(raw)
    ascii_alpha_count = len(re.findall(r"[A-Za-z]", raw))
    if component_name == "标题":
        if han_count < 2 and _count_han_chars(original) < 2:
            reasons.append("标题中文内容过少")
        if raw_lower == original_lower and _count_han_chars(original) < 2:
            reasons.append("标题疑似未翻译")
    else:
        if han_count < 10:
            reasons.append("正文中文内容过少")
        if raw_lower == original_lower and _count_han_chars(original) < 10:
            reasons.append("正文疑似未翻译")
        if ascii_alpha_count > max(24, han_count * 2):
            reasons.append("正文英文占比过高")
    return reasons


def _translate_title_component_v3(
    translator: OpenAICompatTranslator | None,
    title: str,
) -> tuple[str, str | None]:
    original_title = (title or "").strip()
    if not original_title:
        return "", "标题为空"
    if translator is None:
        return original_title, "未配置大模型，标题直接使用原文"

    system_prompt = (
        "Translate the forum post title into natural Simplified Chinese for internal security monitoring. "
        "Preserve proper nouns, malware family names, actor names, organization names, product names, CVE IDs, counts, URLs, and technical abbreviations. "
        "Use natural Chinese wording and do not translate too literally. Output only the translated title."
    )

    attempt_errors: list[str] = []
    try:
        translated = translator.complete(system_prompt, original_title).strip()
        failed_reasons = _looks_like_failed_component_translation_v3(
            translated,
            original_text=original_title,
            component_name="标题",
        )
        if not failed_reasons:
            print("[翻译][标题] LLM 翻译成功。")
            return translated, None
        err = "；".join(failed_reasons)
        print(f"[翻译][标题] LLM 翻译未通过校验：{err}")
        attempt_errors.append(f"llm={err}")
    except Exception as e:
        err = repr(e)
        print(f"[翻译][标题] LLM 翻译失败：{err}")
        attempt_errors.append(f"llm={err}")

    try:
        translated = translator.translate_text_to_zh_via_google(original_title).strip()
        failed_reasons = _looks_like_failed_component_translation_v3(
            translated,
            original_text=original_title,
            component_name="标题",
        )
        if not failed_reasons:
            print("[翻译][标题] Google 风格回退翻译成功。")
            return translated, "标题已使用 Google 风格回退翻译"
        err = "；".join(failed_reasons)
        print(f"[翻译][标题] Google 风格回退未通过校验：{err}")
        attempt_errors.append(f"google={err}")
    except Exception as e:
        err = repr(e)
        print(f"[翻译][标题] Google 风格回退失败：{err}")
        attempt_errors.append(f"google={err}")

    return original_title, "标题翻译失败，已回退原文：" + " | ".join(attempt_errors)


def _translate_content_component_v3(
    translator: OpenAICompatTranslator | None,
    first_post_text: str,
) -> tuple[str, str | None]:
    original_content = _prepare_post_content_for_translation_v3(first_post_text)
    if not original_content:
        return "未抓取到可用正文内容。", "正文为空"
    if translator is None:
        return original_content, "未配置大模型，正文直接使用原文"

    system_prompt = (
        "Translate the forum post content into natural Simplified Chinese for internal monitoring. "
        "Preserve proper nouns, organization names, product names, malware family names, actor names, URLs, IDs, and technical abbreviations. "
        "Keep paragraph structure. Output only the translated content without commentary."
    )
    reduced_content = _reduce_post_content_for_retry_v3(first_post_text)
    attempt_plan = [
        ("llm_full", original_content),
        ("llm_first_last", reduced_content),
    ]
    attempt_errors: list[str] = []
    last_source_text = original_content

    for strategy_name, source_text in attempt_plan:
        last_source_text = source_text
        try:
            translated = translator.complete(system_prompt, source_text).strip()
            failed_reasons = _looks_like_failed_component_translation_v3(
                translated,
                original_text=source_text,
                component_name="正文",
            )
            if not failed_reasons:
                print(f"[翻译][正文] {strategy_name} 成功。")
                return translated, (None if strategy_name == "llm_full" else "正文已使用缩减内容重试翻译")
            err = "；".join(failed_reasons)
            print(f"[翻译][正文] {strategy_name} 未通过校验：{err}")
            attempt_errors.append(f"{strategy_name}={err}")
        except Exception as e:
            err = repr(e)
            print(f"[翻译][正文] {strategy_name} 失败：{err}")
            attempt_errors.append(f"{strategy_name}={err}")

    try:
        translated = translator.translate_text_to_zh_via_google(last_source_text).strip()
        failed_reasons = _looks_like_failed_component_translation_v3(
            translated,
            original_text=last_source_text,
            component_name="正文",
        )
        if not failed_reasons:
            print("[翻译][正文] Google 风格回退翻译成功。")
            return translated, "正文已使用 Google 风格回退翻译"
        err = "；".join(failed_reasons)
        print(f"[翻译][正文] Google 风格回退未通过校验：{err}")
        attempt_errors.append(f"google={err}")
    except Exception as e:
        err = repr(e)
        print(f"[翻译][正文] Google 风格回退失败：{err}")
        attempt_errors.append(f"google={err}")

    return original_content, "正文翻译失败，已回退原文：" + " | ".join(attempt_errors)


def _build_overview_values_local_v3(
    *,
    post_url: str,
    created_at: str,
    title: str,
    first_post_text: str,
    chongqing_related: bool,
) -> dict[str, str]:
    regions = "、".join(_extract_region_labels(title, first_post_text))
    target_scope = _infer_target_scope(title, first_post_text)
    source_value = f"[原帖链接]({post_url})"
    if created_at:
        source_value = f"{source_value}，时间：{created_at}"
    return {
        "区域": regions,
        "关注对象": target_scope,
        "风险概述": "疑似信息外泄或数据风险",
        "信息来源": source_value,
        "重点提示": ("该条内容命中了重庆相关关键词，请优先关注。" if chongqing_related else "该条内容与中国相关，请持续关注。"),
    }


def _translate_overview_component_v3(
    translator: OpenAICompatTranslator | None,
    *,
    post_url: str,
    created_at: str,
    title: str,
    first_post_text: str,
    chongqing_related: bool,
) -> tuple[dict[str, str], str | None]:
    local_values = _build_overview_values_local_v3(
        post_url=post_url,
        created_at=created_at,
        title=title,
        first_post_text=first_post_text,
        chongqing_related=chongqing_related,
    )
    if translator is None:
        return local_values, "未配置大模型，监控概览使用本地模板"

    prompt = (
        "Using the provided safe monitoring fields, return concise Simplified Chinese values for exactly five fields. "
        "Preserve proper nouns and geographic names accurately. Output exactly these five lines and nothing else:\n"
        "区域: ...\n关注对象: ...\n风险概述: ...\n信息来源: ...\n重点提示: ..."
    )
    user_input = "\n".join([
        f"Region: {local_values['区域']}",
        f"Target type: {local_values['关注对象']}",
        f"Risk summary: {local_values['风险概述']}",
        f"Source: {post_url}",
        f"Publish time: {created_at or 'unknown'}",
        f"Priority hint: {local_values['重点提示']}",
    ])
    try:
        translated = translator.complete(prompt, user_input).strip()
        parsed: dict[str, str] = {}
        for line in translated.replace("\r", "").splitlines():
            line = line.strip()
            if not line:
                continue
            normalized = line.replace("：", ":", 1)
            if ":" not in normalized:
                continue
            key, value = normalized.split(":", 1)
            parsed[key.strip()] = value.strip()
        required_keys = ("区域", "关注对象", "风险概述", "信息来源", "重点提示")
        if all(parsed.get(key) for key in required_keys) and not _looks_like_translation_refusal(translated):
            print("[翻译][概览] LLM 填充成功。")
            return {key: parsed[key] for key in required_keys}, None
        print("[翻译][概览] LLM 填充未通过校验，回退本地模板。")
        return local_values, "监控概览翻译失败，已回退本地模板"
    except Exception as e:
        print(f"[翻译][概览] LLM 填充失败：{repr(e)}")
        return local_values, f"监控概览翻译失败，已回退本地模板：{repr(e)}"


def _build_unified_dingtalk_message_v3(
    *,
    translated_post_title: str,
    translated_post_content: str,
    overview_values: dict[str, str],
) -> str:
    parts = [
        "## 帖子标题",
        translated_post_title.strip() or "未获取到标题翻译。",
        "",
        "## 帖子内容",
        translated_post_content.strip() or "未获取到正文翻译。",
        "",
        "## 监控概览",
        f"- 区域: {overview_values.get('区域', '')}",
        f"- 关注对象: {overview_values.get('关注对象', '')}",
        f"- 风险概述: {overview_values.get('风险概述', '')}",
        f"- 信息来源: {overview_values.get('信息来源', '')}",
        f"- 重点提示: {overview_values.get('重点提示', '')}",
    ]
    return "\n".join(parts).strip()


def _translate_dingtalk_message_v3(
    translator: OpenAICompatTranslator | None,
    *,
    notification_title: str,
    post_url: str,
    title: str,
    created_at: str,
    author_name: str,
    first_post_text: str,
    chongqing_related: bool,
) -> tuple[str, str, str | None]:
    translated_title, title_note = _translate_title_component_v3(translator, title)
    translated_content, content_note = _translate_content_component_v3(translator, first_post_text)
    overview_values, overview_note = _translate_overview_component_v3(
        translator,
        post_url=post_url,
        created_at=created_at,
        title=title,
        first_post_text=first_post_text,
        chongqing_related=chongqing_related,
    )

    send_text = _build_unified_dingtalk_message_v3(
        translated_post_title=translated_title,
        translated_post_content=translated_content,
        overview_values=overview_values,
    )

    notes = [note for note in (title_note, content_note, overview_note) if note]
    if notes:
        send_text += "\n\n> 说明: " + "；".join(notes)
        return notification_title[:128], send_text, "；".join(notes)
    return notification_title[:128], send_text, None


def _deliver_to_feishu(conn, settings: Settings) -> None:
    if not settings.feishu_enabled:
        return

    cfg = FeishuConfig(
        app_id=settings.feishu_app_id,
        app_secret=settings.feishu_app_secret,
        chat_id=settings.feishu_chat_id,
    )
    client = FeishuClient(cfg)

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
                    content.append([{"tag": "a", "text": u.strip()[:120], "href": u.strip()}])

        if first_post_text:
            excerpt = first_post_text.strip()
            if len(excerpt) > 1200:
                excerpt = excerpt[:1200] + "..."
            content.append([{"tag": "text", "text": f"\n首楼内容摘要:\n{excerpt}"}])

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
            mark_delivered(
                conn,
                post_url=post_url,
                provider="feishu",
                delivered_at=datetime.utcnow().isoformat(),
                message_id=message_id,
            )
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
        pass

    print(
        f"[feishu] summary limit={max_success} attempted={attempted} delivered={delivered} failed={failed} remaining={remaining}"
    )


def _deliver_to_dingtalk(conn, settings: Settings) -> None:
    if not settings.dingtalk_enabled:
        return

    cfg = DingTalkConfig(webhook=settings.dingtalk_webhook, secret=(settings.dingtalk_secret or None))
    client = DingTalkClient(cfg)
    translator = None
    if settings.llm_api_key:
        translator = OpenAICompatTranslator(
            OpenAICompatConfig(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                proxy_server=(settings.llm_proxy_server if settings.llm_use_proxy else ""),
            )
        )

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
    filtered = 0
    fetch_limit = max_success * 20
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

        if not _is_china_related(post_url, title, first_post_text):
            mark_delivered(
                conn,
                post_url=post_url,
                provider="dingtalk",
                delivered_at=datetime.utcnow().isoformat(),
                message_id="filtered_non_china",
            )
            filtered += 1
            print(f"[dingtalk] filtered_non_china: {post_url}")
            continue

        chongqing_related = _is_chongqing_related(post_url, title, first_post_text)
        notification_title = _build_notification_title(
            title=title,
            first_post_text=first_post_text,
            chongqing_related=chongqing_related,
        )
        send_title, send_text, translate_error = _translate_dingtalk_message_v3(
            translator,
            notification_title=notification_title,
            post_url=post_url,
            title=title,
            created_at=created_at,
            author_name=author_name,
            first_post_text=first_post_text,
            chongqing_related=chongqing_related,
        )
        if translate_error:
            print(f"[dingtalk] translate_failed fallback_original: {post_url} -> {translate_error}")

        try:
            client.send_markdown(title=send_title, text=send_text)
            mark_delivered(
                conn,
                post_url=post_url,
                provider="dingtalk",
                delivered_at=datetime.utcnow().isoformat(),
                message_id=None,
            )
            delivered += 1
            print(f"[dingtalk] delivered: {post_url}")
        except Exception as e:
            failed += 1
            print(f"[dingtalk] failed: {post_url} -> {repr(e)}")
            continue

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
        pass

    print(
        f"[dingtalk] summary limit={max_success} attempted={attempted} delivered={delivered} failed={failed} filtered={filtered} remaining={remaining}"
    )


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
        print("[1/4] 开始登录论坛...")
        login(page, settings.base_url, settings.username, settings.password)
        save_storage_state(page, storage_state_path)
        print("[1/4] 登录成功，已保存登录态。")

        if settings.forum_urls:
            print(f"[2/4] 开始发现帖子，共 {len(settings.forum_urls)} 个版块...")
            if settings.latest_page_only:
                print(f"[2/4] 已开启仅抓最新页：每个版块只访问最新 1 页，排序参数={settings.forum_sort_query!r}")
            discovered = 0
            inserted = 0
            sample_urls: list[str] = []
            discover_workers = min(max(1, int(settings.scrape_workers)), len(settings.forum_urls))
            print(f"[2/4] 发现阶段并发数={discover_workers}")
            cursor_map = {forum_url: get_cursor(conn, f"forum:{forum_url}") for forum_url in settings.forum_urls}
            with ThreadPoolExecutor(max_workers=discover_workers) as executor:
                future_map = {
                    executor.submit(_discover_forum_worker, settings, storage_state_path, forum_url, cursor_map.get(forum_url)): forum_url
                    for forum_url in settings.forum_urls
                }
                for future in as_completed(future_map):
                    forum_url = future_map[future]
                    outcome = future.result()
                    if outcome.error is not None:
                        print(f"[2/4] 发现失败：forum={forum_url}，错误={outcome.error}")
                        continue
                    max_started_at: str | None = None
                    for th in outcome.threads:
                        if len(sample_urls) < 5:
                            sample_urls.append(th.url)
                        if upsert_discovered(conn, th.url, th.discovered_at, th.started_at):
                            inserted += 1
                        discovered += 1
                        if th.started_at and ((max_started_at is None) or (th.started_at > max_started_at)):
                            max_started_at = th.started_at
                    if max_started_at:
                        set_cursor(conn, f"forum:{forum_url}", max_started_at)
            print("[2/4] 帖子发现完成。")
            print(f"[2/4] 发现帖子数（去重前）: {discovered}")
            print(f"[2/4] 写入 SQLite 新线程数: {inserted}")
            if sample_urls:
                print("[2/4] 样例帖子链接：")
                for u in sample_urls:
                    print(f"  - {u}")

        print("[3/4] 开始抓取待处理帖子内容...")
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

                try:
                    created_iso = get_thread_created_at(conn, url)
                except Exception:
                    created_iso = None

                if created_iso and not (settings.full_site_mode or settings.latest_page_only):
                    try:
                        dt0 = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
                        if dt0 < cutoff_dt:
                            mark_failed(conn, url, "older_than_cutoff")
                            print(f"[跳过] {url} -> 超过时间窗口，created_at={created_iso}")
                            processed += 1
                            failed_threads += 1
                            charged += 1
                            continue
                    except Exception:
                        pass

                mark_processing(conn, url)
                base_may_reply = not has_reply(conn, url)
                allow_reply = False
                if settings.full_site_mode:
                    allow_reply = False
                elif settings.latest_page_only:
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
                        may_reply=(False if settings.full_site_mode else allow_reply),
                        created_at_cutoff_iso=(None if (settings.full_site_mode or settings.latest_page_only) else cutoff_dt.isoformat()),
                    )
                )
            except Exception as e:
                mark_failed(conn, url, repr(e))
                print(f"[失败] 预处理帖子失败：{url} -> {repr(e)}")
                processed += 1
                failed_threads += 1
                charged += 1
            if settings.max_threads_per_day > 0 and len(pending_tasks) >= settings.max_threads_per_day:
                break

        if pending_tasks:
            worker_count = min(max(1, int(settings.scrape_workers)), len(pending_tasks))
            print(f"[3/4] 待抓取数量={len(pending_tasks)}，抓取并发数={worker_count}")
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {executor.submit(_scrape_thread_worker, settings, storage_state_path, task): task.url for task in pending_tasks}
                for future in as_completed(future_map):
                    url = future_map[future]
                    try:
                        outcome = future.result()
                    except Exception as e:
                        mark_failed(conn, url, repr(e))
                        print(f"[失败] 抓取任务执行异常：{url} -> {repr(e)}")
                        processed += 1
                        failed_threads += 1
                        charged += 1
                        continue

                    if outcome.browser_check:
                        mark_failed(conn, url, "browser_check")
                        print(f"[跳过] {url} -> 命中浏览器验证页")
                        processed += 1
                        failed_threads += 1
                        skipped_browser_check += 1
                        if skipped_browser_check >= max_browser_check_skips:
                            print(f"[跳过] 已达到浏览器验证页跳过上限：{skipped_browser_check}")
                            break
                        continue

                    if outcome.error is not None or outcome.result is None:
                        err = outcome.error or "unknown_worker_error"
                        mark_failed(conn, url, err)
                        print(f"[失败] 抓取帖子失败：{url} -> {err}")
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
                    print(f"[帖子信息] created_at={created_at or '(缺失)'} url={url}")

                    if not (settings.full_site_mode or settings.latest_page_only):
                        parsed_dt = _parse_localized_dt(created_at)
                        if parsed_dt is None:
                            mark_failed(conn, url, "created_at_unparseable")
                            print(f"[失败] {url} -> 发帖时间无法解析")
                            processed += 1
                            failed_threads += 1
                            charged += 1
                            continue
                        if parsed_dt < cutoff_dt:
                            mark_failed(conn, url, "older_than_cutoff")
                            print(f"[跳过] {url} -> 超过时间窗口，created_at={created_at}")
                            processed += 1
                            failed_threads += 1
                            charged += 1
                            continue

                    if result.did_reply:
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
                    print(f"[成功] {url} -> 已写入帖子内容（下载链接 {len(download_urls)} 条）")
                    ok_threads += 1
                    total_download_urls += len(download_urls)
                    inserted_download_rows += 1
                    processed += 1
                    charged += 1
                    if settings.max_threads_per_day > 0 and charged >= settings.max_threads_per_day:
                        break

        if settings.dingtalk_enabled:
            print("[4/4] 开始推送钉钉消息...")
            try:
                _deliver_to_dingtalk(conn, settings)
            except Exception as e:
                print(f"[钉钉] 推送阶段失败 -> {repr(e)}")
        elif settings.feishu_enabled:
            print("[4/4] 开始推送飞书消息...")
            try:
                _deliver_to_feishu(conn, settings)
            except Exception as e:
                print(f"[飞书] 推送阶段失败 -> {repr(e)}")

        print("[4/4] 本轮执行完成。")

        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE status IN ('new','failed') AND extracted_at IS NULL"
            ).fetchone()[0]
            threads_total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            posts_total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            print(
                "[运行统计] processed=%d charged=%d skipped_browser_check=%d ok=%d failed=%d download_urls=%d downloads_new=%d"
                % (processed, charged, skipped_browser_check, ok_threads, failed_threads, total_download_urls, inserted_download_rows)
            )
            print("[数据库] threads_total=%d posts_total=%d pending=%d" % (threads_total, posts_total, pending))
        except Exception:
            pass

        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM threads GROUP BY status")
            stats = {row[0]: int(row[1]) for row in cur.fetchall()}
            print(f"[状态统计] {stats}")
        except Exception:
            pass

        try:
            pruned = prune_threads(conn, retention_days=30)
            if pruned:
                print(f"[数据库] 已清理过期线程数={pruned}")
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
