# concept.ps1 — 概念板块分析一键脚本（先快速榜单，后慢速深度）
# 用法:
#   .\concept.ps1            # 默认 top 10
#   .\concept.ps1 8          # 指定 top N
param(
    [int]$Top = 10
)

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$ErrorActionPreference = "Stop"

Write-Host "=== 步骤 1/2: 快速抓取概念榜单 (list, 秒级) ===" -ForegroundColor Cyan
python core/cli.py concept --stage list --top $Top
if ($LASTEXITCODE -ne 0) {
    Write-Host "榜单抓取失败，终止。" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== 步骤 2/2: 拉取成分股 + 深度分析 (detail, 慢速) ===" -ForegroundColor Cyan
python core/cli.py concept --stage detail --top $Top
