param(
    [Parameter(Mandatory = $true)][string]$RustDeskDir,
    [ValidateSet("customer", "staff")][string]$Role = "customer",
    [string]$AgentExe,
    [int]$CustomerId,
    [string]$AgentToken,
    [string]$InstallerPassword = "",
    [string]$ApiBase = "https://rmm.itadl.com:8443",
    [string]$Output = "远程安装客户端.exe"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path $PSScriptRoot).Path
$rustDesk = (Resolve-Path $RustDeskDir).Path
if ($Role -eq "customer") {
    if (-not $AgentExe -or -not $CustomerId -or -not $AgentToken) {
        throw "客户版需要 AgentExe、CustomerId 和 AgentToken"
    }
    $agent = (Resolve-Path $AgentExe).Path
}
$stage = Join-Path $env:TEMP "remote-install-client-$([guid]::NewGuid())"
$payloadDir = Join-Path $stage "payload"
$publishDir = Join-Path $stage "publish"
New-Item -ItemType Directory -Force $payloadDir | Out-Null

try {
    Copy-Item (Join-Path $rustDesk "*") $payloadDir -Recurse -Force
    Set-Content (Join-Path $payloadDir "ops-client-role.txt") $Role -Encoding ASCII
    if ($Role -eq "customer") {
        Copy-Item $agent (Join-Path $payloadDir "customer-agent.exe") -Force
        $config = @{ api_base = $ApiBase.TrimEnd('/'); customer_id = $CustomerId; agent_token = $AgentToken; installer_password = $InstallerPassword } |
            ConvertTo-Json -Compress
        [IO.File]::WriteAllText(
            (Join-Path $payloadDir "agent-config.json"),
            $config,
            [Text.UTF8Encoding]::new($false)
        )
    }

    $zip = Join-Path $root "payload.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $payloadDir "*") -DestinationPath $zip -CompressionLevel Optimal
    dotnet publish (Join-Path $root "customer-installer.csproj") -c Release -r win-x64 --self-contained true `
        /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true -o $publishDir
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
