from __future__ import annotations

from dataclasses import dataclass
import json

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
        for extra in attempts:
            try:
                resp = requests.post(url, **request_kwargs, **extra)
                break
            except SSLError as exc:
                last_exc = exc
            except Exception as exc:
                last_exc = exc
        if resp is None:
            raise RuntimeError(f"openai_compat_request_failed: {repr(last_exc)}")

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise RuntimeError(f"openai_compat_http_{resp.status_code}: {body}")

        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"openai_compat_bad_response: {data}") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"openai_compat_empty_response: {data}")
        return content.strip()

    def translate_title_to_zh(self, title: str) -> str:
        return self._post_chat(
            "Translate the input title into concise Simplified Chinese. Keep technical terms, organization names, counts, IDs, and URLs accurate. Output only the translated title.",
            title,
        )

    def translate_markdown_to_zh(self, markdown_text: str) -> str:
        return self._post_chat(
            "Translate the input markdown into clear, easy-to-understand Simplified Chinese. Preserve markdown structure, raw URLs, links, list markers, and blockquotes. Translate natural language only and output only the translated markdown.",
            markdown_text,
        )
