# -*- coding: utf-8 -*-
<#
.SYNOPSIS
  注册/卸载/查看「唯一」常驻调度任务 —— StockAnalysisScheduler

  本脚本只向 Windows 任务计划注册「一个」任务: 开机后常驻启动
  python scripts/scheduler.py --daemon。真正的定时计划全部写在 scheduler.py 的
  TASKS 表里(单一可信源), 不在这里分散定义, 便于统一维护。

.EXAMPLE
  .\install_scheduler.ps1 install     # 注册并立即启动常驻调度
  .\install_scheduler.ps1 uninstall   # 卸载
  .\install_scheduler.ps1 status      # 查看状态
  .\install_scheduler.ps1 run         # 仅运行一次(前台, 用于调试)
#>
param(
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$SchedulerPy = Join-Path $Root "scripts\scheduler.py"

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

$TaskName = "StockAnalysisScheduler"

switch ($Action) {
    "install" {
        # 常驻: 不限制执行时长, 崩溃后 1 分钟内重启(最多 3 次)
        $Action2 = New-ScheduledTaskAction `
            -Execute $PythonExe `
            -Argument "scripts\scheduler.py --daemon" `
            -WorkingDirectory $Root
        $Trigger = New-ScheduledTaskTrigger -AtLogOn
        $Settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -StartWhenAvailable `
            -RunOnlyIfNetworkAvailable:$false

        # 若已存在则先删后建
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $TaskName -Action $Action2 -Trigger $Trigger -Settings $Settings -Force | Out-Null
        Write-Host "✓ 已注册常驻任务: $TaskName"
        Write-Host "  命令: $PythonExe scripts\scheduler.py --daemon"
        Write-Host "  工作目录: $Root"
        Write-Host "  触发: 登录时启动, 崩溃自动重启"

        # 立即启动一次
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "✓ 已尝试启动(可在任务计划程序查看运行状态)"
        Write-Host ""
        Write-Host "查看计划内容:  python scripts\scheduler.py --list"
        Write-Host "查看今日任务:  python scripts\scheduler.py --dry-run"
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
        & $PythonExe (Join-Path $Root "scripts\scheduler.py") --daemon
    }
    default {
        Write-Host "用法: .\install_scheduler.ps1 [install|uninstall|status|run]"
    }
}
