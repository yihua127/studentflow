"""Read local RFC 822 messages without executing or writing attachment content."""

from __future__ import annotations

import hashlib
import ntpath
import re
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

MAX_EMAIL_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_BODY_CHARS = 200_000
MAX_PARTS = 200
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}


class _VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in {"br", "p", "div", "li", "tr", "h1", "h2"}:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif not self.hidden_depth and tag in {"p", "div", "li", "tr"}:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.fragments.append(data)


def _html_text(value: str) -> str:
    parser = _VisibleHTML()
    parser.feed(value)
    parser.close()
    return re.sub(r"\n[ \t]*\n+", "\n\n", "".join(parser.fragments)).strip()


def _decode_text(part: Message, warnings: list[str]) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        value = part.get_payload()
        return value if isinstance(value, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        warnings.append(f"正文字符编码 {charset} 无法完整解码，已使用 UTF-8 替代字符。")
        return payload.decode("utf-8", errors="replace")


def _filename(value: str, fallback: str, warnings: list[str]) -> str:
    # Names are display metadata only. No attachment is ever written to this name.
    name = ntpath.basename(value.replace("\x00", ""))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:240] or fallback
    if name != value:
        warnings.append("附件文件名已去除路径或控制字符，仅用于显示。")
    return name


def parse_eml_bytes(raw: bytes, source_name: str) -> dict:
    """Parse one uploaded/local message. Raise ValueError when its size is excessive."""
    if len(raw) > MAX_EMAIL_BYTES:
        raise ValueError("单封邮件超过 20 MB，未解析。")
    if not raw.strip():
        raise ValueError("邮件文件为空。")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    warnings: list[str] = []
    attachments: list[dict] = []
    part_count = 0

    def body_and_attachments(part: Message, depth: int = 0) -> tuple[str, str]:
        nonlocal part_count
        part_count += 1
        if part_count > MAX_PARTS or depth > 24:
            warnings.append("邮件 MIME 层级或分段数量过多，剩余内容未解析。")
            return "", ""
        content_type = part.get_content_type()
        warnings.extend(f"MIME 解析提示：{type(defect).__name__}" for defect in part.defects)
        if content_type == "message/rfc822" or content_type.startswith("message/"):
            warnings.append("内嵌邮件附件未展开；请将需要处理的邮件单独上传。")
            return "", ""
        original_name = part.get_filename()
        is_attachment = (
            part.get_content_disposition() == "attachment"
            or original_name is not None
            or (not part.is_multipart() and content_type not in {"text/plain", "text/html"})
        )
        if is_attachment:
            attachment_warnings: list[str] = []
            fallback = f"attachment_{len(attachments) + 1}"
            filename = _filename(original_name or fallback, fallback, attachment_warnings)
            content = part.get_payload(decode=True) or b""
            attachment_warnings.extend(
                f"MIME 解析提示：{type(defect).__name__}" for defect in part.defects
            )
            attachment_id = hashlib.sha256(content).hexdigest()
            extension = ntpath.splitext(filename)[1].lower()
            if len(content) > MAX_ATTACHMENT_BYTES:
                attachment_warnings.append("附件超过 10 MB，内容未保留或解析。")
                content = b""
            elif extension not in ALLOWED_EXTENSIONS:
                attachment_warnings.append("暂不支持此附件类型；内容未保留，不执行附件。")
                content = b""
            attachments.append({
                "id": attachment_id,
                "filename": filename,
                "content_type": content_type,
                "content": content,
                "warnings": attachment_warnings,
            })
            return "", ""
        if part.is_multipart():
            results = [body_and_attachments(child, depth + 1) for child in part.iter_parts()]
            if part.get_content_subtype() == "alternative":
                # Walk every child to retain attachment metadata, but use one body alternative.
                for preferred in ("text/plain", "text/html"):
                    for body, kind in results:
                        if body.strip() and kind == preferred:
                            return body, kind
                return next(((body, kind) for body, kind in results if body.strip()), ("", ""))
            bodies = [(body, kind) for body, kind in results if body.strip()]
            text = "\n\n".join(body for body, _ in bodies)
            kind = "text/plain" if any(kind == "text/plain" for _, kind in bodies) else "text/html"
            return text, kind
        value = _decode_text(part, warnings)
        # Decoding malformed base64 may append defects after the initial inspection.
        warnings.extend(f"MIME 解析提示：{type(defect).__name__}" for defect in part.defects)
        if content_type == "text/html":
            value = _html_text(value)
        return value.strip(), content_type

    body, _ = body_and_attachments(message)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
        warnings.append("邮件正文超过 200,000 字符，已截断。")
    sent_at = None
    if message.get("Date"):
        try:
            date = parsedate_to_datetime(str(message["Date"]))
            if date.tzinfo is None or date.utcoffset() is None:
                warnings.append("邮件发送时间缺少时区；相对日期需要人工确认。")
            else:
                sent_at = date.isoformat()
        except (ValueError, TypeError, OverflowError):
            warnings.append("邮件发送时间无法识别；相对日期需要人工确认。")
    else:
        warnings.append("邮件没有发送时间；相对日期需要人工确认。")
    if not body:
        warnings.append("邮件未找到可读取的正文。")
    return {
        "id": hashlib.sha256(raw).hexdigest(),
        "source_name": source_name,
        "subject": str(message.get("Subject", "（无主题）"))[:1000],
        "sender": str(message.get("From", ""))[:1000],
        "sent_at": sent_at,
        "body": body,
        "warnings": list(dict.fromkeys(warnings)),
        "attachments": attachments,
    }
