<#
.SYNOPSIS
    建立或同步 agy 自訂代理 (.md)，自動生成標準 YAML 設定並冪等注入 ~\.agents\skills 與跨對話紀錄規則。
.DESCRIPTION
    - 若目標 ~/.gemini/config/agents/<agent>.md 不存在，會自動建立標準範本（含生成指令註解、excludeDefaultComponents: true 與已驗證可用的 tools 清單）。
    - 若檔案已存在，則保留既有的 YAML Frontmatter 與自訂系統提示詞正文，僅冪等更新尾端的 <conversation_transcript> 與 <skills> 區塊。
    - 冪等鏡像 skills：agy 原生掃描路徑不含 HOME 層級的 ~\.agents\skills（僅工作區 .agents/skills 與全域 ~/.gemini/config/skills 等），
      本腳本會將各 skill 以位元組級複製鏡像到 ~/.gemini/config/skills，使預設代理、TUI 斜線指令與 Antigravity App/IDE 得以原生發現；
      實測 agy 掃描器不跟隨 directory junction，故須實體複製。僅鏡像頂層即含 SKILL.md 的資料夾（對齊 native 掃描結構 skills/*/SKILL.md）。
      ~/.gemini/config/skills 的管理範圍僅限同名：同名資料夾一律刪除重抄以來源為準（殘缺鏡像自動修復），
      其餘資料夾（如使用者自建 skill）完全不動；來源端已刪除的 skill 其鏡像因無法與自建內容區分，刻意不自動清理，需自行移除。
.EXAMPLE
    pwsh -Command "irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/sync-agent.ps1 | iex"
    pwsh -File .\agy\sync-agent.ps1 -Agents build
    pwsh -File .\agy\sync-agent.ps1 -Agents build,research -DryRun
#>
[CmdletBinding()]
param(
    [string[]]$Agents = @("build"),
    [string]$SkillsDir = (Join-Path $HOME ".agents\skills"),
    [string]$AgentsDir = (Join-Path $HOME ".gemini\config\agents"),
    [string]$GlobalSkillsDir = (Join-Path $HOME ".gemini\config\skills"),
    [string]$AppDataDir = (Join-Path $HOME ".gemini\antigravity-cli"),
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$OneLinerCmd = 'pwsh -Command "irm https://raw.githubusercontent.com/Artin0123/harness-setup/master/agy/sync-agent.ps1 | iex"'

function Parse-SkillFrontmatter {
    param([string]$SkillMdPath)

    $raw = Get-Content -Path $SkillMdPath -Raw -Encoding UTF8
    if ($raw -notmatch '(?s)\A---\r?\n(.*?)\r?\n---') {
        return $null
    }

    $yaml = $Matches[1]
    $name = ""
    $descLines = [System.Collections.Generic.List[string]]::new()
    $inDescBlock = $false
    $descIndent = -1

    foreach ($line in ($yaml -split '\r?\n')) {
        if ($inDescBlock) {
            if ($line -match '^(\s+)(.*)$') {
                $indent = $Matches[1].Length
                if ($descIndent -lt 0) { $descIndent = $indent }
                if ($indent -ge $descIndent) {
                    $descLines.Add($Matches[2].Trim())
                    continue
                }
            } elseif ($line.Trim() -eq "") {
                continue
            }
            $inDescBlock = $false
        }

        if ($line -match '^name:\s*(.+?)\s*$') {
            $name = $Matches[1].Trim().Trim('"', "'")
        } elseif ($line -match '^description:\s*(.*)$') {
            $val = $Matches[1].Trim()
            if ($val -in @('>', '>-', '|', '|-')) {
                $inDescBlock = $true
                $descIndent = -1
            } else {
                $descLines.Add($val.Trim('"', "'"))
            }
        }
    }

    if (-not $name) {
        $name = Split-Path (Split-Path $SkillMdPath -Parent) -Leaf
    }

    return [PSCustomObject]@{
        Name        = $name
        Description = ($descLines -join " ").Trim()
        Path        = $SkillMdPath
    }
}

function Get-TemplateFrontmatterEntries {
    param([string]$AgentName)

    return @(
        @{ Key = 'name'; Text = "name: $AgentName" },
        @{ Key = 'description'; Text = "description: $AgentName" },
        @{ Key = 'excludeDefaultComponents'; Text = "excludeDefaultComponents: true" },
        @{
            Key  = 'tools'
            Text = @"
tools:
  # --- 檔案讀寫 ---
  - read_file
  - write_file
  - edit_file
  # --- 終端機 (manage_task 會自動強制附掛，無需填寫) ---
  - run_command
  # --- 網路與生圖 ---
  # - search_web
  # - generate_image
  # --- 互動提問 ---
  - ask_question
  # --- 子代理與排程 (已驗證可用，依需求取消註解) ---
  # - define_subagent
  # - invoke_subagent
  # - manage_subagents
  # - send_message
  # - run_workflow
  # - schedule
"@.TrimEnd("`r", "`n")
        }
    )
}

function New-AgentTemplate {
    param([string]$AgentName)

    $entries = Get-TemplateFrontmatterEntries -AgentName $AgentName
    $body = ($entries | ForEach-Object { $_.Text }) -join "`r`n"
    return "---`r`n# 生成指令:`r`n# $OneLinerCmd`r`n$body`r`n---"
}

function Merge-AgentFrontmatter {
    param(
        [string]$Content,
        [string]$AgentName
    )

    $templateEntries = Get-TemplateFrontmatterEntries -AgentName $AgentName
    $managedKeys = @($templateEntries | ForEach-Object { $_.Key })

    if ($Content -notmatch '(?s)\A\uFEFF?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n(.*)|\z)') {
        $template = New-AgentTemplate -AgentName $AgentName
        $trimmedBody = $Content.TrimStart("`r", "`n")
        if ($trimmedBody) {
            return "$template`r`n`r`n$trimmedBody"
        }
        return "$template`r`n"
    }

    $existingYaml = $Matches[1]
    $restBody = $Matches[2]

    # 解析現有 frontmatter 的頂層 key 區塊（保留非同名欄位）
    $otherBlocks = [System.Collections.Generic.List[string]]::new()
    $currentKey = $null
    $currentLines = [System.Collections.Generic.List[string]]::new()

    $flushBlock = {
        if ($null -ne $currentKey -and ($managedKeys -notcontains $currentKey)) {
            $blockText = ($currentLines -join "`r`n").TrimEnd("`r", "`n")
            if ($blockText) { $otherBlocks.Add($blockText) }
        }
    }

    foreach ($line in ($existingYaml -split '\r?\n')) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)') {
            & $flushBlock
            $currentKey = $Matches[1]
            $currentLines = [System.Collections.Generic.List[string]]::new()
            $currentLines.Add($line)
        } elseif ($null -ne $currentKey) {
            # 若遇到頂層獨立註解（非縮排且前面已有空行），不併入其他區塊
            $currentLines.Add($line)
        }
    }
    & $flushBlock

    $mergedParts = [System.Collections.Generic.List[string]]::new()
    $mergedParts.Add("# 生成指令:`r`n# $OneLinerCmd")
    foreach ($entry in $templateEntries) {
        if ($entry.Key -eq 'tools') {
            # 先放保留的自訂欄位，再放 tools 清單較整潔
            foreach ($ob in $otherBlocks) { $mergedParts.Add($ob) }
        }
        $mergedParts.Add($entry.Text)
    }

    $mergedYaml = ($mergedParts -join "`r`n").TrimEnd("`r", "`n")
    return "---`r`n$mergedYaml`r`n---`r`n$restBody"
}

# 1. 掃描 skills
$skills = @()
if (Test-Path $SkillsDir) {
    $skillFiles = Get-ChildItem -Path $SkillsDir -Filter "SKILL.md" -Recurse -Depth 2 | Sort-Object FullName
    foreach ($file in $skillFiles) {
        $parsed = Parse-SkillFrontmatter -SkillMdPath $file.FullName
        if ($parsed) {
            $skills += $parsed
        }
    }
}

# 1.5 冪等鏡像 skills → 全域原生掃描路徑 (~/.gemini/config/skills)
# 根據官方文檔與實測：agy 原生僅掃描工作區 .agents/skills 與全域 ~/.gemini/config/skills 等位置，
# HOME 層級的 ~\.agents\skills 不在掃描範圍；且掃描器不跟隨 junction（實測驗證），故以位元組級複製鏡像。
# 僅鏡像「頂層即含 SKILL.md」的資料夾，對齊 native 掃描結構 skills/*/SKILL.md；
# 深層 SKILL.md 連鏡了也不會被 native glob 發現，刻意不鏡像，僅列於注入區塊供 read_file 使用。
# 管理範圍僅限同名：同名資料夾一律刪除重抄（以來源為準；重抄中途失敗的殘缺鏡像下次執行亦同機制自癒），
# config/skills 內其餘資料夾（如使用者自建 skill）完全不動。
# 來源端已刪除的 skill 其鏡像因無法與使用者自建內容區分，刻意不自動清理，需自行移除。
$mirroredNames = [System.Collections.Generic.List[string]]::new()
if (Test-Path $SkillsDir) {
    $skillDirs = @(Get-ChildItem -LiteralPath $SkillsDir -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "SKILL.md") -PathType Leaf })
    if ($skillDirs.Count -gt 0 -and -not $DryRun -and -not (Test-Path $GlobalSkillsDir)) {
        New-Item -ItemType Directory -Path $GlobalSkillsDir -Force | Out-Null
    }
    foreach ($dir in $skillDirs) {
        $dest = Join-Path $GlobalSkillsDir $dir.Name
        if (-not $DryRun) {
            if (Test-Path -LiteralPath $dest) {
                Remove-Item -LiteralPath $dest -Recurse -Force
            }
            Copy-Item -LiteralPath $dir.FullName -Destination $GlobalSkillsDir -Recurse -Force
        }
        $mirroredNames.Add($dir.Name)
    }
    if ($DryRun) {
        Write-Host "[DryRun] 鏡像預覽 ($SkillsDir -> $GlobalSkillsDir) 將鏡像(同名覆蓋): $($mirroredNames -join ', ')" -ForegroundColor Yellow
    } elseif ($mirroredNames.Count -gt 0) {
        Write-Host "[OK]已鏡像到 $GlobalSkillsDir (同名覆蓋): $($mirroredNames -join ', ')" -ForegroundColor Green
    }
}
# 2. 組裝注入區塊（直接填入實際絕對路徑，避免 excludeDefaultComponents: true 缺少 <user_information> 時 read_file 因相對路徑失敗）
$sb = [System.Text.StringBuilder]::new()
[void]$sb.AppendLine('<conversation_transcript>')
[void]$sb.AppendLine("Transcripts are located directly at ``$AppDataDir\brain\<conversation-id>\.system_generated\logs\transcript.jsonl``.")
[void]$sb.AppendLine('- When a user mentions `@[conversation:"..."]`, find its `Conversation ID` in `<ADDITIONAL_METADATA>`.')
[void]$sb.AppendLine('- Start with `transcript.jsonl` (compact). When `truncated_fields` is present, read only the specific corresponding line in `transcript_full.jsonl`.')
[void]$sb.AppendLine('</conversation_transcript>')
[void]$sb.AppendLine('')
[void]$sb.AppendLine('<skills>')
[void]$sb.AppendLine('If a skill below is relevant to your current task, read its `SKILL.md` with `read_file` before proceeding:')
foreach ($s in $skills) {
    [void]$sb.AppendLine("- $($s.Name) ($($s.Path)): $($s.Description)")
}
[void]$sb.Append('</skills>')

