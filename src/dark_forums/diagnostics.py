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


def _build_notification_title(*, title: str, first_post_text: str, chongqing_related: bool) -> str:
    prefix = "重庆相关风险摘要" if chongqing_related else "中国相关风险摘要"
    return prefix[:128]


def _looks_like_translation_refusal(text: str) -> bool:
    haystack = (text or "").strip().lower()
    if not haystack:
        return True
    return any(pattern in haystack for pattern in _TRANSLATION_REFUSAL_PATTERNS)


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
    parts: list[str] = [f"[{title}]({post_url})"]
    if chongqing_related:
        parts.append("## 重庆相关重点提示\n该条内容命中了重庆相关关键词，请优先关注。")

    meta_line = []
    if author_name:
        meta_line.append(f"作者: {author_name}")
    if created_at:
        meta_line.append(f"时间: {created_at}")
    if meta_line:
        parts.append("\n" + "    ".join(meta_line))

    summary = _build_local_safe_summary(title, first_post_text, max_chars=220)
    if summary:
        parts.append("\n> 合规摘要:\n> " + summary.replace("\n", "\n> "))

    if download_urls:
        parts.append("\n> 说明: 具体下载链接与敏感细节已省略，请进入原帖人工核验。")

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
    parts: list[str] = []
    parts.append(f"标题: {title}")
    parts.append(f"帖子链接: {post_url}")
    if chongqing_related:
        parts.append("提示: 该内容命中重庆相关关键词，需要重点提示。")
    if author_name:
        parts.append(f"作者: {author_name}")
    if created_at:
        parts.append(f"时间: {created_at}")
    parts.append("要求: 输出合规、简洁的中文风险摘要；不要输出下载链接、凭证、口令、Cookie、Token、样本、利用步骤或其他敏感细节。")
    parts.append("首楼合规摘要:")
    parts.append(_build_local_safe_summary(title, first_post_text, max_chars=260))
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
    send_title, send_text, translate_error = _translate_for_test(
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
