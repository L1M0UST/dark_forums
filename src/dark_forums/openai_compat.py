from __future__ import annotations

from dataclasses import dataclass
import json
import re

import requests
from requests.exceptions import SSLError


@dataclass(frozen=True)
class OpenAICompatConfig:
    base_url: str
    api_key: str
    model: str
    proxy_server: str = ""


class OpenAICompatTranslator:
    def __init__(self, cfg: OpenAICompatConfig) -> None:
        self._cfg = cfg

    @property
    def model_name(self) -> str:
        return self._cfg.model

    @staticmethod
    def _strip_think_blocks(text: str) -> str:
        cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.IGNORECASE | re.DOTALL)
        return cleaned.strip()

    def _post_chat(self, system_prompt: str, user_prompt: str) -> str:
        url = f"{self._cfg.base_url}/chat/completions"
        payload = json.dumps(
            {
                "model": self._cfg.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            },
            ensure_ascii=False,
        )
        proxies = None
        if self._cfg.proxy_server:
            proxies = {
                "http": self._cfg.proxy_server,
                "https": self._cfg.proxy_server,
            }

        request_kwargs = {
            "headers": {
                "Authorization": f"Bearer {self._cfg.api_key}",
                "Content-Type": "application/json",
            },
            "data": payload,
            "timeout": 60,
        }

        attempts: list[dict] = []
        if proxies is not None:
            attempts.append({"proxies": proxies})
            attempts.append({"proxies": proxies, "verify": False})
        attempts.append({})
        attempts.append({"verify": False})

        last_exc: Exception | None = None
        resp = None
        for idx, extra in enumerate(attempts, start=1):
            try:
                if extra.get("proxies"):
                    print(f"[翻译] 正在请求模型，第 {idx}/{len(attempts)} 次，使用代理：{self._cfg.proxy_server}")
                else:
                    print(f"[翻译] 正在请求模型，第 {idx}/{len(attempts)} 次，直连访问。")
                resp = requests.post(url, **request_kwargs, **extra)
                break
            except SSLError as exc:
                last_exc = exc
                print(f"[翻译] 模型请求 SSL 失败，第 {idx}/{len(attempts)} 次：{repr(exc)}")
            except Exception as exc:
                last_exc = exc
                print(f"[翻译] 模型请求失败，第 {idx}/{len(attempts)} 次：{repr(exc)}")
        if resp is None:
            raise RuntimeError(f"模型请求失败，可能是网络不通或代理异常：{repr(last_exc)}")

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(f"模型请求失败，HTTP {resp.status_code}，返回内容：{body}")

        data = resp.json()
        if bool(data.get("input_sensitive")) or bool(data.get("output_sensitive")):
            raise RuntimeError(f"模型触发风控拦截：{data}")
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"模型返回格式异常：{data}") from exc
        if not isinstance(content, str):
            raise RuntimeError(f"模型返回内容格式异常：{data}")
        content = self._strip_think_blocks(content)
        if not content.strip():
            raise RuntimeError(f"模型返回内容为空：{data}")
        return content.strip()

    def translate_title_to_zh(self, title: str) -> str:
        return self._post_chat(
            "Translate the input title into concise Simplified Chinese. Keep technical terms, organization names, counts, IDs, and URLs accurate. Output only the translated title. Do not explain. Do not add notes. Do not add any preamble or analysis.",
            title,
        )

    def translate_markdown_to_zh(self, markdown_text: str) -> str:
        return self._post_chat(
            "Translate the input markdown into clear, easy-to-understand Simplified Chinese. Preserve markdown structure, raw URLs, links, list markers, and blockquotes. Translate natural language only and output only the translated markdown. Do not explain. Do not add notes. Do not add any preamble, analysis, or commentary.",
            markdown_text,
        )
