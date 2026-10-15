param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ProductArgs)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$pythonFile = Join-Path $PSScriptRoot 'runtime\python.txt'
if (-not (Test-Path -LiteralPath $pythonFile)) { throw '请先双击安装.cmd。' }
$productPython = [System.IO.File]::ReadAllText($pythonFile).Trim()
if (-not (Test-Path -LiteralPath $productPython)) { throw '安装时的Python已不存在，请重新运行安装.cmd。' }
if (-not $ProductArgs) { $ProductArgs = @('status') }
& $productPython -B (Join-Path $PSScriptRoot 'run.py') @ProductArgs
exit $LASTEXITCODE