$block = $sb.ToString()

if ($DryRun) {
    Write-Host "=== DryRun: 預覽注入內容 ===" -ForegroundColor Cyan
    Write-Host $block
}

# 3. 建立或冪等更新目標 agent .md
if (-not $DryRun -and -not (Test-Path $AgentsDir)) {
    New-Item -ItemType Directory -Path $AgentsDir -Force | Out-Null
}

$blockPattern = '(?s)<conversation_transcript>.*?</conversation_transcript>\s*<skills>.*?</skills>'
$transcriptOnlyPattern = '(?s)\s*<conversation_transcript>.*?</conversation_transcript>'
$skillsOnlyPattern = '(?s)\s*<skills>.*?</skills>'
$legacyPattern = '(?s)<!-- BEGIN_AUTO_INJECTED_CONTEXT -->.*?<!-- END_AUTO_INJECTED_CONTEXT -->'

foreach ($agentName in ($Agents | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
    $targetPath = Join-Path $AgentsDir "$agentName.md"

    if (-not (Test-Path $targetPath)) {
        $template = New-AgentTemplate -AgentName $agentName
        $updated = "$template`r`n`r`n$block`r`n"
        $action = "已自動建立新代理並寫入注入區塊"
        if ($DryRun) {
            Write-Host "=== DryRun: 新建檔案預覽 ($targetPath) ===" -ForegroundColor Cyan
            Write-Host $updated
        }
    } else {
        $content = Get-Content -Path $targetPath -Raw -Encoding UTF8
        $mergedContent = Merge-AgentFrontmatter -Content $content -AgentName $agentName
        if ($mergedContent -match $legacyPattern) {
            $updated = [regex]::Replace($mergedContent, $legacyPattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block })
            $action = "已合併 YAML 並取代舊標記更新注入區塊"
        } elseif ($mergedContent -match $blockPattern) {
            $updated = [regex]::Replace($mergedContent, $blockPattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block })
            $action = "已合併 YAML 並更新既有注入區塊"
        } else {
            # 若僅殘留其中一個標籤，先移除再統一附加，避免重複
            $cleaned = [regex]::Replace($mergedContent, $transcriptOnlyPattern, '')
            $cleaned = [regex]::Replace($cleaned, $skillsOnlyPattern, '')
            $trimmed = $cleaned.TrimEnd("`r", "`n")
            $updated = "$trimmed`r`n`r`n$block`r`n"
            $action = "已合併 YAML 並附加新注入區塊"
        }
        if ($DryRun) {
            Write-Host "=== DryRun: 合併後 Frontmatter 預覽 ($targetPath) ===" -ForegroundColor Cyan
            if ($updated -match '(?s)\A(---\r?\n.*?\r?\n---)') {
                Write-Host $Matches[1]
            }
        }
    }

    if ($DryRun) {
        Write-Host "[DryRun] $targetPath -> $action" -ForegroundColor Yellow
    } else {
        [System.IO.File]::WriteAllText($targetPath, $updated, [System.Text.UTF8Encoding]::new($false))
        Write-Host "[OK] $targetPath -> $action (共載入 $($skills.Count) 個 skills)" -ForegroundColor Green
    }
}
