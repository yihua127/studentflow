"""Copied PDF proposals retain source identity even when a quote needs review."""

import pytest

from studentflow.attachments import extract_attachment
from studentflow.batch_ui import review_rows, validate_selected_rows
from studentflow.extraction import validate_extraction
from studentflow.source_extraction import analyze_sources


def pdf_bytes(lines):
    stream = "BT /F1 12 Tf 72 720 Td " + " 0 -18 Td ".join(f"({line}) Tj" for line in lines) + " ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream.encode())).encode() + b" >>\nstream\n" + stream.encode() + b"\nendstream",
    ]
    output, offsets = bytearray(b"%PDF-1.4\n"), []
    for index, content in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + content + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


@pytest.mark.parametrize("invalid_quote", [False, True])
def test_five_pdf_tasks_are_five_review_rows_across_two_identical_copies(invalid_quote):
    names = ["Research outline", "Source evaluation quiz", "Individual essay", "Presentation slides", "Research presentation"]
    dates = [f"{12 + index} October 2026, 17:00" for index in range(5)]
    content = pdf_bytes([line for pair in zip(names, dates) for line in pair])
    mails = [{
        "id": identifier, "gmail_id": identifier, "gmail_account": "student@example.invalid",
        "subject": "Forwarded course syllabus " + identifier,
        "body": "", "sent_at": "2026-10-09T09:00:00+08:00",
        "attachments": [{"id": "file-" + identifier, "filename": "syllabus.pdf", "content": content}],
    } for identifier in ("a", "b")]
    calls = []

    def extractor(body, subject, sent_at, api_key, model, attachments=None):
        calls.append(True)
        document = extract_attachment(attachments[0])
        assert "\n" in document["text"]
        tasks = [{
            "title": name, "due_at": f"2026-10-{12 + index:02d}T17:00:00+08:00",
            "deadline_basis": "absolute", "estimated_minutes": None,
            "source_quote": "Fabricated unrelated instruction." if invalid_quote else f"{name} {date}",
            "source_attachment_id": None, "confidence": 0.9, "needs_clarification": [],
        } for index, (name, date) in enumerate(zip(names, dates))]
        return validate_extraction({"tasks": tasks}, "", [document])

    output = analyze_sources(mails, "student@example.invalid", "fake", "fake", extractor=extractor)
    entries = [dict(mail_id=mail["id"], gmail_id=mail["gmail_id"], cache_key=mail["id"], **output[mail["id"]]) for mail in mails]
    rows, sources = review_rows(entries, {mail["id"]: mail for mail in mails})
    assert len(calls) == 1
    assert len(rows) == 5
    assert all("Duplicate mentions combined (2)." in row["Review notes"] for row in rows)
    for row in rows:
        assert "Forwarded course syllabus a" in row["Source message"]
        assert "Forwarded course syllabus b" in row["Source message"]
    selected = [dict(row, Include=True) for row in rows]
    if invalid_quote:
        assert all(row["Supporting text"] == "" for row in rows)
        with pytest.raises(ValueError, match="must match"):
            validate_selected_rows(selected, sources, True)
    else:
        records = validate_selected_rows(selected, sources, True)
        assert len(records) == 5
        assert all(len(record["sources"]) == 2 for record in records)
        assert all("\n" in row["Supporting text"] for row in rows)
