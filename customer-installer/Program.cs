using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

const string customerInstallRoot = @"C:\Program Files\RemoteInstallClient";
const string staffInstallRoot = @"C:\Program Files\RemoteInstallStaff";
const string bootstrapRoot = @"C:\ProgramData\RemoteInstall\bootstrap";
const string logFile = @"C:\ProgramData\RemoteInstall\agent\logs\customer-installer.log";
const string rustDeskPermanentPassword = "abc123";
const uint mbOk = 0;
const uint mbIconError = 0x10;

[DllImport("user32.dll", CharSet = CharSet.Unicode)]
static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

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
        MessageBoxW(IntPtr.Zero, $"{message}\n\n详细日志：{logFile}", "远程安装客户端", mbOk | mbIconError);
    }
    catch { }
}

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

using var installMutex = new Mutex(false, @"Global\RemoteInstallRuntimeInstaller");
var installLockHeld = false;
try
{
    installLockHeld = installMutex.WaitOne(TimeSpan.FromMinutes(10));
}
catch (AbandonedMutexException)
{
    installLockHeld = true;
    Log("installer_mutex_recovered");
}
if (!installLockHeld)
{
    Log("installer_mutex_timeout");
    ShowError("另一份远程安装程序仍在运行，请等待其完成后重试。");
    return 1;
}
Log("installer_mutex_acquired");

