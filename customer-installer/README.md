# 单文件客户安装器

`build-single-client.ps1` 将已经编译好的 RustDesk 必需运行文件、可选的 `customer-agent.exe` 和客户配置封装成一个 Windows EXE。

产物用途：

- `remote-install-client-x86_64.exe`：发给客户。首次运行需要输入后台生成的激活码。
- `remote-install-staff-x86_64.exe`：安装在客服电脑，仅用于启动客服版 RustDesk。

两个文件不能混用。客服电脑不要运行客户版 EXE，客户电脑也不要运行客服版 EXE。

## 诊断日志

客户版运行后，日志保存在客户电脑的：

`C:\ProgramData\RemoteInstall\agent\logs\customer-agent.log`

安装器启动、提权、解包和启动 Agent 的日志保存在：

`C:\ProgramData\RemoteInstall\agent\logs\customer-installer.log`

日志会自动轮转，最多保留约 8 MB。日志只记录时间、阶段、状态码、任务编号和错误类型，不记录 Agent Token、远程密码或安装密码。

排查时在客户电脑 PowerShell 执行：

```powershell
Get-Content "$env:ProgramData\RemoteInstall\agent\logs\customer-agent.log" -Tail 100
Get-Content "$env:ProgramData\RemoteInstall\agent\logs\customer-installer.log" -Tail 100
```

```powershell
.\build-single-client.ps1 `
  -RustDeskDir .\rustdesk `
  -AgentExe .\customer-agent.exe `
  -CustomerId 12 `
  -AgentToken "从后台获取的客户令牌"
```

客户只需双击客户版 EXE，Windows 会弹出一次管理员授权。程序随后释放到 `C:\Program Files\RemoteInstallClient`，注册 Agent 开机启动，并启动 RustDesk 与 Agent。启动失败时会弹窗，同时把详细信息写入上述日志。

此封包过程不重新编译 RustDesk，通常只需几十秒。Agent Token 是客户专属配置，不要提交到 Git 或写入公共工作流日志。
