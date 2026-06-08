from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from .config import Settings, load_settings
from .dingtalk import DingTalkClient, DingTalkConfig
from .openai_compat import OpenAICompatConfig, OpenAICompatTranslator
from .runner import _build_dingtalk_raw_markdown, _build_translation_input, _translate_for_dingtalk


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
    print("[测试] 步骤 1/2：开始测试标题翻译与正文翻译。")
    send_title, send_text, translate_error = _translate_for_dingtalk(
        translator,
        title,
        model_input_text,
        raw_text,
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
