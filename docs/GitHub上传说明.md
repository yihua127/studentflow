# StudentFlow GitHub 维护说明

项目公开地址为 https://github.com/yihua127/studentflow 。仓库只保存应用代码、测试、配置示例、锁文件和开发说明。

## 公开前的整理

2026-10-26 的公开准备保留了完整私有备份，并重新整理全部 18 次历史提交：作者和提交者使用 GitHub noreply 邮箱；原课表配置替换为完全虚构的日期、活动和教室；操作指南使用项目相对路径。历史提交编号因此改变。

公开仓库从清理后的独立 Git 对象库建立，原历史保留在单独的私有备份中。旧开发记录仅反映当时状态，当前功能和验证范围参见 README 和 `docs/TESTING.md`。配置示例不是任何人的实际课表。

## 后续保存

只在 `studentflow` 项目目录内维护此仓库，不要把整个课程文件夹上传。先查看变更和待提交文件，再提交代码：

```powershell
git status --short
git diff
git add <reviewed-files>
git diff --cached --stat
git diff --cached --check
git commit -m "Describe the completed change"
git push origin main
```

将 `<reviewed-files>` 替换为已检查的文件路径。不要用旧备份覆盖新历史；其他旧克隆应重新克隆公开仓库，避免把清理前的提交重新合并回来。

作者邮箱使用 GitHub Settings → Emails 中显示的 noreply 地址，并为此项目单独配置：

```powershell
git config --local user.name "YOUR_GITHUB_LOGIN"
git config --local user.email "YOUR_GITHUB_NOREPLY_ADDRESS"
```

`.env`、`credentials/`、`data/`、`local_data/`、真实邮件和附件、账号令牌、数据库、日志、下载纪要以及课程原始材料不应提交。`.gitignore` 已覆盖常见文件名；每次提交仍需核对实际内容，不能依赖忽略规则识别被改名的数据。

每位使用者在自己的电脑上安装依赖、配置 API 和完成 Google 授权；这些账号信息不随代码共享。当前仓库未添加开源许可证，公开可见不代表已授予开源许可。
