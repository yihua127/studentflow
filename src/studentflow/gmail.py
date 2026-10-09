"""Read the signed-in user's Gmail inbox through desktop OAuth, without mail writes.

Credentials and tokens stay in caller-selected local files. No model is called here.
The Google libraries are imported only when the user connects an account.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
MAX_EMAIL_BYTES = 20 * 1024 * 1024
MAX_RAW_CHARS = 4 * ((MAX_EMAIL_BYTES + 2) // 3)
MAX_JSON_BYTES = MAX_RAW_CHARS + 64 * 1024
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class GmailError(Exception):
    """A user-facing error that never contains token values or server responses."""


def _load_json(path: Path, description: str) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("oversized configuration")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise TypeError("configuration must be an object")
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        raise GmailError(f"无法读取{description}，请检查本机 JSON 文件是否正确。") from None


def _client_config(path: Path) -> dict:
    if not path.is_file():
        raise GmailError("还没有 Gmail 客户端配置，请先下载桌面应用的 OAuth JSON 文件。")
    config = _load_json(path, "Gmail 客户端配置")
    client = config.get("installed")
    if not isinstance(client, dict):
        raise GmailError("请使用 Google OAuth 的“桌面应用”配置，不是网页应用或服务账号。")
    if client.get("auth_uri") not in AUTH_URIS or client.get("token_uri") != TOKEN_URI:
        raise GmailError("Gmail 客户端配置的授权地址不正确，请重新从 Google 下载配置。")
    if not all(isinstance(client.get(key), str) and client[key] for key in (
        "client_id", "client_secret",
    )):
        raise GmailError("Gmail 客户端配置不完整，请重新下载。")
    # Pass only validated endpoint and client fields to the OAuth library.
    return {"installed": {key: client[key] for key in (
        "client_id", "client_secret", "auth_uri", "token_uri",
    )}}


def _scope_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(value.split())
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return set(value)
    return set()


def _check_token_info(info: dict, client: dict) -> None:
    if (
        info.get("client_id") != client["client_id"]
        or info.get("client_secret") != client["client_secret"]
        or info.get("token_uri") != TOKEN_URI
        or _scope_set(info.get("scopes")) != set(SCOPES)
    ):
        raise GmailError("已有 Gmail 授权与当前客户端或只读权限不符，请重新连接 Gmail。")


def _check_credentials(credentials: Any) -> None:
    granted = getattr(credentials, "granted_scopes", None)
    if _scope_set(credentials.scopes) != set(SCOPES) or (
        granted is not None and _scope_set(granted) != set(SCOPES)
    ):
        raise GmailError("Google 返回的权限不是所需的 Gmail 只读权限，请检查配置。")
    if not credentials.valid:
        raise GmailError("Google 没有返回有效授权，请重新连接 Gmail。")


def _save_token(path: Path, credentials: Any) -> None:
    """Replace the local token atomically; it is sensitive plaintext, not encrypted."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".gmail-", delete=False,
        ) as destination:
            temporary = Path(destination.name)
            destination.write(credentials.to_json())
        os.replace(temporary, path)
    except OSError:
        raise GmailError("Gmail 已授权，但本机无法保存授权文件，请检查项目文件夹权限。") from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def connect(
    credentials_path: Path, token_path: Path, interactive: bool = False,
) -> GmailMailbox:
    """Connect using existing consent, or open Google login only if explicitly requested."""
    config = _client_config(Path(credentials_path))
    token_path = Path(token_path)
    try:
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise GmailError("尚未安装 Gmail 连接组件，请先更新项目依赖。") from None

    credentials = None
    if token_path.is_file() and not interactive:
        info = _load_json(token_path, "Gmail 授权记录")
        _check_token_info(info, config["installed"])
        try:
            credentials = Credentials.from_authorized_user_info(info, SCOPES)
        except (ValueError, TypeError):
            raise GmailError("本机 Gmail 授权记录无效，请重新连接 Gmail。") from None

    if not interactive:
        if credentials is None:
            raise GmailError("尚未连接 Gmail，请点击连接按钮完成 Google 授权。")
        if not credentials.valid:
            if not credentials.refresh_token:
                raise GmailError("Gmail 授权已失效，请重新连接 Gmail。")
            try:
                request = Request()

                def limited_request(*args: Any, **kwargs: Any) -> Any:
                    kwargs["timeout"] = 30
                    return request(*args, **kwargs)

                credentials.refresh(limited_request)
            except Exception:  # noqa: BLE001 - OAuth errors may contain credential material.
                raise GmailError("Gmail 授权刷新失败，请检查网络或重新连接 Gmail。") from None
            _check_credentials(credentials)
            _save_token(token_path, credentials)
    else:
        try:
            flow = InstalledAppFlow.from_client_config(config, SCOPES)
            credentials = flow.run_local_server(
                host="127.0.0.1", port=0, timeout_seconds=180,
                prompt="select_account consent",
                authorization_prompt_message="请在浏览器完成 Google 授权。",
                success_message="授权完成，可以返回 StudentFlow。",
            )
        except Exception:  # noqa: BLE001 - Do not surface OAuth callback or token details.
            raise GmailError("Google 授权未完成，请检查网络、测试用户设置并重试。") from None
        _check_credentials(credentials)
        _save_token(token_path, credentials)

    _check_credentials(credentials)
    return GmailMailbox(AuthorizedSession(credentials, max_refresh_attempts=0))


