# ============================================================
# MacroFlow Studio — GitHub Release 发布脚本
# 用法：.\release.ps1 -Version 1.0.0
# 前置：已 git commit & git push 到 GitHub，gh CLI 已安装并登录
#   winget install GitHub.cli
#   gh auth login
# 说明：按 GitHub 要求为每个版本创建 vX.Y.Z 标签 + Release，
#   附带对应 zip 安装包与 release-notes/vX.Y.Z.md 更新说明。
# ============================================================
param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$Tag = "v$Version"
$Zip = "MacroFlowStudio_${Version}_win64.zip"
$Notes = "release-notes/$Tag.md"

if (-not (Test-Path -LiteralPath $Zip)) {
    throw "缺少安装包：$Zip（请先打包）"
}
if (-not (Test-Path -LiteralPath $Notes)) {
    throw "缺少发布说明：$Notes"
}

$gh = Get-Command gh -ErrorAction SilentlyContinue
if (-not $gh) {
    throw "未安装 GitHub CLI。请先执行：winget install GitHub.cli 然后 gh auth login"
}

# 1) 创建带注释的 git 标签（指向当前提交）
if (-not (git tag -l $Tag)) {
    git tag -a $Tag -m "MacroFlow Studio $Tag"
    git push origin $Tag
    Write-Host "已创建并推送标签 $Tag"
} else {
    Write-Host "标签 $Tag 已存在，跳过创建"
}

# 2) 创建 GitHub Release 并上传安装包（GitHub 要求：tag + 说明 + 资产）
gh release create $Tag $Zip `
    --title "MacroFlow Studio $Tag" `
    --notes-file $Notes

Write-Host "发布完成：https://github.com/<owner>/<repo>/releases/tag/$Tag"
