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
        result["warnings"].append("附件超过 10 MB，未解析。")
        return result
    if not content:
        result["status"] = "blocked" if result["warnings"] else "empty"
        result["warnings"].append("附件没有可用内容。")
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
            "图片尚未识别，需要用户授权后运行 OCR；当前不能据此回答。"
            if valid_signature else "文件内容与图片扩展名不匹配。"
        )
        return result
    try:
        if extension == ".pdf":
            if b"%PDF-" not in content[:1024]:
                raise ValueError("文件内容不是可识别的 PDF。")
            reader = PdfReader(io.BytesIO(content), strict=False)
            if reader.is_encrypted:
                result["status"] = "encrypted"
                result["warnings"].append("PDF 已加密，未读取；请提供未加密版本。")
                return result
            total_chars = 0
            truncated = len(reader.pages) > MAX_PAGES
            if truncated:
                result["warnings"].append(f"PDF 超过 {MAX_PAGES} 页，仅提取前 {MAX_PAGES} 页。")
            for number, page in enumerate(reader.pages[:MAX_PAGES], 1):
                text = (page.extract_text() or "").strip()
                available = min(MAX_PAGE_CHARS, MAX_TEXT_CHARS - total_chars)
                if len(text) > available:
                    text = text[:available]
                    truncated = True
                    result["warnings"].append(f"PDF 第 {number} 页达到字符上限，已截断。")
                if not text:
                    result["warnings"].append(f"PDF 第 {number} 页没有可提取文字，可能需要 OCR。")
                result["pages"].append({"page": number, "text": text})
                total_chars += len(text)
                if total_chars >= MAX_TEXT_CHARS:
                    truncated = True
                    result["warnings"].append("PDF 达到总字符上限，后续页面未处理。")
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
                    result["warnings"].append("DOCX 解压体积或压缩比超过安全上限，未读取。")
                    return result
                if "word/document.xml" not in archive.namelist():
                    raise ValueError("文件不包含 Word 文档正文。")
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
            result["warnings"].append("Word 仅提取正文段落和表格；未识别图片，未计算实际页码。")
            if truncated:
                result["warnings"].append("Word 正文达到字符上限，已截断。")
        else:
            result["warnings"].append("仅支持 PDF、DOCX、PNG 和 JPG 附件。")
    except Exception as exc:
        result["status"] = "error"
        result["warnings"].append(f"附件解析失败（{type(exc).__name__}），请检查原文件。")
    return result
