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
        warnings.append(f"Some text could not be decoded using {charset}. Unreadable characters have been replaced.")
        return payload.decode("utf-8", errors="replace")


def _filename(value: str, fallback: str, warnings: list[str]) -> str:
    # Names are display metadata only. No attachment is ever written to this name.
    name = ntpath.basename(value.replace("\x00", ""))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:240] or fallback
    if name != value:
        warnings.append("The attachment name was shortened or cleaned for display.")
    return name


def parse_eml_bytes(raw: bytes, source_name: str) -> dict:
    """Parse one uploaded/local message. Raise ValueError when its size is excessive."""
    if len(raw) > MAX_EMAIL_BYTES:
        raise ValueError("The message exceeds 20 MB and could not be read.")
    if not raw.strip():
        raise ValueError("The message is empty.")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    warnings: list[str] = []
    attachments: list[dict] = []
    part_count = 0

    def body_and_attachments(part: Message, depth: int = 0) -> tuple[str, str]:
        nonlocal part_count
        part_count += 1
        if part_count > MAX_PARTS or depth > 24:
            warnings.append("This message is too complex to read in full. Some content was skipped.")
            return "", ""
        content_type = part.get_content_type()
        warnings.extend("Some message formatting could not be read." for defect in part.defects)
        if content_type == "message/rfc822" or content_type.startswith("message/"):
            warnings.append("Attached emails are not expanded. Select the original message from your inbox instead.")
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
                "Some message formatting could not be read." for defect in part.defects
            )
            attachment_id = hashlib.sha256(content).hexdigest()
            extension = ntpath.splitext(filename)[1].lower()
            if len(content) > MAX_ATTACHMENT_BYTES:
                attachment_warnings.append("The attachment exceeds 10 MB. Its content was not saved or read.")
                content = b""
            elif extension not in ALLOWED_EXTENSIONS:
                attachment_warnings.append("This attachment type is not supported. Its content was not saved.")
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
        warnings.extend("Some message formatting could not be read." for defect in part.defects)
        if content_type == "text/html":
            value = _html_text(value)
        return value.strip(), content_type

    body, _ = body_and_attachments(message)
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
        warnings.append("The message body exceeds 200,000 characters and has been shortened.")
    sent_at = None
    if message.get("Date"):
        try:
            date = parsedate_to_datetime(str(message["Date"]))
            if date.tzinfo is None or date.utcoffset() is None:
                warnings.append("The sent time has no timezone. Check relative dates before confirming a task.")
            else:
                sent_at = date.isoformat()
        except (ValueError, TypeError, OverflowError):
            warnings.append("The sent time could not be read. Check relative dates before confirming a task.")
    else:
        warnings.append("The message has no sent time. Check relative dates before confirming a task.")
    if not body:
        warnings.append("No readable message body was found.")
    return {
        "id": hashlib.sha256(raw).hexdigest(),
        "source_name": source_name,
        "subject": str(message.get("Subject", "(No subject)"))[:1000],
        "sender": str(message.get("From", ""))[:1000],
        "reply_to": str(message.get("Reply-To", ""))[:1000],
        "sent_at": sent_at,
        "body": body,
        "warnings": list(dict.fromkeys(warnings)),
        "attachments": attachments,
    }
