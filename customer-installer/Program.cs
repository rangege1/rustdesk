using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Text;
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
    catch
    {
        try
        {
            File.AppendAllText(
                Path.Combine(Path.GetTempPath(), "RemoteInstallClient-startup.log"),
                $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {message}{Environment.NewLine}");
        }
        catch { }
    }
}

void ShowError(string message)
{
    try
    {
        MessageBox.Show(
            $"{message}\n\n详细日志：{logFile}",
            "远程安装客户端",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }
    catch { }
}

Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
Application.ThreadException += (_, args) =>
{
    Log($"ui_exception type={args.Exception.GetType().Name} message={args.Exception.Message}");
    ShowError($"客户端启动失败：{args.Exception.Message}");
};

AppDomain.CurrentDomain.UnhandledException += (_, args) =>
    Log($"unhandled_exception terminating={args.IsTerminating} detail={args.ExceptionObject}");
TaskScheduler.UnobservedTaskException += (_, args) =>
{
    Log($"unobserved_task_exception detail={args.Exception}");
    args.SetObserved();
};

Log($"installer_start pid={Environment.ProcessId} os={Environment.OSVersion} arch={RuntimeInformation.ProcessArchitecture} 64bit={Environment.Is64BitOperatingSystem}");

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
        ShowError($"管理员授权失败：{ex.Message}");
        return 1;
    }
}

try
{
    StopProcesses("rustdesk");
    StopProcesses("customer-agent");

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
        ExtractWithRetry(entry, target);
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
            var config = Register(apiBase);
            File.WriteAllText(agentConfig, config, new System.Text.UTF8Encoding(false));
            Log("customer_activation_ok");
        }

        using var key = Microsoft.Win32.Registry.LocalMachine.CreateSubKey(
            @"Software\Microsoft\Windows\CurrentVersion\Run");
        key!.SetValue("RemoteInstallCustomerAgent", $"\"{agent}\"");
        StartChild(agent, "customer-agent");
        Log("customer_agent_started");
    }

    StartChild(rustDesk, "rustdesk");
    Log($"rustdesk_started role={(isCustomer ? "customer" : "staff")}");
    Console.WriteLine(isCustomer ? "远程安装客户端安装完成" : "远程安装客服端安装完成");
    return 0;
}
catch (Exception ex)
{
    Log($"installer_failed type={ex.GetType().Name} message={ex.Message.Replace("\r", " ").Replace("\n", " ")}");
    ShowError($"启动失败：{ex.Message}");
    return 1;
}

static bool IsAdministrator()
{
    using var identity = WindowsIdentity.GetCurrent();
    return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
}

void StopProcesses(string processName)
{
    var stopped = 0;
    foreach (var process in Process.GetProcessesByName(processName))
    {
        try
        {
            process.Kill(true);
            stopped++;
        }
        catch (Exception ex)
        {
            Log($"process_stop_failed name={processName} type={ex.GetType().Name}");
        }
        finally
        {
            process.Dispose();
        }
    }
    Log($"processes_stopped name={processName} count={stopped}");
    if (stopped > 0)
        Thread.Sleep(500);
}

void ExtractWithRetry(ZipArchiveEntry entry, string target)
{
    IOException? lastError = null;
    for (var attempt = 1; attempt <= 8; attempt++)
    {
        try
        {
            entry.ExtractToFile(target, true);
            return;
        }
        catch (IOException ex) when (attempt < 8)
        {
            lastError = ex;
            Log($"payload_extract_retry file={entry.Name} attempt={attempt}");
            Thread.Sleep(500);
        }
    }
    throw new IOException($"无法写入文件 {entry.Name}，文件可能仍被其他程序占用", lastError);
}

void StartChild(string executable, string name)
{
    var process = Process.Start(new ProcessStartInfo
    {
        FileName = executable,
        WorkingDirectory = installRoot,
        UseShellExecute = false,
        CreateNoWindow = true,
    });
    if (process is null)
        throw new InvalidOperationException($"无法启动 {name}");
    Log($"child_process_started name={name} pid={process.Id}");
    process.Dispose();
}

static string Register(string apiBase)
{
    try
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
        using var response = client.PostAsJsonAsync(
            $"{apiBase}/api/agent/register",
            new { machine_id = GetMachineId(), computer_name = Environment.MachineName }).GetAwaiter().GetResult();
        var body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
        if (!response.IsSuccessStatusCode)
            throw new InvalidOperationException($"设备注册失败，服务器返回 HTTP {(int)response.StatusCode}");
        using var json = JsonDocument.Parse(body);
        var root = json.RootElement;
        var returnedApiBase = root.GetProperty("api_base").GetString() ?? apiBase;
        var customerId = root.GetProperty("customer_id").GetInt32();
        var agentToken = root.GetProperty("agent_token").GetString();
        if (string.IsNullOrWhiteSpace(agentToken))
            throw new InvalidOperationException("服务器返回的设备配置不完整");
        return JsonSerializer.Serialize(new
        {
            api_base = returnedApiBase.TrimEnd('/'),
            customer_id = customerId,
            agent_token = agentToken,
        });
    }
    catch (Exception ex)
    {
        MessageBox.Show($"设备注册失败：{ex.Message}", "远程安装服务", MessageBoxButtons.OK, MessageBoxIcon.Error);
        throw new InvalidOperationException("设备注册失败");
    }
}

static string GetMachineId()
{
    try
    {
        using var key = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(
            @"SOFTWARE\Microsoft\Cryptography");
        var value = key?.GetValue("MachineGuid")?.ToString();
        if (!string.IsNullOrWhiteSpace(value))
        {
            var bytes = SHA256.HashData(Encoding.UTF8.GetBytes($"RemoteInstall:{value}"));
            return Convert.ToHexString(bytes).ToLowerInvariant();
        }
    }
    catch { }
    return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
        $"RemoteInstall:{Environment.MachineName}"))).ToLowerInvariant();
}
