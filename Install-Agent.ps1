param(
    [string]$Clients = 'Generic',
    [switch]$Apply,
    [switch]$Check,
    [switch]$Login,
    [switch]$RememberLogin,
    [string]$PythonExe = '',
    [string]$Ref = 'main'
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if ($RememberLogin -and -not $Login) { throw 'RememberLogin requires Login and the user choice to retain login.' }
if ($Ref -notmatch '^[A-Za-z0-9._-]+$') { throw 'Invalid GitHub ref.' }
$allowed = @('Codex', 'DSH', 'WorkBuddy', 'ZCode', 'Generic')
foreach ($clientName in $Clients.Split(',')) {
    if ($clientName.Trim() -notin $allowed) { throw 'Use Codex,DSH,WorkBuddy,ZCode or Generic.' }
}

function Read-GitHubJson([string]$Endpoint) {
    try {
        return Invoke-RestMethod -Uri ('https://api.github.com/' + $Endpoint) -Headers @{
            Accept = 'application/vnd.github+json'; 'User-Agent' = 'Lingxi-Memory-Installer'
        } -TimeoutSec 60
    } catch {
        throw 'Public GitHub source download failed. Check network, repository URL or API rate limits and retry later. No GitHub sign-in or token is required. Existing installation is preserved.'
    }
}

$registryFile = Join-Path $env:USERPROFILE '.lingxi-memory-connector\install.json'
if (Test-Path -LiteralPath $registryFile) {
    try {
        $registration = Get-Content -LiteralPath $registryFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($registration.product -ne 'lingxi-memory-connector') { throw 'Invalid registry' }
        if (-not (Test-Path -LiteralPath $registration.python -PathType Leaf)) { throw 'Runtime missing' }
        if (-not [System.IO.Path]::IsPathRooted($registration.root) -or -not [System.IO.Path]::IsPathRooted($registration.python)) { throw 'Absolute paths required' }
        if (-not (Test-Path -LiteralPath (Join-Path $registration.root 'run.py') -PathType Leaf)) { throw 'Entrypoint missing' }
        $PythonExe = $registration.python
    } catch { throw 'Existing installation registry is invalid. Preserved; no second authorization profile was created.' }
} elseif ($Check) {
    Write-Output '{"state":"NOT_INSTALLED","action":"install_required"}'
    exit 3
} elseif (-not $Apply) {
    Write-Output '{"state":"NOT_INSTALLED","action":"run_with_apply"}'
    exit 3
}

if (-not $PythonExe) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        & $pythonCommand.Source -B -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) and sys.maxsize > 2**32 else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $PythonExe = $pythonCommand.Source }
    }
}

if (-not $PythonExe) {
    # Official private runtime only; no system Python, PATH or registry changes.
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Automatic runtime currently supports Windows x64 only.' }
    $bootstrapBase = Join-Path $env:LOCALAPPDATA 'LingxiMemoryConnector\bootstrap'
    New-Item -ItemType Directory -Path $bootstrapBase -Force | Out-Null
    $runtimePath = Join-Path $bootstrapBase 'python-3.13.16'
    $runtimeExe = Join-Path $runtimePath 'python.exe'
    if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
        if (Test-Path -LiteralPath $runtimePath) { throw 'Incomplete runtime directory preserved; inspect before continuing.' }
        $archivePath = Join-Path $bootstrapBase 'python-3.13.16-embeddable-amd64.zip'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-embeddable-amd64.zip' -OutFile $archivePath -TimeoutSec 180
        $digest = [System.Security.Cryptography.SHA256]::Create()
        try { $archiveHash = [BitConverter]::ToString($digest.ComputeHash([System.IO.File]::ReadAllBytes($archivePath))).Replace('-', '').ToLowerInvariant() }
        finally { $digest.Dispose() }
        if ($archiveHash -ne '589dc1e9d02549ca5f680710307ff9b77f6e3ffc4aa3efccd7fa54eeeafac94b') { throw 'Official Python runtime hash mismatch; nothing executed.' }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimePath
    }
    $PythonExe = $runtimeExe
}
& $PythonExe -B -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) and sys.maxsize > 2**32 else 1)'
if ($LASTEXITCODE -ne 0) { throw 'A functioning Windows x64 Python 3.12+ runtime is required.' }

