# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  注册/卸载/查看「每日自检与自我进化」定时任务 —— StockSelfCheck

  独立 Windows 任务计划(不依赖调度 daemon 生死): 每个交易日 12:00 运行
  python scripts/self_check.py, 自检推送逻辑/环境、K线联网新鲜度、热点板块及
  成分股跟踪、股票池分析, 并在安全范围内自动修复 + 企微推送报告。

  注意: 节假日由 self_check.py 内部 is_trading_day 判断并提前退出(不推送),
        因此这里用"周一~周五 12:00"即可, 无需在计划程序里维护节假日表。

.EXAMPLE
  .\install_selfcheck.ps1 install     # 注册并立即试运行一次
  .\install_selfcheck.ps1 uninstall   # 卸载
  .\install_selfcheck.ps1 status      # 查看状态
  .\install_selfcheck.ps1 run         # 仅前台运行一次(调试)
#>
param(
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$SelfCheckPy = Join-Path $Root "scripts\self_check.py"

# 解析 python 可执行文件
$PythonExe = $null
try { $PythonExe = (Get-Command python -ErrorAction Stop).Source } catch { }
if (-not $PythonExe) {
    try { $PythonExe = (Get-Command py -ErrorAction Stop).Source } catch { }
}
if (-not $PythonExe) {
    Write-Error "未找到 python / py 可执行文件, 请先安装 Python 并加入 PATH"
    exit 1
}

$TaskName = "StockSelfCheck"

switch ($Action) {
    "install" {
        $Action2 = New-ScheduledTaskAction `
            -Execute $PythonExe `
            -Argument "scripts\self_check.py" `
            -WorkingDirectory $Root
        # 每个交易日(周一~周五) 12:00 触发
        $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "12:00"
        $Settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -RunOnlyIfNetworkAvailable `
            -StartWhenAvailable `
            -MultipleInstances IgnoreNew

        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $TaskName -Action $Action2 -Trigger $Trigger -Settings $Settings -Force | Out-Null
        Write-Host "✓ 已注册定时任务: $TaskName (每个交易日 12:00)"
        Write-Host "  命令: $PythonExe scripts\self_check.py"
        Write-Host "  工作目录: $Root"

        # 立即试运行一次(验证脚本可跑通)
        Write-Host "`n▶ 立即试运行一次(今日首次自检)..."
        & $PythonExe (Join-Path $Root "scripts\self_check.py")
        Write-Host "`n✓ 安装并试运行完成"
    }
    "uninstall" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "✓ 已卸载任务: $TaskName"
    }
    "status" {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $t) { Write-Host "任务不存在: $TaskName (请先 install)"; exit 0 }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "任务: $TaskName"
        Write-Host "  状态: $($t.State)"
        Write-Host "  上次运行: $($info.LastRunTime)"
        Write-Host "  上次结果: $($info.LastTaskResult)"
    }
    "run" {
        Write-Host "前台运行一次(调试)..."
        & $PythonExe (Join-Path $Root "scripts\self_check.py")
    }
    default {
        Write-Host "用法: .\install_selfcheck.ps1 [install|uninstall|status|run]"
    }
}
