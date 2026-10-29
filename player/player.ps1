# player.ps1 —— 可见窗口播放器（给 OBS「应用音频采集」当捕获目标）
#
# ## 为什么必须是"可见窗口"（实现原理）
#
# OBS 的「应用音频采集 / 窗口音频采集」抓的是**进程的音频会话**，而 Windows 上
# 一个进程只有在**创建了窗口**并真正输出音频时，才会出现在那个列表里。
#
# 所以播放器不能是"后台静默播放"：
#   - 必须有窗口（可以有边框，也可以很小）
#   - 必须由**同一个进程**播放（外部 ffplay 也行，但要由这个进程托管子进程）
#   - 窗口标题要稳定、可配置，因为 OBS 是按标题/进程选的
#
# 这也是为什么本文件用 WinForms 而不是 `[console]::beep` 或后台任务：
# **窗口就是那个被采集的"锚点"。**
#
# ## 队列约定
#
#   队列目录/<序号>_<毫秒>.wav      要播放的音频
#   队列目录/<同名>.txt             对应的文字（可选，用于显示"现在在念什么"）
#   队列目录/_preempt               抢占标记（bridge 写入；本进程看到就清队并停当前）
#
# 用"文件名排序"决定播放顺序，不用时间戳——因为 bridge 生成的序号是单调递增的。

param(
    [string]$QueueDir = (Join-Path $PSScriptRoot '..\播放队列'),
    [string]$OutDir   = (Join-Path $PSScriptRoot '..\输出'),
    [string]$Title    = 'TTS-PLAYER',
    [int]$PollMs      = 150,
    [string]$Ffplay   = ''      # 留空则用 System.Media.SoundPlayer（只吃 PCM wav）
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

New-Item -ItemType Directory -Force -Path $QueueDir, $OutDir | Out-Null
$LogFile = Join-Path $OutDir '播放器日志.txt'

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# ---------------- 窗口 ----------------
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Size = New-Object System.Drawing.Size(460, 170)
$form.StartPosition = 'Manual'
$form.Location = New-Object System.Drawing.Point(40, 40)
$form.BackColor = [System.Drawing.Color]::FromArgb(24, 22, 32)
$form.FormBorderStyle = 'FixedToolWindow'   # 小窗即可，标题栏保留（OBS 按标题识别）
$form.ShowInTaskbar = $true

$lbl = New-Object System.Windows.Forms.Label
$lbl.Dock = 'Fill'
$lbl.ForeColor = [System.Drawing.Color]::FromArgb(235, 235, 245)
$lbl.Font = New-Object System.Drawing.Font('Microsoft YaHei', 10)
$lbl.TextAlign = 'MiddleCenter'
$lbl.Text = "$Title`r`n等待中…"
$form.Controls.Add($lbl)

# ---------------- 播放 ----------------
$player = $null
if (-not $Ffplay) { $player = New-Object System.Media.SoundPlayer }

function Play-File($path) {
    if ($Ffplay) {
        & $Ffplay -nodisp -autoexit -loglevel quiet $path 2>$null
    } else {
        $player.SoundLocation = $path
        $player.PlaySync()        # 同步：一句话播完再取下一句，天然串行
    }
}

$form.Add_Shown({
    Write-Log "播放器启动 title=$Title queue=$QueueDir"
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = $PollMs
    $timer.Add_Tick({
        # 抢占：bridge 写了标记 → 清空积压并停当前
        $mark = Join-Path $QueueDir '_preempt'
        if (Test-Path $mark) {
            Remove-Item $mark -Force -ErrorAction SilentlyContinue
            Get-ChildItem $QueueDir -Filter '*.wav' -ErrorAction SilentlyContinue |
                Remove-Item -Force -ErrorAction SilentlyContinue
            try { if ($Ffplay) { Get-Process ffplay -ErrorAction SilentlyContinue | Stop-Process -Force } else { $player.Stop() } } catch {}
            $lbl.Text = "$Title`r`n已抢占，队列清空"
            Write-Log '收到抢占标记，已清队'
            return
        }

        $next = Get-ChildItem $QueueDir -Filter '*.wav' -ErrorAction SilentlyContinue |
                Sort-Object Name | Select-Object -First 1
        if (-not $next) {
            if ($lbl.Text -notlike "*等待中*") { $lbl.Text = "$Title`r`n等待中…" }
            return
        }

        $txtPath = "$($next.FullName).txt"
        $what = if (Test-Path $txtPath) { (Get-Content $txtPath -Raw -Encoding UTF8).Trim() } else { '(无文本)' }
        if ($what.Length -gt 60) { $what = $what.Substring(0, 60) + '…' }
        $lbl.Text = "$Title`r`n▶ $what"
        $form.Refresh()

        $sw = [Diagnostics.Stopwatch]::StartNew()
        try {
            Play-File $next.FullName
            Write-Log ("播放 $($next.Name) 用时 $([int]$sw.Elapsed.TotalMilliseconds) ms")
        } catch {
            Write-Log "播放失败 $($next.Name): $_"
        } finally {
            $sw.Stop()
            Remove-Item $next.FullName -Force -ErrorAction SilentlyContinue
            Remove-Item $txtPath -Force -ErrorAction SilentlyContinue
        }
    })
    $timer.Start()
})

$form.Add_FormClosing({ Write-Log '播放器关闭' })
[void]$form.ShowDialog()
