<#
.SYNOPSIS
    安裝或更新 agy 常用輔助腳本（目前為 latest_cid.py）至 ~\.gemini\config\scripts。
.DESCRIPTION
    - 將 agy/latest_cid.py 安裝或覆寫更新至 ~/.gemini/config/scripts/latest_cid.py（本地執行時優先複製，遠端 irm | iex 則從 GitHub Raw 下載）。
    - 不會刪除目標目錄中的其他既有檔案。
.EXAMPLE
    pwsh -Command "irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/install-scripts.ps1 | iex"
    pwsh -File .\agy\install-scripts.ps1
    pwsh -File .\agy\install-scripts.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [string]$ScriptsDir = (Join-Path $HOME ".gemini\config\scripts"),
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RawBaseUrl = 'https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy'
$HelperScripts = @(
    @{ Name = 'latest_cid.py'; Description = '最新對話 ID 查詢腳本' }
)

if (-not $DryRun -and -not (Test-Path $ScriptsDir)) {
    New-Item -ItemType Directory -Path $ScriptsDir -Force | Out-Null
}

foreach ($script in $HelperScripts) {
    $dest = Join-Path $ScriptsDir $script.Name
    $localSrc = if ($PSScriptRoot) { Join-Path $PSScriptRoot $script.Name } else { $null }
    $rawUrl = "$RawBaseUrl/$($script.Name)"

    if ($DryRun) {
        $srcLabel = if ($localSrc -and (Test-Path -LiteralPath $localSrc)) { $localSrc } else { $rawUrl }
        Write-Host "[DryRun] $dest <- $srcLabel" -ForegroundColor Yellow
        continue
    }

    if ($localSrc -and (Test-Path -LiteralPath $localSrc)) {
        Copy-Item -LiteralPath $localSrc -Destination $dest -Force
    } else {
        Invoke-WebRequest -Uri $rawUrl -OutFile $dest -UseBasicParsing
    }
    Write-Host "[OK] $dest -> 已安裝／更新$($script.Description)" -ForegroundColor Green
}
