# StudentFlow 功能说明书

DSS5105 Track 4 · Sprint 3 本地界面版 · 2026-11-07

StudentFlow 是一个在本机运行的学生工作台。它把课程邮件、附件和讨论整理成可核对的任务、固定事项、学习计划及回复草稿。界面为英文，本说明用中文解释操作，保留英文按钮名称以便查找。日历和截止时间按新加坡时间显示。

## 1. 安装与进入工作台

Mac 安装 uv 后，在包含 `app.py` 的目录打开终端，执行：

```bash
uv sync --locked
uv run --locked streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
```

在浏览器打开 `http://127.0.0.1:8501/`，使用期间保留终端进程。Python 要求 3.12.6 及以上的 3.12，或 3.13。Windows 启动和桌面快捷方式见 [README](../README.md)。

- 首次进入会看到欢迎动画。已有 Gmail 连接时点 **Enter StudentFlow** 进入 Overview，也可点 **Skip intro** 直接体验本地工作台。
- 要读 Gmail，按 [Gmail 设置指南](GMAIL_SETUP.md) 放置自己的桌面 OAuth 客户端文件 `credentials/gmail_client.json`，在欢迎页点 **Continue to Gmail** 或从左侧 **Gmail connection** 连接并完成 Google 授权。若 Google 项目仍在测试模式，登录账号须列为测试用户。
- 要使用 AI 分类、提取、回复或总结，在 **Settings → AI settings** 保存自己的 API key 和 Model ID，再到 **Connection check** 测试。API key、可用额度和模型访问权限分别影响测试结果。Google 授权与 AI 配置各自独立。
- 已保存的任务、日历浏览和本地排程可以在不调用 AI 的情况下使用。新安装的工作台会从自己的本地数据开始。

## 2. Overview · 总览、日历和待办

顶部三项统计展示未完成任务、已知剩余学习时间和待补充信息的任务。剩余时间只累计已有估时的任务。

- **Your calendar** 显示未完成任务的截止日期、已保存的学习时段、固定事项和手工承诺。用左右键或拖动活页切月，点 **Today** 回到今天。点日期查看右侧日计划，下面的七列展示选中日期所在周（周一到周日）。
- 点击日计划或周计划中任务截止事项左侧的色条，可以选 **Blue / Pink / Purple / Orange / Green**。颜色按任务保存，两个视图使用同一设置。色条用来区分事项。
- **To do** 默认按较早截止时间优先显示未完成任务。可用 **All open / Due today / Needs details** 筛选。点任务名称进入编辑，点左边复选框标记完成。完成项划线并排到末尾，过截止时间后从 Overview 待办隐藏，仍可在 **Tasks → Completed** 查阅。无截止日期的完成项不会按日期自动隐藏。
- **Pick up your work** 下有 Inbox、Study Plan 和 Discussions 快捷入口。**Daily progress** 用于查看和填写到期的学习进度。

## 3. Inbox · 读邮件、提取任务和分类

左侧 **Messages** 负责列表，右侧 **Email workspace** 负责读取和处理。点 **Refresh inbox** 获取邮件，每次加载一批，可用 **Load more** 继续。按主题或发件人搜索，也能按标签筛选。**Select all shown** 选中当前显示的邮件，**Clear selection** 清空选择。

| 模式 | 操作及结果 |
| --- | --- |
| **Read email** | 点击主题或把邮件拖到阅读区，打开邮件正文。用 **View selected messages** 阅读选中的邮件，用 **Reply to this email** 进入 Assistant 起草回复。 |
| **Tasks & events** | 勾选邮件并同意处理范围，点 **Analyze selected messages**。结果将可安排的学习任务和时间已确定的固定事项分开。检查来源原文、截止时间、估时及重复项，只保存选中的结果。可读附件也可提供证据。 |
| **Email categories** | 每批最多分类 30 封。AI 可建议 **Needs reply / Needs action / No action needed**，并提供依据。可修改分类，先保存到本机，再选择是否同步 Gmail。 |

如需手动保存任务，可在邮件阅读页展开 **Add a task manually**，从邮件或附件选证据，填写标题、截止时间、估时和对应原文。确认任务需要有效截止时间和大于 0 的预计分钟数，信息不全的条目可先存为 **Needs details**。

用 **Manage labels** 重命名分类或添加自定义标签。写入 Gmail 要另外连接标签权限，先点 **Preview Gmail label changes** 核对目标邮件和变更，再确认 **Apply Gmail labels**。只改已选邮件对应的 StudentFlow 标签。

## 4. Tasks · 搜索、编辑、完成和导出

1. 在 **Your task list** 切换 **Unfinished / Completed**，用关键词和 **Task status** 筛选。
2. 在 **Edit task** 顶部下拉框选择任务，或把左侧任务卡拖入虚线区。选中后可查看 **Original supporting text**、已完成与剩余时间。
3. 编辑名称、截止时间、预计和已完成的分钟数。核对后点 **Save changes**，再检查任务状态。
4. 删除任务时，按 **Delete task** 的确认流程进入揉纸界面，将纸团拖入垃圾桶提交删除，或点 **Keep task** 保留。删除会从任务和相关计划中移除该任务，已保存的附件和已记录的学习时间保留。
5. **Full task table** 展开表格，**Download tasks** 导出 CSV。

