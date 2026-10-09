# StudentFlow：接入自己的 Gmail

10 月 16 日先用个人 Gmail 完成真实邮箱读取。NUS Outlook 目前卡在学校的应用注册或授权权限，需要由学校批准；更换为个人 Outlook 仍要配置开发应用。QQ 邮箱的 IMAP 路线可另行开发，本版先完成 Gmail。

**这里创建 Google Cloud 项目，是登记“哪个程序要读取邮箱”。StudentFlow 仍在自己的电脑运行，不需要把系统部署到云上。**读取邮箱与调用 AI 是两件事；完成以下步骤不需要模型 API Key。

1. **创建项目。**用自己的 Gmail 登录 [Google Cloud 控制台](https://console.cloud.google.com/)，点击顶部项目选择器 →“新建项目”，名称填 `StudentFlow`，创建后选中它。

2. **启用邮箱接口。**进入“API 和服务 → 库”，搜索 `Gmail API`，打开后点击“启用”。

3. **登记应用信息。**进入“Google Auth Platform → Branding（品牌信息）”，首次使用点击“开始使用”。应用名称填 `StudentFlow`；用户支持邮箱和联系邮箱均填自己的 Gmail。Audience（受众）选 `External（外部）`，按页面提示完成创建。个人 Gmail 不选 Internal。[官方配置说明](https://developers.google.com/workspace/guides/configure-oauth-consent)

4. **只允许自己测试。**进入 Audience，保持发布状态为 `Testing（测试）`。在“Test users（测试用户）”中添加刚才的 Gmail 并保存。此时不需要发布给所有用户。

5. **添加只读权限。**进入“Data Access（数据访问）→ 添加或移除范围”，添加且仅保留本功能需要的 `https://www.googleapis.com/auth/gmail.readonly`，保存。这允许读取邮件及设置，不允许发送、删除或修改邮件。[权限说明](https://developers.google.com/workspace/gmail/api/auth/scopes)

6. **下载应用配置。**进入“Clients（客户端）→ 创建客户端”，类型选 `Desktop app（桌面应用）`，名称可填 `StudentFlow Local`。创建后下载 JSON，重命名为 `gmail_client.json`，放到：

   ```text
   studentflow\credentials\gmail_client.json
   ```

   使用下载的原文件，不要把内容复制到聊天，也不要手工创建 API Key、服务账号或填写应用密钥。此文件和登录令牌不上传 GitHub。[桌面应用配置说明](https://developers.google.com/workspace/gmail/api/quickstart/python)

7. **在 StudentFlow 登录。**启动程序，打开 **Inbox (Gmail)**，展开 **Gmail connection**，勾选 **Allow StudentFlow to read my Gmail messages**，再点击 **Connect Gmail**。浏览器中选择已添加为测试用户的 Gmail，核对应用为自己刚创建的 StudentFlow，权限只有邮箱只读，再按页面允许的流程授权。若出现未验证提醒，先核对自己的应用配置和账号；若明确显示访问被阻止，则停止并记录错误，不使用陌生应用或他人的配置文件。

8. **读取一封真实邮件。**回到程序，点击 **Refresh inbox**，先保留默认 5 封。在 **Choose a message** 选择一封，点击 **Open message**，在下方 **Message review** 检查正文和发送时间。附件在 **Attachments → Read attachments** 中查看。5 封是本次读取数量，授权本身并非只覆盖这 5 封邮件。

9. **继续任务流程。**没有模型 API 时，可以在 **Add a task** 中人工填写任务并确认 **Save task**，再到 **Study Plan** 添加自己的空闲时段和固定安排、生成计划。若要使用 AI 提取，需另行配置模型 API，展开 **Extract tasks with AI**，勾选向模型服务发送当前邮件的确认项后点击 **Extract tasks**；邮箱授权成功不会自动把邮件发给模型。

10. **准备 10 月 16 日演示。**自己在邮箱网页中手动发送或接收一封专门用于演示的邮件，包含明确截止时间和一个 PDF／Word 附件。演示时从真实收件箱读取它，展示正文、附件、任务确认和计划。邮件可以是人为编写的测试内容，但应说明“测试邮件经真实邮箱接入”，不能称为真实业务评估数据。只有本人完成登录、列表读取和邮件导入，才记录为“真实邮箱接入已验证”。

**10 月 15 日再完整试一次。**External + Testing 状态下，本应用所用的刷新令牌会在 7 天后过期，届时需要重新登录；不要到上台时才检查授权。[Google 令牌有效期说明](https://developers.google.com/identity/protocols/oauth2#expiration)
