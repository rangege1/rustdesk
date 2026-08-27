using System.Diagnostics;
using System.IO.Compression;
using System.Reflection;
using System.Security.Principal;

const string installRoot = @"C:\Program Files\RemoteInstallClient";

if (!OperatingSystem.IsWindows())
{
    Console.Error.WriteLine("This installer only supports Windows.");
    return 1;
}

if (!IsAdministrator())
{
    var startInfo = new ProcessStartInfo(Environment.ProcessPath!)
    {
        UseShellExecute = true,
        Verb = "runas",
    };
    try
    {
        Process.Start(startInfo);
        return 0;
    }
    catch (Exception ex)
    {
        Console.Error.WriteLine($"管理员授权失败: {ex.Message}");
        return 1;
    }
}

try
{
    var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
        ?? throw new InvalidOperationException("安装包载荷缺失");

    Directory.CreateDirectory(installRoot);
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
    }

    Process.Start(new ProcessStartInfo(rustDesk) { WorkingDirectory = installRoot });
    Console.WriteLine(isCustomer ? "远程安装客户端安装完成" : "远程安装客服端安装完成");
    return 0;
}
catch (Exception ex)
{
    Console.Error.WriteLine($"安装失败: {ex.Message}");
    return 1;
}

static bool IsAdministrator()
{
    using var identity = WindowsIdentity.GetCurrent();
    return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
}
