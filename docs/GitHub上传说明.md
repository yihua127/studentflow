# 将 StudentFlow 保存到自己的 GitHub

本项目选择保存到个人账号 `yihua127` 下的独立私有仓库 `studentflow`。以下步骤供复现和后续维护；如果仓库已经创建，不要重复初始化或创建。

只把 `studentflow` 作为仓库根目录。不要在整个 `dss5105` 课程文件夹中初始化或上传，也不要上传课程讲义、小组原始邮件包或人工标准答案。

## 1. 登录自己的账号

本机已经有可用登录时可直接进入下一步。仅在尚未登录或登录确实失效时，在 PowerShell 执行：

```powershell
gh auth login --hostname github.com --web --git-protocol https
```

按终端提示，在浏览器中登录自己的 GitHub 并完成授权。不要把登录码、访问令牌或账号密码发给别人。登录后检查当前账号：

```powershell
gh auth status --hostname github.com
```

官方说明：[GitHub CLI 登录](https://cli.github.com/manual/gh_auth_login)。

## 2. 确认仓库名称和可见性

已选择 `yihua127/studentflow`，可见性为 Private，与已有的其他课程仓库分开。

如果以后改为 Public，应先检查全部提交历史中的材料、截图和个人信息，并确认小组材料可以公开。当前场景配置含课程时间与地点；公开前也需核对。

## 3. 准备本地版本

在资源管理器中进入 `studentflow`，在此目录打开终端。若尚未建立 Git 仓库，执行：

```powershell
git init -b main
git status --short
git add .
git diff --cached --name-only
```

检查待提交列表只包含代码、测试、必要配置、文档和 `uv.lock`。`.env`、授权 JSON、令牌缓存、`data/`、真实邮件、SQLite 数据库和 `.venv/` 不应出现在列表里。`.gitignore` 已覆盖常见名称，但不能识别任意改名的敏感文件。

如发现不该提交的文件，先从暂存区撤下该文件并补充忽略规则，再提交。不要强制添加被忽略的账号文件。

```powershell
git commit -m "Add StudentFlow local prototype"
```

若 Git 提示缺少作者信息，按提示配置自己的姓名与邮箱；希望隐藏真实邮箱时使用 GitHub 账号设置中的 noreply 邮箱。

## 4. 创建并上传

仅在 `yihua127/studentflow` 尚未创建、本地已有提交时执行：

```powershell
gh repo create yihua127/studentflow --private --source . --remote origin --push
```

该命令会在 GitHub 创建私有仓库并上传当前提交。官方说明：[从本地创建 GitHub 仓库](https://cli.github.com/manual/gh_repo_create)。

已有 GitHub 仓库时，应检查本地 `origin` 是否指向该仓库，再推送；不要为了套用上面的命令覆盖已有地址或历史。

## 5. 后续保存

每完成一项可运行的功能，检查改动后提交并上传：

```powershell
git status --short
git add .
git diff --cached --name-only
git commit -m "Describe the completed change"
git push
```

仓库保存的是开发成果。邮箱授权仍需每位运行者在自己的电脑上完成，不随代码上传。
