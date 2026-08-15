# ============================================================
# MacroFlow Studio — 最新安装包打包脚本
# 用法：.\pack.ps1
# 前置：已运行 .\build.ps1 产出 dist\MacroFlowStudio.exe
# 产出：MacroFlowStudio_latest_win64.zip
#   （exe + 外置 OCR 组件 paddle_ocr/ + README.md + CHANGELOG.md，
#     与 vX.Y.Z 发布包结构一致；不含个人数据：脚本/工作流/截图/日志）
# 规则：每次重新构建 exe 后必须重新打包，保持 zip 与最新 exe 同步。
# ============================================================
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$Exe = Join-Path $ProjectDir "dist\MacroFlowStudio.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "缺少构建产物：$Exe（请先运行 .\build.ps1）"
}

$Zip = Join-Path $ProjectDir "MacroFlowStudio_latest_win64.zip"
$Stage = Join-Path $ProjectDir "dist"

$Items = @(
    (Join-Path $Stage "MacroFlowStudio.exe"),
    (Join-Path $Stage "paddle_ocr"),
    (Join-Path $Stage "README.md"),
    (Join-Path $Stage "CHANGELOG.md")
)
foreach ($Item in $Items) {
    if (-not (Test-Path -LiteralPath $Item)) {
        throw "打包内容缺失：$Item"
    }
}

# -Force 覆盖旧包；多 Path 条目保持 zip 根目录结构（exe/文档在根，paddle_ocr/ 为目录）
Compress-Archive -LiteralPath $Items -DestinationPath $Zip -CompressionLevel Optimal -Force

Write-Host "打包完成：$Zip"

