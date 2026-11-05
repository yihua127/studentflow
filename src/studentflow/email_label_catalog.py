"""Account-local display names and manually chosen custom email tags."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

DEFAULT_NAMES = {
    "needs_reply": "Needs reply", "needs_action": "Needs action", "ignore": "No action needed",
}
TAG_ID = re.compile(r"tag_[0-9a-f]{32}\Z")
MAX_CUSTOM_LABELS = 50


def _account(value):
    if (not isinstance(value, str) or not value.strip() or "@" not in value
            or len(value) > 320 or any(ord(char) < 33 for char in value.strip())):
        raise ValueError("Choose a connected Gmail account before editing labels.")
    return value.strip().casefold()


def validate_name(value):
    if not isinstance(value, str):
        raise ValueError("Enter a label name.")  # noqa: TRY004 - One validation error type for the UI.
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Use a label name without control characters.")
    name = unicodedata.normalize("NFC", value.strip())
    if (not 1 <= len(name) <= 60 or "/" in name or "\\" in name
            or any(unicodedata.category(char).startswith("C") for char in name)):
        raise ValueError("Use 1–60 characters without slashes or control characters for a label name.")
    if name.casefold() in {"all messages", "unclassified"}:
        raise ValueError("That name is reserved for an inbox view. Choose another name.")
    return name


def _version(value):
    payload = {key: value[key] for key in ("account", "names", "aliases", "custom_ids")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def default_catalog(account):
    value = {"account": _account(account), "names": dict(DEFAULT_NAMES),
             "aliases": {key: [] for key in DEFAULT_NAMES}, "custom_ids": []}
    return value | {"version": _version(value)}


def validate_catalog(value, *, account=None):
    if not isinstance(value, dict):
        raise ValueError("The saved label names could not be read.")  # noqa: TRY004 - Invalid saved input.
    owner = _account(value.get("account"))
    if account is not None and owner != _account(account):
        raise ValueError("These label names belong to a different Gmail account.")
    names, aliases, custom = value.get("names"), value.get("aliases"), value.get("custom_ids")
    if (not isinstance(names, dict) or not isinstance(aliases, dict)
            or not isinstance(custom, list) or len(custom) > MAX_CUSTOM_LABELS
            or any(not isinstance(key, str) or not TAG_ID.fullmatch(key) for key in custom)
            or len(set(custom)) != len(custom)
            or set(names) != set(DEFAULT_NAMES) | set(custom) or set(aliases) != set(names)):
        raise ValueError("The saved label names could not be read.")
    used, clean_names, clean_aliases = {}, {}, {}
    for key, name in names.items():
        clean_names[key] = validate_name(name)
        previous = aliases[key]
        if not isinstance(previous, list) or len(previous) > 100:
            raise ValueError("The saved label history could not be read.")
        clean_aliases[key] = list(dict.fromkeys(validate_name(item) for item in previous))
        for item in [clean_names[key], *clean_aliases[key]]:
            folded = item.casefold()
            if folded in used and used[folded] != key:
                raise ValueError("Label names must be unique, including previously used names.")
            used[folded] = key
    result = {"account": owner, "names": clean_names, "aliases": clean_aliases, "custom_ids": list(custom)}
    if value.get("version") != _version(result):
        raise ValueError("The saved label names changed. Reload them before continuing.")
    return result | {"version": _version(result)}


def gmail_label_names(catalog):
    """Current logical names and the exact old names known to this local catalog."""
    value = validate_catalog(catalog)
    current = {key: "StudentFlow/" + name for key, name in value["names"].items()}
    allowed = set(current.values())
    for aliases in value["aliases"].values():
        allowed.update("StudentFlow/" + name for name in aliases)
    return current, allowed


class LabelCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS email_label_catalog "
                       "(account TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    @staticmethod
    def _read(db, account):
        row = db.execute("SELECT payload FROM email_label_catalog WHERE account=?", (account,)).fetchone()
        if row is None:
            return default_catalog(account)
        try:
            return validate_catalog(json.loads(row[0]), account=account)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("Saved labels could not be read. Existing settings have been kept.") from None

    def get(self, account):
        account = _account(account)
        with closing(sqlite3.connect(self.path, timeout=15)) as db:
            return self._read(db, account)

    def _change(self, account, name, key, expected_version):
        account, name = _account(account), validate_name(name)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = self._read(db, account)
            if expected_version != current["version"]:
                raise ValueError("Label settings changed in another window. Reload and try again.")
            value = deepcopy(current)
            if key is None:
                if len(value["custom_ids"]) >= MAX_CUSTOM_LABELS:
                    raise ValueError("You can keep up to 50 custom labels.")
                key = "tag_" + uuid4().hex
                value["custom_ids"].append(key)
                value["aliases"][key] = []
            elif not isinstance(key, str) or key not in value["names"]:
                raise ValueError("Choose an existing label to rename.")
            old = value["names"].get(key)
            if old == name:
                return current
            prior_names = ([old] if old else []) + value["aliases"][key]
            if any(item != name and item.casefold() == name.casefold() for item in prior_names):
                raise ValueError("Choose a name that differs by more than capitalization.")
            for other, other_name in value["names"].items():
                if other != key and name.casefold() in {
                    item.casefold() for item in [other_name, *value["aliases"][other]]
                }:
                    raise ValueError("That label name is already used. Choose another name.")
            value["names"][key] = name
            history = value["aliases"][key]
            if old and old not in history:
                history.append(old)
            value["aliases"][key] = [item for item in history if item != name]
            value["version"] = _version(value)
            value = validate_catalog(value, account=account)
            db.execute("INSERT OR REPLACE INTO email_label_catalog VALUES (?, ?)",
                       (account, json.dumps(value, ensure_ascii=False)))
        return value

    def add(self, account, name, *, expected_version):
        return self._change(account, name, None, expected_version)

    def rename(self, account, key, name, *, expected_version):
        return self._change(account, name, key, expected_version)
