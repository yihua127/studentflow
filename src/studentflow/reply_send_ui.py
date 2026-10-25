"""Explicit Gmail sending of the exact reply reviewed in the UI."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path

import streamlit as st

from studentflow.gmail import GmailError, connect
from studentflow.gmail_send import (
    TOKEN_FILENAME,
    GmailSendError,
    connect_send,
    prepare_reply,
    reply_send_status,
    send_reviewed_reply,
)
from studentflow.reply_validation import unresolved_placeholders


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def render_reply_send(root: Path, data_dir: Path, item: dict, source: dict,
                      subject: str, body: str, report_id: str) -> None:
    """Opening a draft never connects to Google or sends mail."""
    markers = unresolved_placeholders(subject + "\n" + body)
    if markers:
        st.warning("Replace the remaining template fields before sending: " + ", ".join(markers))
    if source.get("provider") != "gmail" or not source.get("gmail_id") or not source.get("account"):
        st.caption("To send a reply here, open its original message from the connected Gmail inbox.")
        return
    account = str(source["account"]).strip().casefold()
    active = str(st.session_state.get("gmail_profile", {}).get("email_address") or "").strip().casefold()
    if active != account:
        st.info("Reconnect the original Gmail inbox account before sending this reply.")
        return
    root, data_dir = Path(root), Path(data_dir)
    prefix = "sf_reply_send_" + _fingerprint([account, report_id, item["id"]])[:20]
    preview_key = prefix + "_preview"
    client_path = Path(os.environ.get("STUDENTFLOW_GMAIL_CLIENT", str(root / "credentials" / "gmail_client.json")))
    read_token = data_dir / "auth" / "gmail_token.json"
    send_token = data_dir / "auth" / TOKEN_FILENAME
    with st.expander("Send with Gmail"):
        st.caption("Send this reply from " + account + ". The assistant never sends automatically.")
        recipient = st.text_input(
            "Reply recipient", value=item.get("recipient") or "", key=prefix + "_recipient",
            help="The address must match this email's verified Reply-To or From address.",
        )
        current = _fingerprint([account, source, recipient, subject, body])
        saved = st.session_state.get(preview_key)
        if saved and saved["content"] != current:
            st.session_state.pop(preview_key, None)
            saved = None
            st.caption("The reply changed. Review the updated message before sending.")

        if not client_path.is_file():
            st.info("Set up the Google desktop connection in Gmail settings first.")
            return
        if not send_token.is_file():
            st.caption("Connect sending once to grant Gmail permission to send the replies you approve.")
        connect_label = "Connect Gmail sending" if not send_token.is_file() else "Change sending connection"
        if st.button(connect_label, key=prefix + "_connect"):
            st.session_state.pop(preview_key, None)
            saved = None
            try:
                with st.spinner("Connecting Gmail sending..."), closing(connect_send(
                    client_path, send_token, interactive=True,
                )) as client:
                    if client.account().casefold() != account:
                        raise GmailSendError("Choose the same Gmail account as this inbox email.")
                st.success("Gmail sending is connected. Review the reply below.")
            except GmailError as error:
                st.error(str(error))

        if st.button("Review for sending", key=prefix + "_prepare",
                     disabled=bool(markers) or not recipient.strip() or not subject.strip() or not body.strip()):
            st.session_state.pop(preview_key, None)
            saved = None
            try:
                with st.spinner("Checking the recipient and conversation..."), closing(connect(
                    client_path, read_token,
                )) as mailbox:
                    preview = prepare_reply(
                        mailbox, source_account=account, source_message_id=source["gmail_id"],
                        recipient=recipient, subject=subject, body=body,
                    )
                saved = {"content": current, "preview": preview}
                st.session_state[preview_key] = saved
            except GmailError as error:
                st.error(str(error))
            except OSError:
                st.error("The local Gmail connection could not be read. Reconnect and try again.")
        if saved is None:
            return
        preview = saved["preview"]
        try:
            receipt = reply_send_status(data_dir, preview.fingerprint)
        except GmailSendError as error:
            st.error(str(error))
            return
        if receipt and receipt["status"] == "sent":
            st.success("Sent with Gmail. This reply has already been sent.")
            return
        if receipt and receipt["status"] in {"pending", "uncertain"}:
            st.warning("Delivery is not confirmed. Check Gmail Sent before taking any further action. This reply is blocked from being sent again here.")
            return
        if receipt and receipt["status"] == "rejected":
            st.info("Gmail rejected the previous attempt. Check the connection and review before retrying.")
        st.text("From: " + preview.sender)
        st.text("To: " + preview.recipient)
        st.text("Subject: " + preview.subject)
        st.text(preview.body)
        review_key = prefix + "_reviewed_" + current
        reviewed = st.checkbox("I have reviewed the recipient and this exact message.", key=review_key)
        ready = reviewed and not markers and send_token.is_file()
        if not send_token.is_file():
            st.caption("Connect Gmail sending above to enable sending.")
        if st.button("Send with Gmail", type="primary", key=prefix + "_send", disabled=not ready):
            # Repeat UI conditions at the event boundary; the backend verifies
            # current source metadata, both account identities and the journal.
            if not ready or saved["content"] != current:
                st.error("Review the current recipient and message before sending.")
                return
            try:
                with st.spinner("Sending your reviewed reply..."), closing(connect(
                    client_path, read_token,
                )) as mailbox, closing(connect_send(client_path, send_token)) as client:
                    send_reviewed_reply(client, mailbox, preview, data_dir=data_dir, confirmed=True)
                st.rerun()
            except GmailError as error:
                st.error(str(error))
            except OSError:
                st.error("The sending connection could not be read. Check Gmail Sent before retrying.")
