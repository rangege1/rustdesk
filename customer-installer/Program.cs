using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Reflection;
using System.Security.Principal;
using System.Text.Json;
using System.Windows.Forms;

const string installRoot = @"C:\Program Files\RemoteInstallClient";
const string logFile = @"C:\ProgramData\RemoteInstall\agent\logs\customer-installer.log";

void Log(string message)
{
    try
    {
        Directory.CreateDirectory(Path.GetDirectoryName(logFile)!);
        File.AppendAllText(logFile, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {message}{Environment.NewLine}");
    }
    catch { }
}

Log($"installer_start pid={Environment.ProcessId}");

if (!OperatingSystem.IsWindows())
{
    Log("installer_rejected non_windows");
    Console.Error.WriteLine("This installer only supports Windows.");
    return 1;
}

if (!IsAdministrator())
{
    Log("admin_elevation_requested");
    var startInfo = new ProcessStartInfo(Environment.ProcessPath!)
    {
        UseShellExecute = true,
        Verb = "runas",
    };
    try
    {
        Process.Start(startInfo);
        Log("admin_elevation_started");
        return 0;
    }
    catch (Exception ex)
    {
        Log($"admin_elevation_failed type={ex.GetType().Name}");
        Console.Error.WriteLine($"管理员授权失败: {ex.Message}");
        return 1;
    }
}

try
{
    var killed = 0;
    foreach (var process in Process.GetProcessesByName("rustdesk"))
    {
        try { process.Kill(true); killed++; } catch { }
        process.Dispose();
    }
    Log($"rustdesk_processes_stopped count={killed}");

    var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
        ?? throw new InvalidOperationException("安装包载荷缺失");

    Directory.CreateDirectory(installRoot);
    Log($"payload_extract_start install_root={installRoot}");
    using var archive = new ZipArchive(payload, ZipArchiveMode.Read);
    foreach (var entry in archive.Entries)
    {
        var target = Path.GetFullPath(Path.Combine(installRoot, entry.FullName));
        if (!target.StartsWith(Path.GetFullPath(installRoot) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("安装包包含非法路径");
        if (string.IsNullOrEmpty(entry.Name))
        {
            Directory.CreateDirectory(target);
            continue;
        }
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        entry.ExtractToFile(target, true);
    }

    var rustDesk = Path.Combine(installRoot, "rustdesk.exe");
    var role = File.ReadAllText(Path.Combine(installRoot, "ops-client-role.txt")).Trim();
    var isCustomer = string.Equals(role, "customer", StringComparison.OrdinalIgnoreCase);
    Log($"payload_extract_ok role={(isCustomer ? "customer" : "staff")}");
    if (!File.Exists(rustDesk))
        throw new InvalidOperationException("安装包不完整，缺少 RustDesk");

    if (isCustomer)
    {
        var agent = Path.Combine(installRoot, "customer-agent.exe");
        if (!File.Exists(agent))
            throw new InvalidOperationException("安装包不完整，缺少 customer-agent");

        var agentConfig = Path.Combine(installRoot, "agent-config.json");
        if (!File.Exists(agentConfig))
        {
            var apiBasePath = Path.Combine(installRoot, "agent-api-base.txt");
            var apiBase = File.Exists(apiBasePath)
                ? File.ReadAllText(apiBasePath).Trim().TrimEnd('/')
                : "https://rmm.itadl.com:8443";
            var activationCode = PromptActivationCode();
            if (string.IsNullOrWhiteSpace(activationCode))
                throw new InvalidOperationException("未完成激活，客户端未启动");
            var config = Activate(apiBase, activationCode);
            File.WriteAllText(agentConfig, config, new System.Text.UTF8Encoding(false));
            Log("customer_activation_ok");
        }

        using var key = Microsoft.Win32.Registry.LocalMachine.CreateSubKey(
            @"Software\Microsoft\Windows\CurrentVersion\Run");
        key!.SetValue("RemoteInstallCustomerAgent", $"\"{agent}\"");
        Process.Start(new ProcessStartInfo(agent) { WorkingDirectory = installRoot });
        Log("customer_agent_started");
    }

    Process.Start(new ProcessStartInfo(rustDesk) { WorkingDirectory = installRoot });
    Log($"rustdesk_started role={(isCustomer ? "customer" : "staff")}");
    Console.WriteLine(isCustomer ? "远程安装客户端安装完成" : "远程安装客服端安装完成");
    return 0;
}
catch (Exception ex)
{
    Log($"installer_failed type={ex.GetType().Name} message={ex.Message.Replace("\r", " ").Replace("\n", " ")}");
    Console.Error.WriteLine($"安装失败: {ex.Message}");
    return 1;
}

static bool IsAdministrator()
{
    using var identity = WindowsIdentity.GetCurrent();
    return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
}

static string? PromptActivationCode()
{
    using var form = new Form
    {
        Text = "激活远程安装服务",
        Width = 430,
        Height = 190,
        StartPosition = FormStartPosition.CenterScreen,
        FormBorderStyle = FormBorderStyle.FixedDialog,
        MaximizeBox = false,
        MinimizeBox = false,
    };
    var label = new Label { Left = 20, Top = 20, Width = 370, Text = "请输入客服提供的激活码：" };
    var input = new TextBox { Left = 20, Top = 52, Width = 370, PlaceholderText = "例如 RI-AB12CD34" };
    var cancel = new Button { Text = "取消", Left = 220, Top = 95, Width = 80, DialogResult = DialogResult.Cancel };
    var confirm = new Button { Text = "激活", Left = 310, Top = 95, Width = 80, DialogResult = DialogResult.OK };
    form.Controls.AddRange([label, input, cancel, confirm]);
    form.AcceptButton = confirm;
    form.CancelButton = cancel;
    return form.ShowDialog() == DialogResult.OK ? input.Text.Trim() : null;
}

static string Activate(string apiBase, string activationCode)
{
    try
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        using var response = client.PostAsJsonAsync(
            $"{apiBase}/api/agent/activate",
            new { activation_code = activationCode }).GetAwaiter().GetResult();
        var body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException("激活码无效或已失效");
        using var json = JsonDocument.Parse(body);
        var root = json.RootElement;
        var returnedApiBase = root.GetProperty("api_base").GetString() ?? apiBase;
        var customerId = root.GetProperty("customer_id").GetInt32();
        var agentToken = root.GetProperty("agent_token").GetString();
        if (string.IsNullOrWhiteSpace(agentToken))
            throw new InvalidOperationException("服务器返回的激活配置不完整");
        return JsonSerializer.Serialize(new
        {
            api_base = returnedApiBase.TrimEnd('/'),
            customer_id = customerId,
            agent_token = agentToken,
        });
    }
    catch (Exception ex)
    {
        MessageBox.Show($"激活失败：{ex.Message}", "远程安装服务", MessageBoxButtons.OK, MessageBoxIcon.Error);
        throw new InvalidOperationException("激活失败");
    }
}
