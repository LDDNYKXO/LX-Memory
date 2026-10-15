param([Parameter(Mandatory=$true)][string]$InputFile)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sessionRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'runtime\session')) + '\'
$inputPath = [System.IO.Path]::GetFullPath($InputFile)
if (-not $inputPath.StartsWith($sessionRoot, [System.StringComparison]::OrdinalIgnoreCase) -or [System.IO.Path]::GetFileName($inputPath) -notmatch '^config-write-[0-9a-f]{24}\.json$') { throw 'Invalid private plan path' }
$request = Get-Content -LiteralPath $inputPath -Raw -Encoding UTF8 | ConvertFrom-Json
$targetPath = [System.IO.Path]::GetFullPath([string]$request.target)
$userRoot = [Environment]::GetFolderPath('UserProfile')
$allowedPaths = @('.codex\config.toml', '.dsh\profiles\desktop\cordis.patch.yml', '.workbuddy\mcp.json', '.zcode\cli\config.json', '.lingxi-memory-connector\install.json', '.agents\skills\lingxi-cloud-memory\SKILL.md', '.workbuddy\skills\lingxi-cloud-memory\SKILL.md') | ForEach-Object { [System.IO.Path]::GetFullPath((Join-Path $userRoot $_)) }
if ($allowedPaths -notcontains $targetPath) { throw 'Target outside allowed client settings' }
$ancestor = $targetPath
while ($ancestor -and $ancestor -ne $userRoot) {
    if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { throw 'Linked target refused' }
    $ancestor = [System.IO.Path]::GetDirectoryName($ancestor)
}
function Get-BytesHash([byte[]]$Bytes) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() } finally { $algorithm.Dispose() }
}
$newBytes = [Convert]::FromBase64String([string]$request.content_base64)
if ((Get-BytesHash $newBytes) -ne $request.after_sha256) { throw 'Payload hash mismatch' }
if (Test-Path -LiteralPath $targetPath) {
    if ((Get-BytesHash ([System.IO.File]::ReadAllBytes($targetPath))) -ne $request.before_sha256) { throw 'Concurrent change' }
} elseif ($null -ne $request.before_sha256) { throw 'Target disappeared' }
$parentPath = [System.IO.Path]::GetDirectoryName($targetPath)
New-Item -ItemType Directory -Path $parentPath -Force | Out-Null
[System.IO.File]::WriteAllBytes($targetPath, $newBytes)
if ((Get-BytesHash ([System.IO.File]::ReadAllBytes($targetPath))) -ne $request.after_sha256) { throw 'Readback mismatch' }
Write-Output 'Verified'
