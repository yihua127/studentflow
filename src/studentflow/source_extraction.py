"""Analyze unique email bodies and document bytes, then restore mail provenance."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

from studentflow import extraction
from studentflow.attachments import extract_attachment
from studentflow.extraction_cache import ExtractionCache, attach_item_ids, source_cache_key

# Fixed appointments now have a separate review; refresh old browser task proposals
# as well as prompt-hashed source caches. Saved human-reviewed tasks stay unchanged.
PIPELINE_VERSION = "source-pipeline-v3-fixed-events"
_SOURCE_FAILURE = "This source could not be analyzed. Run extraction again to retry."
_MISSING_ANCHOR_WARNING = "The sent time is missing or has no valid timezone. Relative dates need manual review."


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _anchor(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC).isoformat() if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _warnings_for_copy(result, canonical_name, actual_name, *, context_free=False):
    warnings = []
    for warning in result.get("warnings", []):
        if context_free and warning == _MISSING_ANCHOR_WARNING:
            continue
        if canonical_name and warning.startswith(canonical_name + ":"):
            warning = str(actual_name or "Attachment") + warning[len(canonical_name):]
        warnings.append(warning)
    return warnings


def analyze_sources(
    mails: list[dict], account, api_key, model, *, extractor,
    cache: ExtractionCache | dict | None = None, reanalyze=False, progress=None,
) -> dict:
    """Extract each body/document once, sharing results across identical copies.

    Document analysis uses no email date. Only relative document deadlines get
    an additional analysis for each distinct message date. Persisted successful
    sources survive a failed sibling source, so retries only repeat failed work.
    The extractor is injected; importing this module never performs model calls.
    """
    prompt_version = _digest((PIPELINE_VERSION + extraction.SYSTEM_PROMPT).encode())
    jobs, references, warnings = {}, {}, {}
    documents, resolved, failures = {}, {}, {}
    account = str(account or "").strip().casefold()

    def key_for(kind, content_hash, context=None):
        return source_cache_key(kind, account, content_hash, model, {
            "prompt_version": prompt_version, "context": context,
        })

    for mail in mails:
        identifier = mail["id"]
        references[identifier] = []
        warnings[identifier] = []
        body = mail.get("body") or ""
        if body.strip():
            values = [mail.get("subject") or "", body, mail.get("sent_at")]
            content_hash = _digest(json.dumps(values, ensure_ascii=False).encode())
            key = key_for("body", content_hash)
            jobs.setdefault(key, (body, mail.get("subject") or "", mail.get("sent_at"), None))
            references[identifier].append({"key": key, "hash": content_hash, "attachment": None})
        for attachment in mail.get("attachments", []):
            content = attachment.get("content")
            name = attachment.get("filename") or "Attachment"
            if not isinstance(content, (bytes, bytearray)) or not content:
                warnings[identifier].append(f"{name}: No readable attachment content was available.")
                continue
            content_hash = _digest(bytes(content))
            if content_hash not in documents:
                canonical = {
                    "id": "document-" + content_hash,
                    "filename": "document" + Path(name).suffix.lower(),
                    "content": bytes(content), "warnings": [],
                }
                try:
                    document = extract_attachment(canonical)
                    documents[content_hash] = (canonical, document)
                except Exception:  # noqa: BLE001 - Isolate malformed local document readers.
                    documents[content_hash] = (canonical, {"text": "", "warnings": ["The attachment could not be read."]})
            canonical, document = documents[content_hash]
            warnings[identifier].extend(f"{name}: {warning}" for warning in document.get("warnings", []))
            if not document.get("text", "").strip():
                warnings[identifier].append(f"{name}: No readable text was available for task extraction.")
                continue
            key = key_for("attachment", content_hash)
            jobs.setdefault(key, ("", "Attached document", None, [canonical]))
            references[identifier].append({"key": key, "hash": content_hash, "attachment": attachment})

    def execute(new_jobs, start, span, expected_relative=None):
        def valid_binding(key, result):
            if not expected_relative or key not in expected_relative:
                return True
            quotes = [task.get("source_quote") for task in result.get("tasks", [])
                      if task.get("deadline_basis") == "message_relative"]
            expected = expected_relative[key]
            return (all(isinstance(quote, str) and quote.strip() for quote in quotes)
                    and len(quotes) == len(expected) == len(set(quotes)) == len(set(expected))
                    and set(quotes) == set(expected))

        pending = {}
        for key, args in new_jobs.items():
            previous = None
            if cache is not None and not reanalyze:
                try:
                    previous = cache.get(key)
                except Exception:  # noqa: BLE001 - Cache outages must not prevent extraction.
                    previous = None
            if (isinstance(previous, dict) and isinstance(previous.get("tasks"), list)
                    and valid_binding(key, previous)):
                resolved[key] = (deepcopy(previous), True)
            else:
                pending[key] = args

        def invoke(key, args):
            body, subject, sent_at, attachments = args
            result = extractor(
                body, subject, sent_at, api_key, model,
                **({"attachments": attachments} if attachments is not None else {}),
            )
            result = attach_item_ids(key, result)
            if not valid_binding(key, result):
                raise ValueError("Relative deadline analysis did not preserve every source instruction.")
            if cache is not None:
                try:
                    if isinstance(cache, dict):
                        cache[key] = deepcopy(result)
                    else:
                        cache.put(key, result)
                except Exception:  # noqa: BLE001 - Keep successful proposals when cache writes fail.
                    result["warnings"].append("These extraction results could not be saved for reuse.")
            return result

        completed = len(new_jobs) - len(pending)
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(invoke, key, args): key for key, args in pending.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    resolved[key] = (future.result(), False)
                except Exception:  # noqa: BLE001 - Provider details stay private and sibling jobs survive.
                    failures[key] = _SOURCE_FAILURE
                completed += 1
                if progress:
                    progress(start + span * completed / max(1, len(new_jobs)), "Analyzing message and attachment sources...")

    execute(jobs, 0, 0.8)
    dated_jobs, bindings, expected_relative = {}, {}, {}
    for mail in mails:
        for reference in references[mail["id"]]:
            if reference["attachment"] is None or reference["key"] not in resolved:
                continue
            result = resolved[reference["key"]][0]
            if not any(task.get("deadline_basis") == "message_relative" for task in result["tasks"]):
                continue
            sent_at = _anchor(mail.get("sent_at"))
            if sent_at is None:
                continue
            key = key_for("attachment-relative", reference["hash"], sent_at)
            canonical = documents[reference["hash"]][0]
            dated_jobs.setdefault(key, ("", "Attached document", sent_at, [canonical]))
            expected_relative[key] = [task.get("source_quote") for task in result["tasks"]
                                      if task.get("deadline_basis") == "message_relative"]
            bindings[(mail["id"], reference["key"])] = key
    execute(dated_jobs, 0.8, 0.2, expected_relative)

    output = {}
    for mail in mails:
        identifier, tasks = mail["id"], []
        mail_warnings = list(warnings[identifier])
        source_results, failed = [], False
        for reference in references[identifier]:
            key, attachment = reference["key"], reference["attachment"]
            if key in failures:
                label = attachment.get("filename") if attachment else "Message text"
                mail_warnings.append(f"{label}: {_SOURCE_FAILURE}")
                failed = True
                continue
            result, cached = resolved[key]
            source_results.append(cached)
            proposals = deepcopy(result["tasks"])
            canonical_name = documents[reference["hash"]][0]["filename"] if attachment is not None else None

            actual_name = attachment.get("filename") if attachment else None
            mail_warnings.extend(_warnings_for_copy(
                result, canonical_name, actual_name, context_free=attachment is not None,
            ))
            bound_key = bindings.get((identifier, key))
            if bound_key:
                if bound_key in failures:
                    failed = True
                    mail_warnings.append("Relative attachment deadlines could not be resolved. Run extraction again to retry.")
                else:
                    bound, was_cached = resolved[bound_key]
                    source_results.append(was_cached)
                    relative = [deepcopy(task) for task in bound["tasks"]
                                if task.get("deadline_basis") == "message_relative"]
                    proposals = [task for task in proposals if task.get("deadline_basis") != "message_relative"] + relative
                    mail_warnings.extend(_warnings_for_copy(bound, canonical_name, actual_name))
            for task in proposals:
                task["_source_hash"] = reference["hash"]
                if attachment is not None:
                    task["source_attachment_id"] = attachment["id"]
                    task["source_filename"] = attachment.get("filename")
                    if task.get("deadline_basis") == "message_relative" and (not bound_key or bound_key in failures):
                        task["due_at"] = None
                else:
                    task["source_attachment_id"] = None
                    task["source_filename"] = None
                tasks.append(task)
        if failed and not source_results:
            output[identifier] = {"error": "Task extraction failed. Run extraction again to retry."}
        else:
            output[identifier] = {
                "result": {"tasks": tasks, "warnings": list(dict.fromkeys(mail_warnings))},
                "cached": bool(source_results) and all(source_results) and not failed,
                "partial": failed,
            }
    if progress:
        progress(1.0, "Selected sources processed.")
    return output
