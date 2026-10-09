"""Bounded, in-memory extraction of supported email attachments."""

from __future__ import annotations

import io
import ntpath
import zipfile

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 60
MAX_TEXT_CHARS = 200_000
MAX_PAGE_CHARS = 20_000
MAX_ZIP_BYTES = 40 * 1024 * 1024


def extract_attachment(attachment: dict) -> dict:
    """Return source text and honest extraction status; never execute/save a file."""
    result = {
        "id": attachment["id"],
        "filename": attachment.get("filename", "attachment"),
        "status": "unsupported",
        "text": "",
        "pages": [],
        "warnings": list(attachment.get("warnings", [])),
    }
    content = attachment.get("content", b"")
    if len(content) > MAX_BYTES:
        result["status"] = "blocked"
        result["warnings"].append("The attachment exceeds 10 MB and could not be read.")
        return result
    if not content:
        result["status"] = "blocked" if result["warnings"] else "empty"
        result["warnings"].append("The attachment has no readable content.")
        return result
    extension = ntpath.splitext(result["filename"])[1].lower()
    if extension in {".png", ".jpg", ".jpeg"}:
        valid_signature = (
            content.startswith(b"\x89PNG\r\n\x1a\n")
            if extension == ".png"
            else content.startswith(b"\xff\xd8\xff")
        )
        result["status"] = "needs_ocr" if valid_signature else "error"
        result["warnings"].append(
            "Text recognition is not available for images. Image content is not included in search results."
            if valid_signature else "The file content does not match its image format."
        )
        return result
    try:
        if extension == ".pdf":
            if b"%PDF-" not in content[:1024]:
                raise ValueError("The file is not a readable PDF.")
            reader = PdfReader(io.BytesIO(content), strict=False)
            if reader.is_encrypted:
                result["status"] = "encrypted"
                result["warnings"].append("This PDF is encrypted and could not be read. Use an unencrypted copy.")
                return result
            total_chars = 0
            truncated = len(reader.pages) > MAX_PAGES
            if truncated:
                result["warnings"].append(f"This PDF exceeds {MAX_PAGES} pages. Only the first {MAX_PAGES} pages were read.")
            for number, page in enumerate(reader.pages[:MAX_PAGES], 1):
                text = (page.extract_text() or "").strip()
                available = min(MAX_PAGE_CHARS, MAX_TEXT_CHARS - total_chars)
                if len(text) > available:
                    text = text[:available]
                    truncated = True
                    result["warnings"].append(f"PDF page {number} reached the text limit and was shortened.")
                if not text:
                    result["warnings"].append(f"PDF page {number} contains no selectable text. Text recognition may be needed.")
                result["pages"].append({"page": number, "text": text})
                total_chars += len(text)
                if total_chars >= MAX_TEXT_CHARS:
                    truncated = True
                    result["warnings"].append("This PDF reached the text limit. The remaining pages were not read.")
                    break
            result["text"] = "\n\n".join(page["text"] for page in result["pages"] if page["text"])
            if not result["text"]:
                result["status"] = "needs_ocr"
            elif truncated or any(not page["text"] for page in result["pages"]):
                result["status"] = "partial"
            else:
                result["status"] = "ok"
        elif extension == ".docx":
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if (
                    len(members) > 2000
                    or sum(member.file_size for member in members) > MAX_ZIP_BYTES
                    or any(member.file_size > 20 * 1024 * 1024 for member in members)
                    or any(member.file_size / max(member.compress_size, 1) > 200 for member in members)
                ):
                    result["status"] = "blocked"
                    result["warnings"].append("This Word document exceeds the processing limits and could not be read.")
                    return result
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("The file contains no Word document body.")
            document = Document(io.BytesIO(content))
            lines: list[str] = []
            for element in document.element.body:
                if element.tag == qn("w:p"):
                    lines.append(Paragraph(element, document).text)
                elif element.tag == qn("w:tbl"):
                    table = Table(element, document)
                    for row in table.rows:
                        lines.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(lines).strip()
            truncated = len(text) > MAX_TEXT_CHARS
            result["text"] = text[:MAX_TEXT_CHARS]
            # Word's pagination is layout-dependent: no fabricated page number.
            result["pages"] = [{"page": None, "text": result["text"]}]
            result["status"] = "partial" if truncated else ("ok" if text else "empty")
            result["warnings"].append("Text and tables were read from this Word document. Images and page numbers are not included.")
            if truncated:
                result["warnings"].append("This Word document reached the text limit and was shortened.")
        else:
            result["warnings"].append("This attachment format is not supported. Use PDF or DOCX for text search.")
    except Exception:  # noqa: BLE001 - Malformed files return a safe, user-facing status.
        result["status"] = "error"
        result["warnings"].append("The attachment could not be read. Check the original file.")
    return result
