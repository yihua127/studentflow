"""Preview and explicitly confirm account-bound Gmail category changes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from studentflow.email_label_catalog import gmail_label_names, validate_catalog
from studentflow.gmail_labels import MANAGED_LABELS, GmailLabelError, message_id, normalized_account

LabelSyncError = GmailLabelError
MAX_MESSAGES = 100


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _items(items, *, allow_tags=False):
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_MESSAGES:
        raise GmailLabelError(f"Select between 1 and {MAX_MESSAGES} reviewed emails to sync.")
    result, seen = [], set()
    for row in items:
        if not isinstance(row, dict):
            raise GmailLabelError("The reviewed categories changed. Review labels again.")
        if row.get("tag_ids") and not allow_tags:
            raise GmailLabelError("Load the saved label settings before syncing custom labels.")
        identifier = message_id(row.get("gmail_id"))
        category = row.get("category")
        subject, fingerprint = row.get("subject", ""), row.get("source_fingerprint", "")
        if (identifier in seen or not isinstance(category, str) or category not in MANAGED_LABELS
                or not isinstance(subject, str) or len(subject) > 2000
                or not isinstance(fingerprint, str) or len(fingerprint) > 500):
            raise GmailLabelError("The reviewed categories are invalid. Review labels again.")
        seen.add(identifier)
        result.append({"gmail_id": identifier, "category": category,
                       "subject": subject, "source_fingerprint": fingerprint})
    return sorted(result, key=lambda item: item["gmail_id"])


def _categories(labels, label_ids):
    return tuple(sorted(category for category, identifier in label_ids.items() if identifier in labels))


@dataclass(frozen=True)
class LabelSyncItem:
    gmail_id: str
    category: str
    subject: str
    source_fingerprint: str
    current_categories: tuple[str, ...]
    add_label: str | None
    remove_labels: tuple[str, ...]
    tag_ids: tuple[str, ...] = ()
    add_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabelSyncPreview:
    account: str
    fingerprint: str
    items: tuple[LabelSyncItem, ...]
    missing_labels: tuple[str, ...]
    label_ids: tuple[tuple[str, str], ...]
    catalog_signature: str = ""


def _preview_fingerprint(account, items, missing_labels, label_ids, catalog_signature=""):
    return _digest([account, [asdict(item) for item in items], missing_labels, label_ids, catalog_signature])


def prepare_label_sync(client, *, account: str, items: list[dict], catalog=None) -> LabelSyncPreview:
    """Read current state and bind a review to exact messages/categories; no writes."""
    account = normalized_account(account)
    if catalog is not None:
        return _prepare_catalog_sync(client, account, items, catalog)
    selected = _items(items)
    if normalized_account(client.account()) != account:
        raise GmailLabelError("Connect Gmail labels to the same account as the selected inbox.")
    label_ids = client.label_ids()
    entries = []
    for row in selected:
        current = _categories(client.message_labels(row["gmail_id"]), label_ids)
        category = row["category"]
        entries.append(LabelSyncItem(
            **row, current_categories=current,
            add_label=None if category in current else MANAGED_LABELS[category],
            add_labels=() if category in current else (MANAGED_LABELS[category],),
            remove_labels=tuple(MANAGED_LABELS[key] for key in current if key != category),
        ))
    targets = {row["category"] for row in selected}
    missing = tuple(MANAGED_LABELS[key] for key in sorted(targets) if key not in label_ids)
    mapping = tuple(sorted(label_ids.items()))
    fingerprint = _preview_fingerprint(account, entries, missing, mapping)
    return LabelSyncPreview(account, fingerprint, tuple(entries), missing, mapping)


@contextmanager
def _journal(data_dir):
    try:
        path = Path(data_dir) / "gmail_label_sync.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS label_sync_runs (fingerprint TEXT PRIMARY KEY, "
                       "payload TEXT NOT NULL, updated_at TEXT NOT NULL)")
            yield db
    except (OSError, sqlite3.Error):
        raise GmailLabelError("The local label-sync record could not be saved. Review labels before retrying.") from None


def label_sync_status(data_dir, fingerprint):
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise GmailLabelError("Review the label changes again.")
    with _journal(data_dir) as db:
        row = db.execute("SELECT payload FROM label_sync_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        raise GmailLabelError("The local label-sync record is unreadable. Review labels again.") from None


def _record(data_dir, fingerprint, result):
    with _journal(data_dir) as db:
        db.execute("INSERT INTO label_sync_runs VALUES (?, ?, ?) ON CONFLICT(fingerprint) "
                   "DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                   (fingerprint, json.dumps(result), datetime.now(UTC).isoformat()))


def apply_label_sync(client, preview: LabelSyncPreview, *, account: str, items: list[dict],
                     data_dir: Path, confirmed: bool = False, catalog=None) -> dict:
    """Set reviewed categories once; retries re-read actual state instead of duplicating writes.