def _validate_message_id(message_id: str) -> str:
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise GmailError("邮件编号无效，请重新加载邮件列表。")
    return message_id


class GmailMailbox:
    """Small read-only Gmail API client; injectable session enables offline tests."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def close(self) -> None:
        self._session.close()

    def _get_json(self, endpoint: str, params: dict | None = None, limit: int = 256_000) -> dict:
        response = None
        try:
            response = self._session.get(
                API_BASE + endpoint, params=params, timeout=30,
                max_allowed_time=30, allow_redirects=False, stream=True,
            )
            if response.status_code == 401:
                raise GmailError("Gmail 授权已失效，请重新连接 Gmail。")
            if response.status_code == 403:
                raise GmailError("Gmail 拒绝读取，请检查 API 是否启用、测试用户及只读授权。")
            if response.status_code == 429:
                raise GmailError("Gmail 请求次数过多，请稍后再试。")
            if response.status_code != 200:
                raise GmailError("Gmail 暂时无法读取，请检查网络后重试。")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > limit:
                raise GmailError("Gmail 返回的数据过大，本次未导入。")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(raw) + len(chunk) > limit:
                    raise GmailError("Gmail 返回的数据过大，本次未导入。")
                raw.extend(chunk)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise GmailError("Gmail 返回的数据格式不正确，请重试。")
            return result
        except GmailError:
            raise
        except Exception:  # noqa: BLE001 - Suppress request headers and private response bodies.
            raise GmailError("Gmail 读取失败，请检查网络及授权状态后重试。") from None
        finally:
            if response is not None:
                response.close()

    def profile(self) -> dict:
        result = self._get_json("/profile", limit=64_000)
        address = result.get("emailAddress")
        if not isinstance(address, str) or len(address) > 320 or "@" not in address:
            raise GmailError("无法确认已连接的 Gmail 账号，请重新连接。")
        return {"email_address": address}

    def list_messages(self, limit: int = 5) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise GmailError("每次读取的邮件数量应为 1 至 20 封。")
        result = self._get_json("/messages", {
            "labelIds": "INBOX", "maxResults": limit, "includeSpamTrash": "false",
        })
        messages = result.get("messages", [])
        if not isinstance(messages, list) or len(messages) > limit:
            raise GmailError("Gmail 邮件列表格式不正确，请重新加载。")
        output = []
        for message in messages:
            message_id = _validate_message_id(message.get("id") if isinstance(message, dict) else None)
            metadata = self._get_json(f"/messages/{message_id}", {
                "format": "metadata", "metadataHeaders": ["Subject", "From", "Date"],
            })
            payload = metadata.get("payload", {})
            headers = payload.get("headers", []) if isinstance(payload, dict) else None
            if not isinstance(headers, list) or len(headers) > 200:
                raise GmailError("Gmail 邮件头格式不正确，请重新加载。")
            selected = {}
            for header in headers:
                if not isinstance(header, dict):
                    continue
                name, value = header.get("name"), header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    selected.setdefault(name.lower(), value[:8192])
            output.append({
                "id": message_id, "subject": selected.get("subject", "（无主题）"),
                "sender": selected.get("from", ""), "sent_at": selected.get("date", ""),
            })
        return output

    def fetch_eml(self, message_id: str) -> bytes:
        message_id = _validate_message_id(message_id)
        result = self._get_json(
            f"/messages/{message_id}", {"format": "raw"}, limit=MAX_JSON_BYTES,
        )
        raw = result.get("raw")
        if not isinstance(raw, str) or not raw or len(raw) > MAX_RAW_CHARS:
            raise GmailError("邮件原文为空或超过 20 MB，本次未导入。")
        if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", raw):
            raise GmailError("邮件原文编码不正确，本次未导入。")
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            raise GmailError("邮件原文编码不正确，本次未导入。") from None
        if not decoded or len(decoded) > MAX_EMAIL_BYTES:
            raise GmailError("邮件原文为空或超过 20 MB，本次未导入。")
        return decoded
