using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Principal;

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
