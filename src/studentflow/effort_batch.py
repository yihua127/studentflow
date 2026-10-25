"""Estimate distinct review tasks without changing extraction facts or saved records."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from studentflow.extraction import evidence_text

VERSION = "effort-review-v1"
MAX_TASKS = 60


def estimate_review_efforts(rows, sources, api_key, model, *, estimator, cache=None):
    """Only called by an explicit extraction action after duplicate review grouping."""
    pending, estimates, warnings = [], {}, []
    for row in rows:
        if row.get("Study time (min)"):
            continue
        identifier = row["_row_id"]
        source = sources[identifier]
        quote = str(row.get("Supporting text") or "")
        text = evidence_text(source, source.get("_source_attachment_id"))
        start = max(0, text.find(quote) - 600) if quote else 0
        task = {
            "id": identifier, "title": row["Task"], "source_quote": quote[:4000],
            "source_text": text[start:start + 4000], "estimated_minutes": None,
        }
        signature = hashlib.sha256(json.dumps(
            [VERSION, model, source.get("gmail_account"), task], sort_keys=True,
        ).encode()).hexdigest()
        if cache is not None and signature in cache:
            estimates[identifier] = deepcopy(cache[signature])
        else:
            pending.append((task, signature))
    if len(pending) > MAX_TASKS:
        warnings.append("Study-time suggestions are limited to 60 tasks per extraction. Enter the remaining estimates manually or select fewer messages.")
    for offset in range(0, min(len(pending), MAX_TASKS), 30):
        batch = pending[offset:min(offset + 30, MAX_TASKS)]
        try:
            result = estimator([task for task, _ in batch], api_key, model)
            proposed = result["estimates"]
            if not isinstance(proposed, dict):
                raise TypeError("invalid_estimates")
            failed = bool(result.get("warnings"))
            if failed:
                warnings.append("Some tasks need more information before study time can be suggested. Review the estimate notes or enter a duration manually.")
            for task, signature in batch:
                value = proposed.get(task["id"])
                if not isinstance(value, dict):
                    raise TypeError("missing_estimate")
                # Treat the public service result as data even when a test adapter is used.
                minutes = value.get("suggested_minutes")
                if minutes is not None and (
                    isinstance(minutes, bool) or not isinstance(minutes, int)
                    or not 1 <= minutes <= 10000
                ):
                    raise ValueError("invalid_estimate")
                reason = value.get("estimate_reason")
                assumptions = value.get("assumptions", [])
                if (not isinstance(reason, str) or not reason.strip()
                        or not isinstance(assumptions, list)
                        or any(not isinstance(item, str) for item in assumptions)):
                    raise ValueError("invalid_explanation")
                item = {
                    "suggested_minutes": minutes, "estimate_reason": reason,
                    "assumptions": list(assumptions), "model": model,
                }
                estimates[task["id"]] = item
                if cache is not None and not failed:
                    cache[signature] = deepcopy(item)
        except Exception:  # noqa: BLE001 - provider errors may contain source text or keys.
            # Preserve the extracted tasks, not a partly validated estimate batch.
            for task, signature in batch:
                estimates.pop(task["id"], None)
                if cache is not None:
                    cache.pop(signature, None)
            warnings.append("Study-time suggestions could not be completed. The extracted tasks remain available; enter their study time manually or retry extraction.")
    return {"estimates": estimates, "warnings": list(dict.fromkeys(warnings))}


def apply_effort_suggestions(rows, sources, estimates):
    """Prefill editable totals while preserving immutable suggestions on the server."""
    prepared, evidence = deepcopy(rows), deepcopy(sources)
    for row in prepared:
        identifier = row["_row_id"]
        stated = evidence[identifier].get("_stated_minutes")
        suggestion = deepcopy(estimates.get(identifier, {})) if not stated else {}
        suggestion["stated_minutes"] = stated
        evidence[identifier]["_effort_suggestion"] = suggestion
        row["Source-stated time (min)"] = stated
        row["AI suggestion (min)"] = suggestion.get("suggested_minutes")
        basis = suggestion.get("estimate_reason", "")
        assumptions = suggestion.get("assumptions", [])
        if assumptions:
            basis += " Assumptions: " + "; ".join(assumptions)
        row["Estimate basis"] = basis or ("Duration stated in the source." if stated else "Enter a duration or request a study-time suggestion.")
        if not row.get("Study time (min)") and suggestion.get("suggested_minutes"):
            row["Study time (min)"] = suggestion["suggested_minutes"]
    return prepared, evidence