Gmail's add/remove-label operations are idempotent. After a timeout the next
explicit attempt observes remote labels first, so an applied change is skipped.
The journal records completion/partial progress without message bodies or tokens.
"""
    if confirmed is not True:
        raise GmailLabelError("Review and explicitly confirm these Gmail label changes first.")
    if not isinstance(preview, LabelSyncPreview):
        raise GmailLabelError("The reviewed categories changed. Review labels again.")
    if catalog is not None or preview.catalog_signature:
        return _apply_catalog_sync(client, preview, account, items, data_dir, catalog)
    account = normalized_account(account)
    selected = _items(items)
    original = [{key: getattr(item, key) for key in (
        "gmail_id", "category", "subject", "source_fingerprint",
    )} for item in preview.items]
    if (account != preview.account or selected != original
            or preview.fingerprint != _preview_fingerprint(
                preview.account, preview.items, preview.missing_labels, preview.label_ids)):
        raise GmailLabelError("The selected account, emails or categories changed. Review labels again.")
    if normalized_account(client.account()) != account:
        raise GmailLabelError("Connect Gmail labels to the same account as the selected inbox.")
    label_ids = client.label_ids()
    if any(label_ids.get(key) != identifier for key, identifier in preview.label_ids):
        raise GmailLabelError("StudentFlow labels changed in Gmail. Review the latest labels again.")
    # Validate the complete selection before any label creation or message write.
    for item in preview.items:
        current = _categories(client.message_labels(item.gmail_id), label_ids)
        if current not in (item.current_categories, (item.category,)):
            raise GmailLabelError("A selected email's category changed in Gmail. Review the latest labels again.")

    result = {"status": "pending", "completed_ids": [], "failed_ids": [],
              "unchanged_count": 0, "errors": []}
    _record(data_dir, preview.fingerprint, result)
    targets = {item.category for item in preview.items}
    try:
        for category in sorted(targets):
            if category not in label_ids:
                # Creation may have succeeded despite a lost response. A later
                # explicit attempt lists exact label names and reuses them.
                label_ids[category] = client.create_label(category)
    except GmailLabelError as error:
        result.update(status="failed", failed_ids=[item.gmail_id for item in preview.items],
                      errors=[str(error)])
        _record(data_dir, preview.fingerprint, result)
        return result

    for item in preview.items:
        try:
            if client.label_ids() != label_ids:
                raise GmailLabelError("StudentFlow labels changed in Gmail. Review the latest labels again.")
            # Recheck immediately before each write, also covering a change
            # made in Gmail while a preceding message was being processed.
            current = _categories(client.message_labels(item.gmail_id), label_ids)
            if current == (item.category,):
                result["unchanged_count"] += 1
            elif current == item.current_categories:
                client.set_category(item.gmail_id, item.category, label_ids)
                if _categories(client.message_labels(item.gmail_id), label_ids) != (item.category,):
                    raise GmailLabelError("Gmail did not confirm the expected category. Review labels before retrying.")
            else:
                raise GmailLabelError("The email's category changed in Gmail. Review its labels before retrying.")
            result["completed_ids"].append(item.gmail_id)
        except GmailLabelError as error:
            result["failed_ids"].append(item.gmail_id)
            if str(error) not in result["errors"]:
                result["errors"].append(str(error))
        _record(data_dir, preview.fingerprint, result)
    result["status"] = ("synced" if not result["failed_ids"] else
                        "partial" if result["completed_ids"] else "failed")
    _record(data_dir, preview.fingerprint, result)
    return result


def _catalog_inputs(account, items, catalog):
    try:
        checked = validate_catalog(catalog, account=account)
    except ValueError:
        raise GmailLabelError("Label settings changed. Reload and preview labels again.") from None
    selection = _items(items, allow_tags=True)
    originals = {item["gmail_id"]: item for item in items}
    for item in selection:
        tags = originals[item["gmail_id"]].get("tag_ids", [])
        if (not isinstance(tags, (list, tuple)) or len(tags) > len(checked["custom_ids"])
                or any(not isinstance(tag, str) or tag not in checked["custom_ids"] for tag in tags)
                or len(set(tags)) != len(tags)):
            raise GmailLabelError("The selected custom labels changed. Review labels again.")
        item["tag_ids"] = tuple(sorted(tags))
    return checked, selection


def _desired_names(item, catalog):
    names, _allowed = gmail_label_names(catalog)
    return tuple(sorted({names[item["category"]], *(names[key] for key in item["tag_ids"])}))


def _managed_names(message_labels, label_ids):
    return tuple(sorted(name for name, identifier in label_ids.items() if identifier in message_labels))


def _prepare_catalog_sync(client, account, items, catalog):
    catalog, selected = _catalog_inputs(account, items, catalog)
    if normalized_account(client.account()) != account:
        raise GmailLabelError("Connect Gmail labels to the same account as the selected inbox.")
    client.configure_catalog(catalog)
    label_ids = client.managed_label_ids()
    entries, targets = [], set()
    for row in selected:
        current = _managed_names(client.message_labels(row["gmail_id"]), label_ids)
        desired = _desired_names(row, catalog)
        targets.update(desired)
        additions = tuple(name for name in desired if name not in current)
        entries.append(LabelSyncItem(
            **row, current_categories=current,
            add_label=additions[0] if additions else None, add_labels=additions,
            remove_labels=tuple(name for name in current if name not in desired),
        ))
    missing = tuple(sorted(targets - set(label_ids)))
    mapping = tuple(sorted(label_ids.items()))
    signature = catalog["version"]
    fingerprint = _preview_fingerprint(account, entries, missing, mapping, signature)
    return LabelSyncPreview(account, fingerprint, tuple(entries), missing, mapping, signature)


def _apply_catalog_sync(client, preview, account, items, data_dir, catalog):
    account = normalized_account(account)
    catalog, selection = _catalog_inputs(account, items, catalog)
    original = [{key: getattr(item, key) for key in (
        "gmail_id", "category", "subject", "source_fingerprint", "tag_ids",
    )} for item in preview.items]
    if (preview.account != account or catalog["version"] != preview.catalog_signature
            or selection != original or preview.fingerprint != _preview_fingerprint(
                preview.account, preview.items, preview.missing_labels, preview.label_ids,
                preview.catalog_signature)):
        raise GmailLabelError("The selected account, labels or messages changed. Preview labels again.")
    if normalized_account(client.account()) != account:
        raise GmailLabelError("Connect Gmail labels to the same account as the selected inbox.")
    client.configure_catalog(catalog)
    label_ids = client.managed_label_ids()
    if any(label_ids.get(name) != identifier for name, identifier in preview.label_ids):
        raise GmailLabelError("StudentFlow labels changed in Gmail. Review the latest labels again.")
    desired_by_id = {item["gmail_id"]: _desired_names(item, catalog) for item in selection}
    for item in preview.items:
        current = _managed_names(client.message_labels(item.gmail_id), label_ids)
        if current not in (item.current_categories, desired_by_id[item.gmail_id]):
            raise GmailLabelError("A selected email's labels changed in Gmail. Preview the latest labels again.")
    result = {"status": "pending", "completed_ids": [], "failed_ids": [],
              "unchanged_count": 0, "errors": []}
    _record(data_dir, preview.fingerprint, result)
    targets = set().union(*map(set, desired_by_id.values()))
    try:
        for name in sorted(targets):
            if name not in label_ids:
                label_ids[name] = client.create_named_label(name)
    except GmailLabelError as error:
        result.update(status="failed", failed_ids=[item.gmail_id for item in preview.items],
                      errors=[str(error)])
        _record(data_dir, preview.fingerprint, result)
        return result
    for item in preview.items:
        try:
            if client.managed_label_ids() != label_ids:
                raise GmailLabelError("StudentFlow labels changed in Gmail. Review the latest labels again.")
            current = _managed_names(client.message_labels(item.gmail_id), label_ids)
            desired = desired_by_id[item.gmail_id]
            if current == desired:
                result["unchanged_count"] += 1
            elif current == item.current_categories:
                client.set_named_labels(item.gmail_id, desired, label_ids)
                if _managed_names(client.message_labels(item.gmail_id), label_ids) != desired:
                    raise GmailLabelError("Gmail did not confirm these labels. Preview labels before retrying.")
            else:
                raise GmailLabelError("The email's labels changed in Gmail. Preview labels before retrying.")
            result["completed_ids"].append(item.gmail_id)
        except GmailLabelError as error:
            result["failed_ids"].append(item.gmail_id)
            if str(error) not in result["errors"]:
                result["errors"].append(str(error))
        _record(data_dir, preview.fingerprint, result)
    result["status"] = ("synced" if not result["failed_ids"] else
                        "partial" if result["completed_ids"] else "failed")
    _record(data_dir, preview.fingerprint, result)
    return result
