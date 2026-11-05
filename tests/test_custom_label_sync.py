"""Custom Gmail label migration is previewed, scoped and verified without networking."""

from copy import deepcopy

import pytest
from test_gmail_labels import ACCOUNT, Provider, Response, Session, items

from studentflow import gmail_labels as labels
from studentflow import triage_sync as sync
from studentflow.email_label_catalog import LabelCatalog, default_catalog, gmail_label_names


class NamedProvider(Provider):
    def configure_catalog(self, catalog):
        self.names, self.allowed = gmail_label_names(catalog)

    def managed_label_ids(self):
        self.calls.append("labels")
        return {key: value for key, value in self.mapping.items() if key in self.allowed}

    def create_named_label(self, name):
        assert name in self.names.values()
        self.writes.append(("create", name))
        self.mapping[name] = "Label_" + str(len(self.mapping))
        return self.mapping[name]

    def set_named_labels(self, identifier, desired, mapping):
        self.writes.append((identifier, desired))
        if identifier != self.fail_message or self.apply_before_failure:
            self.messages[identifier].difference_update(mapping.values())
            self.messages[identifier].update(mapping[name] for name in desired)
        if identifier == self.fail_message:
            raise labels.GmailLabelError("Gmail did not confirm this label operation.")


def custom_catalog(tmp_path):
    store = LabelCatalog(tmp_path / "catalog.sqlite3")
    first = store.get(ACCOUNT)
    renamed = store.rename(ACCOUNT, "needs_reply", "回复", expected_version=first["version"])
    catalog = store.add(ACCOUNT, "Project", expected_version=renamed["version"])
    return store, catalog, catalog["custom_ids"][0]


def prepare(provider, selection, catalog):
    return sync.prepare_label_sync(provider, account=ACCOUNT, items=selection, catalog=catalog)


def apply(provider, preview, selection, catalog, tmp_path, **kwargs):
    options = {"account": ACCOUNT, "items": selection, "catalog": catalog,
               "data_dir": tmp_path, "confirmed": True}
    options.update(kwargs)
    return sync.apply_label_sync(provider, preview, **options)


def test_rename_and_custom_tag_only_migrate_selected_messages(tmp_path):
    _, catalog, tag = custom_catalog(tmp_path)
    provider = NamedProvider(mapping={"StudentFlow/Needs reply": "Label_old",
                                      "StudentFlow/Unknown": "Label_unowned"})
    provider.messages["m1"].update({"Label_old", "Label_unowned"})
    provider.messages["m2"].add("Label_old")
    untouched = set(provider.messages["m2"])
    selection = [items()[0] | {"tag_ids": [tag]}]
    preview = prepare(provider, selection, catalog)
    assert provider.writes == []
    assert preview.items[0].remove_labels == ("StudentFlow/Needs reply",)
    assert preview.items[0].add_labels == ("StudentFlow/Project", "StudentFlow/回复")
    assert set(preview.missing_labels) == set(preview.items[0].add_labels)
    result = apply(provider, preview, selection, catalog, tmp_path)
    assert result["status"] == "synced"
    assert provider.messages["m2"] == untouched
    assert "Label_old" not in provider.messages["m1"]
    assert {"INBOX", "UNREAD", "Label_personal", "Label_unowned"} <= provider.messages["m1"]
    assert provider.mapping["StudentFlow/Project"] in provider.messages["m1"]
    assert provider.mapping["StudentFlow/回复"] in provider.messages["m1"]
    original_writes = deepcopy(provider.writes)
    assert apply(provider, preview, selection, catalog, tmp_path)["unchanged_count"] == 1
    assert provider.writes == original_writes
    selection[0]["tag_ids"] = []
    second = prepare(provider, selection, catalog)
    assert second.items[0].remove_labels == ("StudentFlow/Project",)
    assert apply(provider, second, selection, catalog, tmp_path)["status"] == "synced"
    assert provider.mapping["StudentFlow/Project"] not in provider.messages["m1"]


@pytest.mark.parametrize("change", ["catalog", "tags", "account", "missing_catalog", "confirmation"])
def test_stale_name_tag_account_or_unconfirmed_preview_never_calls_remote(tmp_path, change):
    store, catalog, tag = custom_catalog(tmp_path)
    provider = NamedProvider()
    selection = [items()[0] | {"tag_ids": [tag]}]
    preview = prepare(provider, selection, catalog)
    provider.calls.clear()
    overrides = {}
    if change == "catalog":
        catalog = store.rename(ACCOUNT, tag, "Course", expected_version=catalog["version"])
    elif change == "tags":
        selection[0]["tag_ids"] = []
    elif change == "account":
        overrides["account"] = "other@example.test"
    elif change == "missing_catalog":
        catalog = None
    else:
        overrides["confirmed"] = False
    with pytest.raises(labels.GmailLabelError):
        apply(provider, preview, selection, catalog, tmp_path, **overrides)
    assert provider.calls == [] and provider.writes == []


@pytest.mark.parametrize("tags", [["tag_" + "a" * 32], ["ignore"], "Project", [None]])
def test_unknown_or_malformed_custom_tags_fail_before_network(tmp_path, tags):
    _, catalog, _ = custom_catalog(tmp_path)
    provider = NamedProvider()
    with pytest.raises(labels.GmailLabelError, match="custom labels changed"):
        prepare(provider, [items()[0] | {"tag_ids": tags}], catalog)
    assert provider.calls == [] and provider.writes == []


