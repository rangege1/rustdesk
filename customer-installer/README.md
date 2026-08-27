# 单文件客户安装器

`build-single-client.ps1` 将已经编译好的 RustDesk 发布目录、`customer-agent.exe` 和某个客户的 Agent 配置封装成一个 Windows EXE。

```powershell
.\build-single-client.ps1 `
  -RustDeskDir .\rustdesk `
  -AgentExe .\customer-agent.exe `
  -CustomerId 12 `
  -AgentToken "从后台获取的客户令牌"
```

客户只需双击生成的 `远程安装客户端.exe`，Windows 会弹出一次管理员授权。程序随后释放到 `C:\Program Files\RemoteInstallClient`，注册 Agent 开机启动，并启动 RustDesk 与 Agent。

此封包过程不重新编译 RustDesk，通常只需几十秒。Agent Token 是客户专属配置，不要提交到 Git 或写入公共工作流日志。