## 5. Study plan · 可用时间、排程和日历同步

页面显示可安排任务数、剩余学习时间、已保存时段和固定事项。文件夹展开后有三张可点击的纸，分别打开以下功能。刚进入页面及仅打开文件夹时可看到 **Daily progress**。选中一张纸后查看分支内容，关闭文件夹可回到进度区。

- **Schedule**：点 **Generate study plan** 生成草案，检查按天列出的任务、安排时段和未排入的工作，确认后点 **Save plan**。已完成部分工作后可点 **Replan remaining tasks**。排程根据已确认任务的截止时间、剩余分钟、可用时段和冲突约束计算。
- **Availability & preferences**：设置每天的学习起止、每日上限、休息间隔及规划范围。**Available study time** 下可添加每周重复的可用时段、为指定日期设例外，并查看即将到来的可用时间。
- **Commitments & calendar**：录入固定承诺、查看保存的固定事项。使用 **Google Calendar sync** 时先连接对应账号，再预览并确认变更。同步是从 StudentFlow 到专用日历的手动单向操作。
- **Daily progress**：当已保存的计划有到期复盘时，可记录全部完成或逐项填写实际分钟。保存后更新任务剩余时间，同一天的复盘只记一次。需要时再生成并保存调整后的计划。

## 6. Discussions 与 Assistant · 总结和草拟回复

- **Discussions** 可连接 Google Chat，选择讨论空间，点 **Read selected discussion**，勾选要纳入的消息或可选附件。检查右侧请求与授权后点 **Run assistant**。查看摘要、决定、行动事项、待确认问题、建议议程、跟进草稿和引用。支持导出 Word、TXT 或 Markdown。来源缺少负责人或日期时，结果可保留为未指定。
- **Assistant → Email replies** 可从 Inbox 带入邮件，也可在来源区选择已打开的邮件。可提供自己的写作样本并保存回复偏好。生成后编辑主题和正文，下载草稿，或另外授权 **Send with Gmail**，核对收件人并确认发送。
- **Assistant → Meetings & discussions** 提供讨论分析入口。**Other assistance** 可选择已保存任务、邮件、附件或相关计划信息，提出学习安排或后续行动问题。
- 有 **Context** 与 **Request** 卡片时，用箭头或按住鼠标横向拖动切换。先核对来源，再在 Request 检查授权范围和请求，最后运行并查看下方结果。

## 7. Attachments · 附件资料库

- **Your documents** 按文件名或来源邮件筛选。选中文件后，**Document preview** 显示文件名、来源及可读取的正文。
- **Search document text** 搜索资料库中的可读文字，并显示匹配片段。搜索范围包含资料库内可用文档，独立于左侧文件列表筛选。
- **Download original** 下载保留的原件。**Manage attachment** 中确认删除可从本机资料库移除文件和索引，已存任务及其支持引文仍可查看。没有可读文本的文件仍应查看原件。

## 8. Settings、数据与故障排查

- **AI settings** 保存 API key 和 Model ID。只改模型时可留空 key 输入框以保留原值。**Key saved** 表示本机已有配置。**Test AI connection** 会发送简短测试请求并使用 API 额度。**Setup & account** 提供创建 key、查看账单和设置指南的入口。
- **Gmail connection** 可切换账号、重连或断开。断开清除本机保存的对应登录，已存任务继续保留。读取 Gmail、写标签、发信和写日历有各自的授权与确认步骤。
- 登录凭据、AI 设置、附件、任务及计划由每台电脑分别保存。默认任务数据库位于 `data/studentflow.sqlite3`。组员从 GitHub 拉取代码后应自行授权并使用各自的数据。运行 AI 前，应核对即将提交的来源及附件范围。
- **127.0.0.1 拒绝连接**：在项目目录重新运行启动命令，看到终端输出本地地址后再打开浏览器。
- **Gmail 已连接但列表为空**：先刷新，清除关键词和标签筛选，检查邮件是否在 Gmail 收件箱、垃圾邮件或其他分类，必要时加载更多。
- **AI 测试失败**：检查 API key 是否有效、API 账户是否有可用额度、模型是否开放给该项目以及网络是否正常。

## 9. 组内演示与验收

建议用测试账号及虚构邮件跑一遍 **Inbox → Tasks & events → Tasks → Study plan → Overview**。检查保存后的任务重开仍存在，日期和剩余时间正确。再在周计划改一个任务色条，确认日计划同步，并反向改色核对。使用单独的测试任务验证勾选完成、拖拽编辑、下载和删除。发送邮件、写标签及日历同步只在测试数据上验证。

说明书依据本地源码和已有使用指南整理。Google、OpenAI 的实际连接、浏览器动效及布局以组员设备上的实测为准。
