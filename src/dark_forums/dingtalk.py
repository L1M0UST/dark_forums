from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

import requests
from requests import RequestException


@dataclass
class DingTalkConfig:
    webhook: str
    secret: str | None = None


class DingTalkClient:
    def __init__(self, cfg: DingTalkConfig) -> None:
        self._cfg = cfg

    def _signed_webhook(self) -> str:
        webhook = self._cfg.webhook
        secret = (self._cfg.secret or "").strip()
        if not secret:
            return webhook
        ts = str(int(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}".encode("utf-8")
        h = hmac.new(secret.encode("utf-8"), string_to_sign, digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(h).decode("utf-8")
        # append to existing query
        parsed = urlparse(webhook)
        q = parse_qs(parsed.query, keep_blank_values=True)
        q["timestamp"] = [ts]
        q["sign"] = [sign]
        new_query = urlencode([(k, v[0]) for k, v in q.items()])
        return urlunparse(parsed._replace(query=new_query))

    def send_markdown(self, title: str, text: str) -> dict[str, Any]:
        url = self._signed_webhook()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }
        try:
            resp = requests.post(url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except RequestException as e:
            raise RuntimeError(f"钉钉 markdown 推送失败，可能是网络不通：{repr(e)}") from e
        except ValueError as e:
            raise RuntimeError(f"钉钉 markdown 返回解析失败：{repr(e)}") from e
        if int(data.get("errcode", 0)) != 0:
            raise RuntimeError(f"钉钉 markdown 推送失败，接口返回异常：{data}")
        return data

    def send_image(self, image_base64: str, md5_hex: str) -> dict[str, Any]:
        url = self._signed_webhook()
        payload = {
            "msgtype": "image",
            "image": {
                "base64": image_base64,
                "md5": md5_hex,
            },
        }
        try:
            resp = requests.post(url, headers={"Content-Type": "application/json"}, data=json.dumps(payload), timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except RequestException as e:
            raise RuntimeError(f"钉钉图片推送失败，可能是网络不通：{repr(e)}") from e
        except ValueError as e:
            raise RuntimeError(f"钉钉图片返回解析失败：{repr(e)}") from e
        if int(data.get("errcode", 0)) != 0:
            raise RuntimeError(f"钉钉图片推送失败，接口返回异常：{data}")
        return data


def load_dingtalk_config_from_env() -> DingTalkConfig | None:
    enabled = os.getenv("DINGTALK_ENABLED", "0").strip() not in {"0", "false", "False", ""}
    if not enabled:
        return None

    webhook = os.getenv("DINGTALK_WEBHOOK", "").strip()
    secret = os.getenv("DINGTALK_SECRET", "").strip() or None
    if not webhook:
        raise ValueError("DingTalk enabled but DINGTALK_WEBHOOK not set")
    return DingTalkConfig(webhook=webhook, secret=secret)