try
{
    var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
        ?? throw new InvalidOperationException("安装包载荷缺失");
    using var archive = new ZipArchive(payload, ZipArchiveMode.Read);

    var roleEntry = archive.GetEntry("ops-client-role.txt")
        ?? throw new InvalidOperationException("安装包缺少客户端角色信息");
    using var roleReader = new StreamReader(roleEntry.Open());
    var role = roleReader.ReadToEnd().Trim();
    var isCustomer = string.Equals(role, "customer", StringComparison.OrdinalIgnoreCase);
    var installRoot = Path.Combine(
        bootstrapRoot,
        isCustomer ? "customer" : "staff",
        Environment.ProcessId.ToString());
    var finalInstallRoot = isCustomer ? customerInstallRoot : staffInstallRoot;

    StopProcesses("rustdesk");
    if (isCustomer)
    {
        DisableCustomerAgentStartup();
        StopCustomerAgentService();
        StopProcesses("customer-agent");
    }
    else
    {
        DisableStaffWorkerStartup();
        StopProcesses("rustdesk-worker");
    }

    if (Directory.Exists(installRoot))
        Directory.Delete(installRoot, true);
    Directory.CreateDirectory(installRoot);
    Log($"payload_extract_start install_root={installRoot}");
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
    Log($"payload_extract_ok role={(isCustomer ? "customer" : "staff")}");
    ValidateRustDeskRuntime(installRoot);

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

    }
    else
    {
        var worker = Path.Combine(installRoot, "rustdesk-worker.exe");
        var workerConfig = Path.Combine(installRoot, "worker-config.json");
        if (!File.Exists(worker) || !File.Exists(workerConfig))
            throw new InvalidOperationException("安装包不完整，缺少客服 Worker 或配置文件");
    }

    PrepareFinalInstall(finalInstallRoot);
    CopyPayloadToFinal(installRoot, finalInstallRoot);
    VerifyRuntimeCopy(installRoot, finalInstallRoot);
    ValidateRustDeskRuntime(finalInstallRoot);
    DeleteDirectoryWithRetry(installRoot);
    rustDesk = Path.Combine(finalInstallRoot, "rustdesk.exe");
    if (!File.Exists(rustDesk))
        throw new InvalidOperationException($"RustDesk 运行文件未写入: {rustDesk}");
    StartChild(rustDesk, "rustdesk", finalInstallRoot);
    Log($"rustdesk_started role={(isCustomer ? "customer" : "staff")}");
    if (isCustomer)
    {
        SetRustDeskPassword(rustDesk, finalInstallRoot);
        var agent = Path.Combine(finalInstallRoot, "customer-agent.exe");
        ConfigureCustomerAgentStartup(agent);
        Log("customer_agent_scheduled_task_started");
    }
    else
    {
        var worker = Path.Combine(finalInstallRoot, "rustdesk-worker.exe");
        SetRustDeskPassword(rustDesk, finalInstallRoot);
        ConfigureStaffWorkerStartup(worker);
        StartChild(worker, "rustdesk-worker", finalInstallRoot);
        Log("staff_worker_started");
    }
    Console.WriteLine(isCustomer ? "远程安装客户端安装完成" : "远程安装客服端安装完成");
    return 0;
}
catch (Exception ex)
{
    Log($"installer_failed type={ex.GetType().Name} message={ex.Message.Replace("\r", " ").Replace("\n", " ")}");
    ShowError($"启动失败：{ex.Message}");
    return 1;
}
finally
{
    installMutex.ReleaseMutex();
    Log("installer_mutex_released");
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
    for (var attempt = 1; attempt <= 20; attempt++)
    {
        if (!Process.GetProcessesByName(processName).Any())
            return;
        Thread.Sleep(500);
    }
    throw new InvalidOperationException($"无法停止 {processName}，请关闭占用程序后重试");
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

void StartChild(string executable, string name, string installRoot)
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

void PrepareFinalInstall(string destinationRoot)
{
    // The native installer can start RustDesk again before the payload is copied.
    // Stop it once more and repair ACLs left by an older elevated installation.
    StopProcesses("rustdesk");
    foreach (var service in new[] { "RustDesk", "RustDeskService" })
        RunServiceControlCommand($"stop \"{service}\"", false);
    if (!Directory.Exists(destinationRoot))
        return;

    RunIcaclsCommand($"\"{destinationRoot}\" /grant *S-1-5-32-544:(OI)(CI)F /T /C");
    foreach (var file in Directory.EnumerateFiles(destinationRoot, "*", SearchOption.AllDirectories))
    {
        try { File.SetAttributes(file, File.GetAttributes(file) & ~FileAttributes.ReadOnly); }
        catch (Exception ex) { Log($"final_attribute_clear_failed file={Path.GetFileName(file)} type={ex.GetType().Name}"); }
    }
    DeleteDirectoryWithRetry(destinationRoot);
    Log($"final_install_prepared root={destinationRoot}");
}

void DeleteDirectoryWithRetry(string directory)
{
    Exception? lastError = null;
    for (var attempt = 1; attempt <= 12; attempt++)
    {
        try
        {
            if (Directory.Exists(directory))
                Directory.Delete(directory, true);
            return;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            lastError = ex;
            Log($"directory_delete_retry path={directory} attempt={attempt} type={ex.GetType().Name}");
            Thread.Sleep(500);
        }
    }
    throw new IOException($"无法清理旧版安装目录 {directory}", lastError);
}

void RunIcaclsCommand(string arguments)
{
    using var process = Process.Start(new ProcessStartInfo
    {
        FileName = "icacls.exe",
        Arguments = arguments,
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        CreateNoWindow = true,
    });
    if (process is null)
        throw new InvalidOperationException("无法启动文件权限修复程序");
    var output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
    process.WaitForExit(30000);
    Log($"final_acl_command exit={process.ExitCode}");
    if (process.ExitCode != 0)
        throw new InvalidOperationException($"无法修复安装目录权限：{output.Trim()}");
}

void RunServiceControlCommand(string arguments, bool required)
{
    using var process = Process.Start(new ProcessStartInfo
    {
        FileName = "sc.exe",
        Arguments = arguments,
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        CreateNoWindow = true,
    });
    if (process is null)
    {
        if (required) throw new InvalidOperationException("无法控制 RustDesk 服务");
        return;
    }
    process.WaitForExit(30000);
    Log($"service_control_command exit={process.ExitCode}");
    if (required && process.ExitCode != 0)
        throw new InvalidOperationException("无法配置 Windows 服务");
}

void CopyPayloadToFinal(string sourceRoot, string destinationRoot)
{
    Directory.CreateDirectory(destinationRoot);
    foreach (var source in Directory.EnumerateFiles(sourceRoot, "*", SearchOption.AllDirectories))
    {
        var relativePath = Path.GetRelativePath(sourceRoot, source);
        var destination = Path.Combine(destinationRoot, relativePath);
        Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
        CopyFileWithRetry(source, destination, relativePath);
    }
    Log($"runtime_copy_ok install_root={destinationRoot}");
}

void ValidateRustDeskRuntime(string root)
{
    foreach (var fileName in new[]
    {
        "rustdesk.exe",
        "librustdesk.dll",
        "flutter_windows.dll",
        "desktop_multi_window_plugin.dll",
    })
    {
        var path = Path.Combine(root, fileName);
        if (!File.Exists(path) || new FileInfo(path).Length < 4096)
            throw new InvalidOperationException($"RustDesk 运行时不完整：{fileName}");
        using var stream = File.OpenRead(path);
        if (stream.ReadByte() != 'M' || stream.ReadByte() != 'Z')
            throw new InvalidOperationException($"RustDesk 运行文件格式无效：{fileName}");
    }
    Log($"runtime_structure_verified root={root}");
}

void VerifyRuntimeCopy(string sourceRoot, string destinationRoot)
{
    foreach (var source in Directory.EnumerateFiles(sourceRoot, "*", SearchOption.AllDirectories))
    {
        var relativePath = Path.GetRelativePath(sourceRoot, source);
        var destination = Path.Combine(destinationRoot, relativePath);
        if (!File.Exists(destination) || new FileInfo(source).Length != new FileInfo(destination).Length)
            throw new IOException($"运行文件复制不完整：{relativePath}");
        using var sourceStream = File.OpenRead(source);
        using var destinationStream = File.OpenRead(destination);
        var sourceHash = SHA256.HashData(sourceStream);
        var destinationHash = SHA256.HashData(destinationStream);
        if (!CryptographicOperations.FixedTimeEquals(sourceHash, destinationHash))
            throw new IOException($"运行文件复制校验失败：{relativePath}");
    }
    Log($"runtime_hash_verified install_root={destinationRoot}");
}

void CopyFileWithRetry(string source, string destination, string relativePath)
{
    Exception? lastError = null;
    for (var attempt = 1; attempt <= 12; attempt++)
    {
        try
        {
            File.Copy(source, destination, true);
            return;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            lastError = ex;
            Log($"runtime_copy_retry file={relativePath} attempt={attempt} type={ex.GetType().Name}");
            Thread.Sleep(500);
        }
    }
    throw new IOException($"无法更新运行文件 {relativePath}，文件仍被占用或权限不足", lastError);
}

void DisableCustomerAgentStartup()
{
    using var key = Microsoft.Win32.Registry.LocalMachine.CreateSubKey(
        @"Software\Microsoft\Windows\CurrentVersion\Run");
    key!.DeleteValue("RemoteInstallCustomerAgent", false);
    RunScheduledTaskCommand(false, "/Delete", "/TN", "RemoteInstallCustomerAgent", "/F");
    Log("customer_agent_startup_disabled");
}

void ConfigureCustomerAgentStartup(string agent)
{
    try
    {
        RunAgentServiceCommand(agent, true, "install", "--startup=auto");
        ConfigureCustomerAgentServiceRecovery();
        RunAgentServiceCommand(agent, true, "start");
        Log("customer_agent_service_configured account=LocalSystem");
        return;
    }
    catch (Exception ex)
    {
        Log($"customer_agent_service_fallback type={ex.GetType().Name}");
        RunAgentServiceCommand(agent, false, "remove");
    }

    // Older Task Scheduler versions reject XML ServiceAccount settings. ArgumentList
    // keeps the executable path intact without depending on command-line escaping.
    RunScheduledTaskCommand(true,
        "/Create", "/TN", "RemoteInstallCustomerAgent", "/TR", $"\"{agent}\"",
        "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F");
    RunScheduledTaskCommand(true, "/Run", "/TN", "RemoteInstallCustomerAgent");
    Log("customer_agent_scheduled_task_configured trigger=ONSTART account=SYSTEM");
}

void ConfigureCustomerAgentServiceRecovery()
{
    RunServiceControlCommand(
        "failure \"RemoteInstallCustomerAgent\" reset= 0 actions= restart/60000/restart/60000/restart/60000", true);
    RunServiceControlCommand("failureflag \"RemoteInstallCustomerAgent\" 1", false);
}

void RunScheduledTaskCommand(bool required, params string[] arguments)
{
    var startInfo = new ProcessStartInfo
    {
        FileName = "schtasks.exe",
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        CreateNoWindow = true,
    };
    foreach (var argument in arguments)
        startInfo.ArgumentList.Add(argument);
    using var process = Process.Start(startInfo);
    if (process is null)
    {
        if (required) throw new InvalidOperationException("无法启动 Windows 任务计划程序");
        return;
    }
    var output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
    process.WaitForExit(30000);
    Log($"customer_agent_scheduled_task_command exit={process.ExitCode}");
    if (required && process.ExitCode != 0)
        throw new InvalidOperationException($"客户 Agent 自启动任务配置失败：{output.Trim()}");
}

void StopCustomerAgentService()
{
    var agent = Path.Combine(customerInstallRoot, "customer-agent.exe");
    if (!File.Exists(agent))
        return;
    RunAgentServiceCommand(agent, false, "stop");
    RunAgentServiceCommand(agent, false, "remove");
    Log("customer_agent_service_removed");
}

void RunAgentServiceCommand(string agent, bool required, params string[] arguments)
{
    var startInfo = new ProcessStartInfo
    {
        FileName = agent,
        WorkingDirectory = Path.GetDirectoryName(agent)!,
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
        CreateNoWindow = true,
    };
    foreach (var argument in arguments)
        startInfo.ArgumentList.Add(argument);
    using var process = Process.Start(startInfo);
    var command = string.Join(" ", arguments);
    if (process is null)
    {
        if (required) throw new InvalidOperationException($"无法执行 Agent 服务命令 {command}");
        return;
    }
    var output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
    process.WaitForExit(30000);
    Log($"customer_agent_service_command command={command} exit={process.ExitCode}");
    if (required && process.ExitCode != 0)
        throw new InvalidOperationException($"Agent 服务命令 {command} 失败：{output.Trim()}");
}

void DisableStaffWorkerStartup()
{
    using var key = Microsoft.Win32.Registry.LocalMachine.CreateSubKey(
        @"Software\Microsoft\Windows\CurrentVersion\Run");
    key!.DeleteValue("RemoteInstallStaffWorker", false);
    Log("staff_worker_startup_disabled");
}

void ConfigureStaffWorkerStartup(string worker)
{
    using var key = Microsoft.Win32.Registry.LocalMachine.CreateSubKey(
        @"Software\Microsoft\Windows\CurrentVersion\Run");
    key!.SetValue("RemoteInstallStaffWorker", $"\"{worker}\"");
}

void SetRustDeskPassword(string rustDesk, string installRoot)
{
    for (var attempt = 1; attempt <= 8; attempt++)
    {
        using var process = Process.Start(new ProcessStartInfo
        {
            FileName = rustDesk,
            Arguments = $"--ops-password {rustDeskPermanentPassword}",
            WorkingDirectory = installRoot,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        });
        if (process is not null)
        {
            var output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
            process.WaitForExit(5000);
            if (output.Contains("Done!", StringComparison.Ordinal))
            {
                Log($"rustdesk_password_set attempt={attempt}");
                return;
            }
            Log($"rustdesk_password_retry attempt={attempt} output={output.Trim().Replace("\r", " ").Replace("\n", " ")}");
        }
        Thread.Sleep(1000);
    }
    throw new InvalidOperationException("RustDesk 固定密码写入失败");
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
        MessageBoxW(IntPtr.Zero, $"设备注册失败：{ex.Message}", "远程安装服务", mbOk | mbIconError);
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
