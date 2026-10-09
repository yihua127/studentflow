from studentflow.retrieval import build_chunks, search_chunks


def test_pdf_page_and_mail_sources_retained():
    chunks = build_chunks([{
        "id": "doc-1", "mail_id": "mail-1", "filename": "requirements.pdf", "status": "ok",
        "pages": [
            {"page": 1, "text": "Submission deadline: 16 October, 15:00."},
            {"page": 2, "text": "Presentation duration: five minutes."},
        ],
    }])
    hits = search_chunks("What is the submission deadline?", chunks)
    assert len(hits) == 1
    assert hits[0]["page"] == 1
    assert hits[0]["mail_id"] == "mail-1"
    assert hits[0]["filename"] == "requirements.pdf"
    assert "deadline" in hits[0]["matched_terms"]


def test_chinese_retrieval_and_missing_evidence():
    chunks = build_chunks([
        {"id": "one", "filename": "要求.docx", "text": "演讲时间为五分钟，问答时间为两分钟。"},
        {"id": "two", "filename": "预算.docx", "text": "项目预算为三百新币。"},
    ])
    hits = search_chunks("演讲时间是多少？", chunks)
    assert hits[0]["filename"] == "要求.docx"
    assert search_chunks("生物化学实验室地址", chunks) == []
    assert search_chunks("请问什么", chunks) == []
    assert search_chunks("What's the molecular mass of krypton?", chunks) == []


def test_unextracted_image_text_is_never_evidence():
    chunks = build_chunks([{
        "id": "image", "filename": "poster.png", "status": "needs_ocr", "text": "unverified text",
    }])
    assert chunks == []


def test_chunk_boundaries_keep_long_document_and_limit():
    text = "deadline October sixteen. " * 120
    chunks = build_chunks([{"id": "long", "mail_id": "m", "filename": "long.txt", "text": text}])
    assert len(chunks) > 1
    assert all(len(chunk["text"]) <= 800 for chunk in chunks)
    assert len({chunk["id"] for chunk in chunks}) == len(chunks)
    hits = search_chunks("deadline", chunks, limit=2)
    assert len(hits) == 2
    assert search_chunks("deadline", chunks, limit=0) == []