# Status and normal login must work with the single PS entrypoint, even offline
# from GitHub. Use the registered connector; do not download a second controller.
if ($Check -and $registration) {
    $productEntry = Join-Path $registration.root 'run.py'
    $statusText = & $PythonExe -B $productEntry status
    $statusExit = $LASTEXITCODE
    $status = ($statusText -join "`n") | ConvertFrom-Json
    $connection = @{ state = $status.state; action = 'check_error'; error = $status.error }
    if ($statusExit -eq 0 -and $status.state -eq 'CONNECTED' -and $status.cloud_available -eq $true) {
        $connection.action = 'ready'
    } elseif ($statusExit -eq 0 -and $status.state -in @('NOT_LOGGED_IN', 'AUTH_REQUIRED')) {
        $connection.action = 'login_required'
        if ($Login) {
            $loginArguments = @('-B', $productEntry, 'login')
            if ($RememberLogin) { $loginArguments += '--remember' }
            $loginText = & $PythonExe @loginArguments
            $loginExit = $LASTEXITCODE
            $opened = ($loginText -join "`n") | ConvertFrom-Json
            $connection.state = $opened.state
            if ($loginExit -eq 0) { $connection.action = 'finish_login' }
            else { $connection.action = 'check_error'; $connection.error = $opened.error }
        }
    } elseif ($statusExit -eq 0 -and $status.state -eq 'LOGIN_IN_PROGRESS') {
        $connection.action = 'finish_login'
    }
    @{ reused_install = $true; root = $registration.root; connection = $connection } | ConvertTo-Json -Depth 8
    if ($connection.action -eq 'ready') { exit 0 }
    exit 3
}

function Get-ControllerHash([byte[]]$Bytes) {
    $digest = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($digest.ComputeHash($Bytes)).Replace('-', '').ToLowerInvariant() }
    finally { $digest.Dispose() }
}
$expectedControllerSha256 = 'f056310cb76ec7d6388deb1ffe1ed41d41aaea140b23b6b14e16992eeb7965be'
$agentScript = Join-Path $PSScriptRoot 'agent_install.py'
$adjacentControllerVerified = (Test-Path -LiteralPath $agentScript -PathType Leaf) -and ((Get-ControllerHash ([System.IO.File]::ReadAllBytes($agentScript))) -eq $expectedControllerSha256)
if (-not $adjacentControllerVerified) {
    $downloadDir = Join-Path $env:LOCALAPPDATA 'LingxiMemoryConnector\bootstrap'
    New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
    # Separate identity and exact hash prevent reuse of an old private/GitHub-CLI controller.
    $agentScript = Join-Path $downloadDir ('agent_install-public-' + $expectedControllerSha256 + '.py')
    if (-not (Test-Path -LiteralPath $agentScript -PathType Leaf)) {
    $file = Read-GitHubJson ('repos/LDDNYKXO/lingxi-memory-connector/contents/agent_install.py?ref=' + $Ref)
    if ($file.encoding -ne 'base64') { throw 'Invalid GitHub source response.' }
    $sourceBytes = [Convert]::FromBase64String($file.content)
    $blobHeader = [System.Text.Encoding]::ASCII.GetBytes('blob ' + $sourceBytes.Length + [char]0)
    $blobBytes = [byte[]]($blobHeader + $sourceBytes)
    $sha = [System.Security.Cryptography.SHA1]::Create()
    try { $sourceHash = [BitConverter]::ToString($sha.ComputeHash($blobBytes)).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if ($sourceHash -ne $file.sha) { throw 'GitHub source hash mismatch.' }
    if ((Get-ControllerHash $sourceBytes) -ne $expectedControllerSha256) { throw 'Controller version mismatch. Download the matching Install-Agent.ps1 and retry; no controller executed.' }
    [System.IO.File]::WriteAllBytes($agentScript, $sourceBytes)
    }
}
if ((Get-ControllerHash ([System.IO.File]::ReadAllBytes($agentScript))) -ne $expectedControllerSha256) { throw 'Cached controller hash mismatch; no controller executed.' }
$arguments = @('-B', $agentScript, '--clients', $Clients, '--ref', $Ref)
if ($Apply) { $arguments += '--apply' }
if ($Check) { $arguments += '--check' }
if ($Login) { $arguments += '--login' }
if ($RememberLogin) { $arguments += '--remember-login' }
& $PythonExe @arguments
exit $LASTEXITCODE
