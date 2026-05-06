from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    chat_id: str


class FeishuClient:
    def __init__(self, cfg: FeishuConfig) -> None:
        self._cfg = cfg
        self._token: str | None = None
        self._token_expire_at: datetime | None = None

    def _get_tenant_access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and self._token_expire_at and now < self._token_expire_at:
            return self._token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(
            url,
            json={"app_id": self._cfg.app_id, "app_secret": self._cfg.app_secret},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", -1)) != 0:
            raise RuntimeError(f"feishu_auth_failed: {data}")

        token = str(data["tenant_access_token"])
        expire = int(data.get("expire", 3600))
        self._token = token
        self._token_expire_at = now + timedelta(seconds=max(60, expire - 60))
        return token

    def _headers(self) -> dict[str, str]:
        token = self._get_tenant_access_token()
        return {"Authorization": f"Bearer {token}"}

    def upload_image(self, image_path: Path) -> str:
        url = "https://open.feishu.cn/open-apis/im/v1/images"
        with image_path.open("rb") as fp:
            files = {
                "image": (image_path.name, fp, "image/png"),
            }
            resp = requests.post(
                url,
                headers=self._headers(),
                data={"image_type": "message"},
                files=files,
                timeout=60,
            )
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", -1)) != 0:
            raise RuntimeError(f"feishu_upload_failed: {data}")
        return str(data["data"]["image_key"])

    def send_post_message(self, chat_id: str, title: str, content: list[list[dict[str, Any]]]) -> str:
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "post",
            "content": json.dumps(
                {
                    "zh_cn": {
                        "title": title,
                        "content": content,
                    }
                },
                ensure_ascii=False,
            ),
        }
        resp = requests.post(url, headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if int(data.get("code", -1)) != 0:
            raise RuntimeError(f"feishu_send_failed: {data}")
        return str(data["data"]["message_id"])


def load_feishu_config_from_env() -> FeishuConfig | None:
    enabled = os.getenv("FEISHU_ENABLED", "0").strip() not in {"0", "false", "False", ""}
    if not enabled:
        return None

    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    chat_id = os.getenv("FEISHU_CHAT_ID", "").strip()

    if not (app_id and app_secret and chat_id):
        raise ValueError("Feishu enabled but FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_CHAT_ID not set")

    return FeishuConfig(app_id=app_id, app_secret=app_secret, chat_id=chat_id)
