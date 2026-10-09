"""Streamlit Sprint 1 workbench. Run from this project using start.ps1."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import dotenv_values

from studentflow.attachments import extract_attachment
from studentflow.ingestion import parse_eml_bytes
from studentflow.planner import propose_schedule, validate_schedule
from studentflow.retrieval import build_chunks, search_chunks
from studentflow.storage import Store

ROOT = Path(__file__).resolve().parent
PACK = ROOT.parent / "group work" / "StudentFlow_B_Email_Pack" / "StudentFlow_B_Email_Pack"
ZONE = ZoneInfo("Asia/Singapore")


def load_config(name):
    return json.loads((ROOT / "config" / name).read_text(encoding="utf-8"))


def import_message(raw, name):
    mail = parse_eml_bytes(raw, name)
    mail["imported_at"] = datetime.now(ZONE).isoformat()
    existed = mail["id"] in st.session_state.mails
    st.session_state.mails[mail["id"]] = mail
    return existed


def show_warnings(warnings):
    for warning in dict.fromkeys(warnings):
        st.warning(warning)


def main():
    st.set_page_config(page_title="StudentFlow · Sprint 1", layout="wide")
    st.title("StudentFlow")
    st.caption("Sprint 1 本地开发原型 · 邮件与确认记录保存在本机")
    st.info("可导入 EML，或先授权 Gmail 再读取真实邮件。AI 提取需要单独配置模型 API，并由你选择是否发送邮件正文给模型。")
    st.session_state.setdefault("mails", {})
    st.session_state.setdefault("candidates", {})
    data_dir = Path(os.environ.get("STUDENTFLOW_DATA_DIR", str(ROOT / "data")))
    store = Store(data_dir / "studentflow.sqlite3")
    config = dotenv_values(ROOT / ".env")
    api_key = config.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    model = config.get("OPENAI_MODEL") or os.environ.get("OPENAI_MODEL", "")
    st.sidebar.write("模型配置：" + ("已填写，尚需实际调用验证" if api_key and model else "未配置，使用本地功能"))
    st.sidebar.caption("LangChain 负责调用模型；Streamlit 是当前操作页面。")
    tabs = st.tabs(["1 导入邮件与确认任务", "2 已保存任务", "3 学习计划", "4 附件原文检索", "真实邮箱（Gmail）"])

    with tabs[0]:
        files = []
        # Only these two input directories are discoverable. Never scan answers,
        # manifest, docs, or the mixed input/answer workbook.
        for folder in (PACK / "inputs" / "eml_50", PACK / "inputs" / "attachment_demos"):
            files.extend(sorted(folder.glob("*.eml")))
        selected = st.selectbox("选择同学提供的模拟邮件", files, format_func=lambda p: p.name, index=None)
        if st.button("导入选中的模拟邮件", disabled=selected is None):
            try:
                existed = import_message(selected.read_bytes(), selected.name)
                st.success("该邮件已导入，未重复添加。" if existed else "导入成功。")
            except (ValueError, OSError) as exc:
                st.error(str(exc))
        uploads = st.file_uploader("也可以选择 EML 文件", type=["eml"], accept_multiple_files=True)
        if st.button("导入上传的 EML", disabled=not uploads):
            for uploaded in uploads:
                try:
                    import_message(uploaded.getvalue(), uploaded.name)
                except ValueError as exc:
                    st.error(f"{uploaded.name}：{exc}")
            st.success("已处理所选文件；相同内容的邮件只保留一份。")
        mails = st.session_state.mails
        if mails:
            mid = st.selectbox("查看已导入邮件", list(mails), format_func=lambda key: f"{mails[key]['source_name']} · {mails[key]['subject']}")
            mail = mails[mid]
            st.write(f"发件人：{mail['sender']}　邮件时间：{mail['sent_at'] or '缺失，需核对'}")
            st.text_area("邮件正文", mail["body"], height=200, disabled=True, key="body_" + mid)
            show_warnings(mail["warnings"])
            st.caption(f"实际附件数：{len(mail['attachments'])}。正文提及附件不等于文件已附上。")
            allow_model = st.checkbox("同意将当前邮件的标题、正文和发送时间发给所配置的 OpenAI 模型（可能含个人信息并产生 API 费用）", key="allow_model_" + mid)
            if st.button("用 LangChain 提取任务", disabled=not (api_key and model and allow_model)):
                try:
                    from studentflow.extraction import extract_tasks

                    with st.spinner("正在提取，请核对返回结果…"):
                        result = extract_tasks(mail["body"], mail["subject"], mail["sent_at"], api_key, model)
                    st.session_state.candidates[mid] = result
                except Exception as exc:
                    # Provider error strings can contain sensitive request details.
                    st.error(f"模型调用未完成（{type(exc).__name__}）。请检查模型配置、额度或依赖环境；没有保存任务。")
            candidates = st.session_state.candidates.get(mid)
            if candidates:
                show_warnings(candidates.get("warnings", []))
                for index, candidate in enumerate(candidates.get("tasks", []), 1):
                    with st.expander(f"AI 草稿 {index}：{candidate['title']}", expanded=True):
                        st.json(candidate)
                st.caption("AI 返回值仍是草稿。请把需要保留的任务填入下方，核对后确认。")
            with st.form("confirm_" + mid):
                st.write("人工核对并保存任务")
                title = st.text_input("任务名称")
                due_text = st.text_input("截止时间（新加坡时间；不明确可留空）", placeholder="2026-10-15 23:59")
                minutes = st.number_input("预计需要多少分钟（由你估计；0 表示尚未确认）", min_value=0, max_value=10000, value=0, step=15)
                quote = st.text_area("原文依据（从上方邮件正文复制一段）", height=80)
                reviewed = st.checkbox("我已核对原文、截止时间和预计耗时")
                submitted = st.form_submit_button("确认并保存到本机")
                if submitted:
                    try:
                        if not reviewed or not title.strip():
                            raise ValueError("请填写任务名称，并勾选核对确认。")
                        if not quote.strip() or quote.strip() not in mail["body"]:
                            raise ValueError("原文依据必须是当前邮件正文中的一段连续文字。")
                        due = datetime.strptime(due_text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=ZONE).isoformat() if due_text.strip() else None
                        complete = bool(due and minutes)
                        store.save_task({"mail_id": mid, "source_name": mail["source_name"], "title": title.strip(), "due_at": due, "estimated_minutes": int(minutes) or None, "status": "confirmed" if complete else "pending", "source_quote": quote.strip(), "mail_subject": mail["subject"], "mail_sent_at": mail["sent_at"], "mail_imported_at": mail.get("imported_at")})
                        st.success("已保存。" if complete else "已保存为待补充任务；补齐截止时间和耗时之前不会参与排程。")
                    except ValueError as exc:
                        st.error(str(exc))

    with tabs[1]:
        tasks = store.tasks()
        st.write("再次提交同一邮件、同一任务名称会更新原记录。不同邮件中的相同任务尚需人工合并。")
        if tasks:
            st.dataframe([{ "任务": t["title"], "截止": t["due_at"], "分钟": t["estimated_minutes"], "状态": "已确认" if t["status"] == "confirmed" else "待补充", "来源": t["source_name"], "原文依据": t["source_quote"]} for t in tasks], hide_index=True)
        else:
            st.write("暂未保存任务。先在第一个页面导入邮件并确认。")

    with tabs[2]:
        scenarios = load_config("scenarios.json")
        scenario_key = st.radio("可用时段", list(scenarios), format_func=lambda key: scenarios[key]["label"], horizontal=True)
        scenario = scenarios[scenario_key]
        settings = load_config("planning_rules.json")
        task_source = st.radio("本次排程使用哪些任务", ["我的已保存任务", "同学提供的场景样例任务"], horizontal=True)
        sample_reviewed = False
        if task_source == "同学提供的场景样例任务":
            plan_tasks = copy.deepcopy(scenario["tasks"])
            st.dataframe(plan_tasks, hide_index=True)
            sample_reviewed = st.checkbox("我已核对上述样例输入，确认用于本次模拟排程", key=f"sample_reviewed_{scenario_key}")
            if sample_reviewed:
                for task in plan_tasks:
                    task["status"] = "confirmed"
        else:
            plan_tasks = tasks
            # Real saved tasks must never be planned in past fixture slots.
            # Minute precision avoids invalidating an unchanged plan on reruns.
            settings["now"] = datetime.now(ZONE).replace(second=0, microsecond=0).isoformat()
        with st.expander("查看空闲时段、课程和规则假设"):
            st.dataframe(scenario["available_slots"], hide_index=True)
            st.dataframe(scenario["busy_events"], hide_index=True)
            show_warnings(scenario["warnings"] + settings["assumptions"])
        st.caption("学习范围 08:00–22:00；保留三餐；课外学习每天最多 6 小时；未知路线按单程 40 分钟预留。仅使用已列出的空闲时段。")
        busy = copy.deepcopy(scenario["busy_events"])
        add_busy = st.checkbox("模拟临时会议，重新规划学习时间")
        if add_busy:
            new_day = st.date_input("会议日期", value=date.fromisoformat(scenario["start_date"]))
            start_time = st.time_input("会议开始", value=time(13, 0))
            end_time = st.time_input("会议结束", value=time(14, 0))
            busy.append({"title": "临时线上会议", "start": datetime.combine(new_day, start_time, ZONE).isoformat(), "end": datetime.combine(new_day, end_time, ZONE).isoformat(), "mode": "online"})
        inputs = {"tasks": plan_tasks, "slots": scenario["available_slots"], "busy": busy, "settings": settings}
        signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        if st.button("生成学习计划草稿", disabled=not plan_tasks or (task_source != "我的已保存任务" and not sample_reviewed)):
            plan = propose_schedule(plan_tasks, scenario["available_slots"], busy, settings)
            errors = validate_schedule(plan["blocks"], plan_tasks, scenario["available_slots"], busy, settings)
            st.session_state.plan = {"signature": signature, "result": plan, "validation_errors": errors, "inputs": inputs, "scenario": scenario_key, "task_source": task_source}
        saved_plan = st.session_state.get("plan")
        if saved_plan and saved_plan["signature"] == signature:
            result = saved_plan["result"]
            show_warnings(result["warnings"] + saved_plan["validation_errors"])
            if result["blocks"]:
                st.dataframe(result["blocks"], hide_index=True)
            else:
                st.write("本次没有生成学习时段。")
            if result["unscheduled"]:
                st.warning("以下任务仍有时间未安排，不能视为全部完成：")
                st.dataframe(result["unscheduled"], hide_index=True)
            confirm_plan = st.checkbox("我已核对学习计划和提醒，保存这一版到本机", key="confirm_plan_" + signature)
            if st.button("保存计划", disabled=not confirm_plan or bool(saved_plan["validation_errors"]) or not result["blocks"]):
                store.save_plan(saved_plan)
                st.success("计划已保存在本机。")
            st.download_button("下载本次计划与校验结果", json.dumps(saved_plan, ensure_ascii=False, indent=2), "studentflow_plan.json", "application/json")
        elif saved_plan:
            st.info("排程输入已改变，请重新生成草稿。")

    with tabs[3]:
        st.write("先导入一封带附件的邮件；本地材料可选 AX01（PDF）、AX02（Word）或 AX03（图片）。")
        st.caption("当前提供附件原文检索，尚未实现完整的 RAG 回答。图片需要后续接入文字识别。")
        if st.button("读取当前已导入邮件的附件"):
            documents = []
            for mail in st.session_state.mails.values():
                for attachment in mail["attachments"]:
                    document = extract_attachment(attachment)
                    document["mail_id"] = mail["id"]
                    document["source_name"] = mail["source_name"]
                    documents.append(document)
            st.session_state.documents = documents
        documents = st.session_state.get("documents", [])
        for document in documents:
            with st.expander(f"{document['source_name']} / {document['filename']} · {document['status']}"):
                show_warnings(document["warnings"])
                st.text(document["text"])
        with st.form("search"):
            question = st.text_input("输入关键词查找依据", placeholder="使用附件原文的语言，例如 submission 或 format")
            search = st.form_submit_button("查找附件原文")
        if search:
            hits = search_chunks(question, build_chunks(documents))
            if not hits:
                st.info("没有找到关键词匹配的原文；当前不会猜测答案。")
            for hit in hits:
                source = st.session_state.mails[hit["mail_id"]]["source_name"]
                st.write(f"{source} / {hit['filename']} / " + (f"第 {hit['page']} 页" if hit["page"] else "无可靠页码"))
                st.text(hit["text"])

    with tabs[4]:
        st.write("连接你自己的 Gmail，只读取收件箱及选中邮件的正文、附件。")
        st.caption("不会发送邮件、删除邮件或修改已读状态。连接邮箱不调用 GPT，也不会自动把邮件发给模型。")
        from studentflow.gmail import GmailError, connect

        credential_path = Path(os.environ.get("STUDENTFLOW_GMAIL_CLIENT", str(ROOT / "credentials" / "gmail_client.json")))
        token_path = data_dir / "auth" / "gmail_token.json"
        st.write("首次使用：按项目 docs 文件夹内《Gmail接入操作指南》配置 Google 登录，并把下载的文件保存为 credentials/gmail_client.json。")
        st.write("登录配置文件：" + ("已找到" if credential_path.is_file() else "尚未找到"))
        consent = st.checkbox("我同意 StudentFlow 通过 Google 授权读取我选择的邮箱", key="gmail_consent")
        if st.button("登录并授权 Gmail", disabled=not credential_path.is_file() or not consent):
            # Clear old account-scoped UI state before switching accounts.
            st.session_state.pop("gmail_profile", None)
            st.session_state.pop("gmail_headers", None)
            try:
                with st.spinner("请在浏览器完成 Google 登录和授权，然后回到此页面…"):
                    with closing(connect(credential_path, token_path, interactive=True)) as mailbox:
                        st.session_state.gmail_profile = mailbox.profile()
                st.success("Google 授权及邮箱身份检查已通过，可以读取列表。")
            except GmailError as exc:
                st.error(str(exc))
        profile = st.session_state.get("gmail_profile")
        if profile:
            st.write("当前邮箱：" + profile.get("email_address", "已授权"))
        count = st.selectbox("每次最多列出多少封收件箱邮件", [5, 10, 20])
        if st.button("读取收件箱列表", disabled=not consent or not token_path.is_file()):
            st.session_state.pop("gmail_headers", None)
            try:
                with st.spinner("正在只读查询收件箱…"):
                    with closing(connect(credential_path, token_path)) as mailbox:
                        profile = mailbox.profile()
                        st.session_state.gmail_profile = profile
                        st.session_state.gmail_headers = mailbox.list_messages(count)
                if not st.session_state.gmail_headers:
                    st.info("本次未找到收件箱邮件。")
            except GmailError as exc:
                st.error(str(exc))
        headers = st.session_state.get("gmail_headers", [])
        if headers:
            choice = st.selectbox("选择一封邮件导入", headers, format_func=lambda item: f"{item['subject']} · {item['sender']} · {item['sent_at']}")
            st.caption("列表只查询邮件头。点击导入后才读取选中邮件的完整正文与附件；内容在当前页面会话中保留。")
            if st.button("导入选中的真实邮件", disabled=not consent):
                try:
                    with st.spinner("正在读取选中邮件及附件…"):
                        with closing(connect(credential_path, token_path)) as mailbox:
                            actual_profile = mailbox.profile()
                            if actual_profile != st.session_state.get("gmail_profile"):
                                raise GmailError("登录账号已改变，请重新读取收件箱列表后选择邮件。")
                            raw = mailbox.fetch_eml(choice["id"])
                            duplicate = import_message(raw, "Gmail / " + choice["id"])
                    st.success("该邮件已导入，未重复添加。" if duplicate else "已导入。到第一个页面查看正文和确认任务，到附件页面查看实际附件。")
                except (GmailError, ValueError) as exc:
                    st.error(str(exc))
        if token_path.is_file():
            if st.button("退出本机 Gmail 授权"):
                token_path.unlink(missing_ok=True)
                for key in ("gmail_profile", "gmail_headers", "mails", "candidates", "documents", "plan"):
                    st.session_state.pop(key, None)
                st.session_state.gmail_signed_out = True
                st.rerun()
        if st.session_state.pop("gmail_signed_out", False):
            st.success("已删除本机登录缓存并清空当前页面邮件。已确认保存的任务仍在本机。")
            st.caption("如需撤销 Google 端对 StudentFlow 的许可，请在 Google 账号的第三方连接中移除它。")


if __name__ == "__main__":
    main()