def test_no_catalog_cannot_silently_drop_custom_tags():
    provider = Provider()
    with pytest.raises(labels.GmailLabelError, match="saved label settings"):
        sync.prepare_label_sync(provider, account=ACCOUNT, items=[items()[0] | {"tag_ids": ["tag_x"]}])
    assert provider.calls == []


def test_timeout_after_custom_sync_success_retries_without_duplicate_write(tmp_path):
    _, catalog, tag = custom_catalog(tmp_path)
    provider = NamedProvider()
    selection = [item | {"tag_ids": [tag]} for item in items()]
    preview = prepare(provider, selection, catalog)
    provider.fail_message, provider.apply_before_failure = "m2", True
    assert apply(provider, preview, selection, catalog, tmp_path)["status"] == "partial"
    provider.fail_message = None
    before = list(provider.writes)
    assert apply(provider, preview, selection, catalog, tmp_path)["status"] == "synced"
    assert provider.writes == before


def test_multiple_tags_and_custom_rename_preserve_other_selected_tag(tmp_path):
    store, catalog, first_tag = custom_catalog(tmp_path)
    catalog = store.add(ACCOUNT, "Review", expected_version=catalog["version"])
    second_tag = catalog["custom_ids"][-1]
    provider = NamedProvider()
    selection = [items()[0] | {"tag_ids": [first_tag, second_tag]}]
    preview = prepare(provider, selection, catalog)
    assert apply(provider, preview, selection, catalog, tmp_path)["status"] == "synced"
    old_id = provider.mapping["StudentFlow/Project"]
    provider.messages["m2"].add(old_id)
    catalog = store.rename(ACCOUNT, first_tag, "Research", expected_version=catalog["version"])
    preview = prepare(provider, selection, catalog)
    assert preview.items[0].add_labels == ("StudentFlow/Research",)
    assert preview.items[0].remove_labels == ("StudentFlow/Project",)
    assert apply(provider, preview, selection, catalog, tmp_path)["status"] == "synced"
    assert old_id in provider.messages["m2"] and old_id not in provider.messages["m1"]
    assert provider.mapping["StudentFlow/Review"] in provider.messages["m1"]
    assert provider.mapping["StudentFlow/Research"] in provider.messages["m1"]


def test_external_label_change_blocks_all_selected_writes(tmp_path):
    _, catalog, tag = custom_catalog(tmp_path)
    provider = NamedProvider(mapping={"StudentFlow/Needs action": "Label_action"})
    selection = [item | {"tag_ids": [tag]} for item in items()]
    preview = prepare(provider, selection, catalog)
    provider.messages["m2"].add("Label_action")
    with pytest.raises(labels.GmailLabelError, match="labels changed"):
        apply(provider, preview, selection, catalog, tmp_path)
    assert provider.writes == []


def test_http_custom_sync_uses_only_catalog_names_and_aliases(tmp_path):
    _, catalog, tag = custom_catalog(tmp_path)
    current, _ = gmail_label_names(catalog)
    rows = [{"id": "Label_reply", "name": current["needs_reply"], "type": "user"},
            {"id": "Label_old", "name": "StudentFlow/Needs reply", "type": "user"},
            {"id": "Label_tag", "name": current[tag], "type": "user"},
            {"id": "Label_unowned", "name": "StudentFlow/Not in catalog", "type": "user"},
            {"id": "INBOX", "name": "INBOX", "type": "system"}]
    session = Session([Response({"labels": rows}), Response(status=204, raw=b"")])
    client = labels.GmailLabelClient(session)
    client.configure_catalog(catalog)
    mapping = client.managed_label_ids()
    client.set_named_labels("m1", [current["needs_reply"], current[tag]], mapping)
    assert session.calls[-1][2]["json"] == {
        "ids": ["m1"], "addLabelIds": ["Label_tag", "Label_reply"], "removeLabelIds": ["Label_old"],
    }


def test_http_custom_ids_cannot_be_injected_and_aliases_cannot_be_created(tmp_path):
    _, catalog, _ = custom_catalog(tmp_path)
    client = labels.GmailLabelClient(Session([]))
    client.configure_catalog(catalog)
    for name in ["StudentFlow/Needs reply", "INBOX", "StudentFlow/Unknown"]:
        with pytest.raises(labels.GmailLabelError):
            client.create_named_label(name)
    with pytest.raises(labels.GmailLabelError, match="verified"):
        client.set_named_labels("m1", ["StudentFlow/回复"], {"StudentFlow/回复": "INBOX"})
    assert client._session.calls == []


@pytest.mark.parametrize("rows", [
    [{"id": "INBOX", "name": "StudentFlow/Needs reply", "type": "system"}],
    [{"id": "Label_one", "name": "StudentFlow/Needs reply", "type": "user"}] * 2,
])
def test_http_ambiguous_or_system_catalog_names_rejected(rows):
    client = labels.GmailLabelClient(Session([Response({"labels": rows})]))
    client.configure_catalog(default_catalog(ACCOUNT))
    with pytest.raises(labels.GmailLabelError, match="ambiguous"):
        client.managed_label_ids()
