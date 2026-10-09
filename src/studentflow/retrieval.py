"""Small, local keyword retriever with explicit email/file/page provenance."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

CHUNK_CHARS = 800
CHUNK_OVERLAP = 120
STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "and", "or", "for",
    "in", "on", "at", "it", "this", "that", "with", "from", "please", "what", "when",
    "where", "who", "how", "does", "do", "can", "i", "my", "you", "your", "me",
    "什么", "多少", "哪些", "这个", "那个", "请问", "是否", "怎么", "如何", "可以",
    "一下", "告诉", "根据", "里面", "中的", "的是", "时候",
}


def _tokens(text: str) -> list[str]:
    lower = text.casefold()
    tokens = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", lower)
    for run in re.findall(r"[\u3400-\u9fff]+", lower):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return [token for token in tokens if token not in STOP_WORDS and len(token) > 1]


def build_chunks(documents: list[dict]) -> list[dict]:
    """Chunk extracted documents, never loading other files or answer directories."""
    chunks: list[dict] = []
    for document in documents:
        if document.get("status") in {"error", "encrypted", "blocked", "needs_ocr", "unsupported"}:
            continue
        pages = document.get("pages") or [{"page": None, "text": document.get("text", "")}]
        for page in pages:
            text = str(page.get("text", "")).strip()
            for start in range(0, len(text), CHUNK_CHARS - CHUNK_OVERLAP):
                chunk_text = text[start:start + CHUNK_CHARS].strip()
                if not chunk_text:
                    continue
                mail_id = str(document.get("mail_id", ""))
                filename = str(document.get("filename", ""))
                identity = f"{mail_id}|{document.get('id', filename)}|{page.get('page')}|{start}"
                chunks.append({
                    "id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                    "text": chunk_text,
                    "filename": filename,
                    "page": page.get("page"),
                    "mail_id": mail_id,
                })
                if start + CHUNK_CHARS >= len(text):
                    break
    return chunks


def search_chunks(question: str, chunks: list[dict], limit: int = 4) -> list[dict]:
    """Rank lexical evidence; return [] when the question has no matching terms."""
    query = set(_tokens(question))
    if not query or not chunks or limit <= 0:
        return []
    token_counts = [Counter(_tokens(chunk["text"])) for chunk in chunks]
    document_frequency = Counter(token for counts in token_counts for token in counts)
    ranked: list[dict] = []
    for chunk, counts in zip(chunks, token_counts, strict=True):
        matching = query & counts.keys()
        if not matching:
            continue
        score = sum(
            (1.0 + math.log(counts[token]))
            * (1.0 + math.log((len(chunks) + 1) / (document_frequency[token] + 1)))
            for token in matching
        )
        score *= len(matching) / len(query)
        score /= 1.0 + math.log(1.0 + sum(counts.values()) / 100)
        ranked.append({**chunk, "score": round(score, 6), "matched_terms": sorted(matching)})
    ranked.sort(key=lambda chunk: (-chunk["score"], chunk["id"]))
    return ranked[:min(limit, 20)]
