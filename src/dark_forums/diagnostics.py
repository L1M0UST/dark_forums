from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from .config import Settings, load_settings
from .dingtalk import DingTalkClient, DingTalkConfig
from .openai_compat import OpenAICompatConfig, OpenAICompatTranslator


class _LineTee:
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
            rendered = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {line}\n"
            for st in self._streams:
                self._safe_write_stream(st, rendered)
        return len(s)

    def flush(self) -> None:
        if self._buffer:
            rendered = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {self._buffer}"
            for st in self._streams:
                self._safe_write_stream(st, rendered)
            self._buffer = ""
        for st in self._streams:
            st.flush()


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


def _translate_for_test(
    translator: OpenAICompatTranslator,
    notification_title: str,
    model_input_text: str,
    raw_fallback_text: str,
    *,
    chongqing_related: bool,
) -> tuple[str, str, str | None]:
    try:
        translated_text = translator.translate_markdown_to_zh(model_input_text).strip()
        if _looks_like_translation_refusal(translated_text):
            raise RuntimeError(f"正文翻译疑似拒答：{translated_text}")
        send_title = notification_title[:128]
        send_text = _clean_translated_notification_text(
            translated_text,
            original_title=notification_title,
            translated_title=send_title,
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


def _translate_for_test_v2(
    translator: OpenAICompatTranslator,
    notification_title: str,
    model_input_text: str,
    raw_fallback_text: str,
    *,
    chongqing_related: bool,
) -> tuple[str, str, str | None]:
    send_title = notification_title[:128]
    attempt_errors: list[str] = []
    attempts = _build_translation_attempts(model_input_text, chongqing_related=chongqing_related)
    for idx, (strategy_name, system_prompt, attempt_input) in enumerate(attempts, start=1):
        print(f"[测试][翻译] 开始策略 {idx}/{len(attempts)}：{strategy_name}，输入长度={len(attempt_input)}")
        try:
            translated_text = translator.complete(system_prompt, attempt_input).strip()
            send_text = _clean_translated_notification_text(
                translated_text,
                original_title=notification_title,
                translated_title=send_title,
                chongqing_related=chongqing_related,
            )
            failed_reasons = _evaluate_translation_result(translated_text, send_text)
            if failed_reasons:
                err = f"策略 {strategy_name} 未通过校验：{'；'.join(failed_reasons)}"
                print(f"[测试][翻译] {err}")
                attempt_errors.append(err)
                continue
            print(f"[测试][翻译] 策略 {strategy_name} 成功，已通过翻译校验。")
            return send_title, send_text, None
        except Exception as e:
            err = f"策略 {strategy_name} 请求失败：{repr(e)}"
            print(f"[测试][翻译] {err}")
            attempt_errors.append(err)

    err = " | ".join(attempt_errors) if attempt_errors else "模型未返回可用翻译结果"
    fallback_text = raw_fallback_text + "\n\n> 说明: 模型翻译未通过成功校验，已自动回退为本地生成的中文监控概览。"
    return send_title, fallback_text, err


def _build_test_payload() -> tuple[str, str, str, str, list[str], bool]:
    title = "重庆某企业数据泄露翻译与钉钉推送测试"
    post_url = "https://example.com/dark-forums-test"
    created_at = datetime.now().strftime("%d-%m-%y, %I:%M %p")
    author_name = "dark_forums_test_bot"
    first_post_text = (
        "This is a translation and DingTalk delivery test. "
        "The content mentions Chongqing, China, leaked database, source code, and credential risk. "
        "Please translate this text into clear Simplified Chinese for notification delivery."
    )
    download_urls = ["https://example.com/download/test-file"]
    chongqing_related = True
    return title, post_url, created_at, author_name, first_post_text, download_urls, chongqing_related


def run_test_notify(project_root: Path) -> int:
    settings = load_settings(project_root)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / f"test_notify_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    with log_path.open("a", encoding="utf-8") as log_fp:
        sys.stdout = _LineTee(orig_stdout, log_fp)
        sys.stderr = _LineTee(orig_stderr, log_fp)
        try:
            print(f"[测试] 开始执行翻译与钉钉联调测试，日志文件：{log_path}")
            _validate_test_settings(settings)
            _run_test_notify_inner(settings)
            print("[测试] 翻译与钉钉联调测试完成。")
            return 0
        except Exception as e:
            print(f"[测试] 联调测试失败：{repr(e)}")
            return 1
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr


def _validate_test_settings(settings: Settings) -> None:
    if not settings.dingtalk_enabled:
        raise RuntimeError("DINGTALK_ENABLED 未开启，无法测试钉钉发送。")
    if not settings.dingtalk_webhook.strip():
        raise RuntimeError("DINGTALK_WEBHOOK 为空，无法测试钉钉发送。")
    if not settings.llm_base_url.strip():
        raise RuntimeError("OPENAI_COMPAT_BASE_URL 为空，无法测试翻译模型。")
    if not settings.llm_api_key.strip():
        raise RuntimeError("OPENAI_COMPAT_API_KEY 为空，无法测试翻译模型。")
    if not settings.llm_model.strip():
        raise RuntimeError("OPENAI_COMPAT_MODEL 为空，无法测试翻译模型。")


def _run_test_notify_inner(settings: Settings) -> None:
    translator = OpenAICompatTranslator(
        OpenAICompatConfig(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            proxy_server=(settings.llm_proxy_server if settings.llm_use_proxy else ""),
        )
    )
    client = DingTalkClient(
        DingTalkConfig(
            webhook=settings.dingtalk_webhook,
            secret=(settings.dingtalk_secret or None),
        )
    )

    (
        title,
        post_url,
        created_at,
        author_name,
        first_post_text,
        download_urls,
        chongqing_related,
    ) = _build_test_payload()

    raw_text = _build_dingtalk_raw_markdown(
        post_url=post_url,
        title=title,
        created_at=created_at,
        author_name=author_name,
        first_post_text=first_post_text,
        download_urls=download_urls,
        chongqing_related=chongqing_related,
    )
    notification_title = _build_notification_title(
        title=title,
        first_post_text=first_post_text,
        chongqing_related=chongqing_related,
    )
    model_input_text = _build_translation_input(
        post_url=post_url,
        title=title,
        created_at=created_at,
        author_name=author_name,
        first_post_text=first_post_text,
        download_urls=download_urls,
        chongqing_related=chongqing_related,
    )

    print(
        f"[测试] 翻译模型配置：base_url={settings.llm_base_url} model={settings.llm_model} "
        f"use_proxy={settings.llm_use_proxy} proxy={settings.llm_proxy_server or '(empty)'}"
    )
    print(f"[测试] 步骤 1/2：开始测试合规摘要翻译，通知标题将使用本地安全标题：{notification_title}")
    send_title, send_text, translate_error = _translate_for_test_v2(
        translator,
        notification_title,
        model_input_text,
        raw_text,
        chongqing_related=chongqing_related,
    )
    if translate_error:
        print(f"[测试] 翻译失败，已自动回退原文发送：{translate_error}")
    else:
        print(f"[测试] 翻译成功，标题={send_title}")
    print(f"[测试] 发送正文预览（前 500 字）：{send_text[:500]}")

    print("[测试] 步骤 2/2：开始测试钉钉 markdown 发送。")
    resp = client.send_markdown(title=send_title, text=send_text)
    print(f"[测试] 钉钉发送成功，响应={resp}")
    if translate_error:
        raise RuntimeError(f"翻译测试失败，但钉钉回退发送成功：{translate_error}")
