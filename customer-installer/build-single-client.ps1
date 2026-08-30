param(
    [Parameter(Mandatory = $true)][string]$RustDeskDir,
    [ValidateSet("customer", "staff")][string]$Role = "customer",
    [string]$AgentExe,
    [string]$WorkerExe,
    [string]$WorkerToken,
    [int]$CustomerId,
    [string]$AgentToken,
    [string]$ApiBase = "https://rmm.itadl.com:8443",
    [string]$Output = "远程安装客户端.exe"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path $PSScriptRoot).Path
$rustDesk = (Resolve-Path $RustDeskDir).Path
if ($Role -eq "customer") {
    if (-not $AgentExe) {
        throw "客户版需要 AgentExe"
    }
    $agent = (Resolve-Path $AgentExe).Path
}
else {
    if (-not $WorkerExe -or -not $WorkerToken) {
        throw "客服版需要 WorkerExe 和 WorkerToken"
    }
    $worker = (Resolve-Path $WorkerExe).Path
}
$stage = Join-Path $env:TEMP "remote-install-client-$([guid]::NewGuid())"
$payloadDir = Join-Path $stage "payload"
$publishDir = Join-Path $stage "publish"
New-Item -ItemType Directory -Force $payloadDir | Out-Null

try {
    # Only package the RustDesk runtime. Build outputs, symbols, optional drivers,
    # and helper archives can make the self-extracting EXE unnecessarily large.
    Copy-Item (Join-Path $rustDesk "rustdesk.exe") $payloadDir -Force
    Get-ChildItem $rustDesk -File -Filter "*.dll" | Copy-Item -Destination $payloadDir -Force
    foreach ($directory in @("data")) {
        $sourceDirectory = Join-Path $rustDesk $directory
        if (Test-Path $sourceDirectory) {
            Copy-Item $sourceDirectory (Join-Path $payloadDir $directory) -Recurse -Force
        }
    }
    Set-Content (Join-Path $payloadDir "ops-client-role.txt") $Role -Encoding ASCII
    if ($Role -eq "customer") {
        Copy-Item $agent (Join-Path $payloadDir "customer-agent.exe") -Force
        Set-Content (Join-Path $payloadDir "agent-api-base.txt") $ApiBase.TrimEnd('/') -Encoding ASCII
        if ($CustomerId -and $AgentToken) {
            $config = @{ api_base = $ApiBase.TrimEnd('/'); customer_id = $CustomerId; agent_token = $AgentToken } |
                ConvertTo-Json -Compress
            [IO.File]::WriteAllText((Join-Path $payloadDir "agent-config.json"), $config, [Text.UTF8Encoding]::new($false))
        }
    }
    else {
        Copy-Item $worker (Join-Path $payloadDir "rustdesk-worker.exe") -Force
        $workerConfig = @{
            api_base = $ApiBase.TrimEnd('/');
            worker_token = $WorkerToken;
            rustdesk_exe = "C:\Program Files\RemoteInstallStaff\rustdesk.exe";
            rustdesk_connect_flag = "--ops-connect"
        } | ConvertTo-Json -Compress
        [IO.File]::WriteAllText((Join-Path $payloadDir "worker-config.json"), $workerConfig, [Text.UTF8Encoding]::new($false))
    }

    $zip = Join-Path $root "payload.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $payloadDir "*") -DestinationPath $zip -CompressionLevel Optimal
    dotnet publish (Join-Path $root "customer-installer.csproj") -c Release -r win-x64 --self-contained true `
        /p:PublishAot=false /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true -o $publishDir
    $exe = Get-ChildItem -Path $publishDir -Filter *.exe -File | Select-Object -First 1
    if (-not $exe) { throw "单文件客户端没有生成" }
    $outputPath = if ([IO.Path]::IsPathRooted($Output)) { $Output } else { Join-Path (Get-Location) $Output }
    Copy-Item $exe.FullName $outputPath -Force
    Write-Host "Created: $outputPath"
}
finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $root "payload.zip") -Force -ErrorAction SilentlyContinue
}
