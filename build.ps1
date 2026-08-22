# 用 Continue 而非 Stop：PowerShell 5.1 在 "Stop" 下会把原生命令
# （python.exe）写在 stderr 的任何输出当作错误并中断脚本，而
# PyInstaller 的日志全走 stderr；错误改由 $LASTEXITCODE 显式检查。
param(
  [switch]$Clean
)

$ErrorActionPreference = "Continue"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir
$ProjectPython = $null
if ((Test-Path -LiteralPath (Join-Path $ProjectDir ".deps")) -and
    (Test-Path -LiteralPath "C:\Python313\python.exe")) {
  $ProjectPython = "C:\Python313\python.exe"
}
if (-not $ProjectPython) {
  $ProjectPython = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $ProjectPython) {
  throw "Python executable not found."
}
$ProjectDependencies = Join-Path $ProjectDir ".deps"
$PreviousModulePath = $env:PYTHONPATH
if (Test-Path -LiteralPath $ProjectDependencies) {
  $env:PYTHONPATH = if ($PreviousModulePath) { "$ProjectDependencies;$PreviousModulePath" } else { $ProjectDependencies }
}

# 体积优化（重复 DLL 去重、paddle 头文件/类型标注过滤、
# 排除 pip/networkx/hf_xet 等运行时不可达依赖）都在
# MacroFlowStudio.spec 中实现，构建必须走 spec，不要改回命令行参数模式
# （命令行参数模式会用参数覆盖本文件同目录的 spec，导致优化丢失）。
$BuildStamp = Join-Path $ProjectDir "build\MacroFlowStudio.inputs.sha256"
$BuildInputPaths = @(
  (Join-Path $ProjectDir "MacroFlowStudio.spec"),
  (Join-Path $ProjectDir "build.ps1")
)
$BuildInputPaths += @(Get-ChildItem -LiteralPath (Join-Path $ProjectDir "src") -File -Recurse |
  Where-Object {
    $_.FullName -notmatch '[\\/]__pycache__([\\/]|$)' -and
    $_.Extension -notin @('.pyc', '.pyo')
  } |
  Sort-Object FullName | Select-Object -ExpandProperty FullName)
$BuildInputLines = foreach ($InputPath in $BuildInputPaths) {
  if (-not (Test-Path -LiteralPath $InputPath)) {
    throw "构建输入缺失：$InputPath"
  }
  $RelativePath = $InputPath.Substring($ProjectDir.Length).TrimStart('\')
  $ContentHash = (Get-FileHash -LiteralPath $InputPath -Algorithm SHA256).Hash
  "$RelativePath`t$ContentHash"
}
$HashProvider = [Security.Cryptography.SHA256]::Create()
try {
  $BuildInputHash = [BitConverter]::ToString(
    $HashProvider.ComputeHash([Text.Encoding]::UTF8.GetBytes(($BuildInputLines -join "`n")))
  ).Replace("-", "")
} finally {
  $HashProvider.Dispose()
}
$ExistingBuildHash = if (Test-Path -LiteralPath $BuildStamp) {
  (Get-Content -LiteralPath $BuildStamp -Raw).Trim()
} else {
  ""
}
$CanReuseExecutable = (-not $Clean) -and
  (Test-Path -LiteralPath (Join-Path $ProjectDir "dist\MacroFlowStudio.exe")) -and
  ($ExistingBuildHash -eq $BuildInputHash)

if ($CanReuseExecutable) {
  Write-Host "PyInstaller skipped: source/spec unchanged (use .\build.ps1 -Clean to force rebuild)."
} else {
  try {
    $PyInstallerArgs = @("--noconfirm")
    if ($Clean) {
      $PyInstallerArgs += "--clean"
    }
    $PyInstallerArgs += "MacroFlowStudio.spec"
    & $ProjectPython -m PyInstaller @PyInstallerArgs
  } finally {
    $env:PYTHONPATH = $PreviousModulePath
  }

  if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
  }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $BuildStamp) | Out-Null
  Set-Content -LiteralPath $BuildStamp -Value $BuildInputHash -NoNewline -Encoding ASCII
}

# Keep the release notes beside the packaged executable in sync with this build.
Copy-Item -LiteralPath (Join-Path $ProjectDir "README.md") `
  -Destination (Join-Path $ProjectDir "dist\README.md") -Force
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "dist\README.md"))) {
  throw "Failed to copy README.md to dist\README.md"
}
Copy-Item -LiteralPath (Join-Path $ProjectDir "CHANGELOG.md") `
  -Destination (Join-Path $ProjectDir "dist\CHANGELOG.md") -Force
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "dist\CHANGELOG.md"))) {
  throw "Failed to copy CHANGELOG.md to dist\CHANGELOG.md"
}

# OCR 引擎外置（v1.87.0 起）：paddle 全家与模型不打进 exe，复制到 exe 同目录的
# paddle_ocr/，首次使用 OCR 时由 ocr.py 按需加载，主 exe 因此小很多。
$OcrTarget = Join-Path $ProjectDir "dist\paddle_ocr"
$OcrSources = @(
  (Join-Path $ProjectDir ".deps\paddle"),
  (Join-Path $ProjectDir ".deps\paddleocr"),
  (Join-Path $ProjectDir ".deps\paddlex"),
  (Join-Path $ProjectDir "paddle_models")
)
New-Item -ItemType Directory -Force -Path $OcrTarget | Out-Null
# 使用 robocopy 增量同步，避免每次构建都重新写入约 0.5GB 的 OCR 组件。
# /MIR 只作用于 dist\paddle_ocr 下的对应子目录，确保源目录删除的文件也会清理。
# 注意：paddle/_typing 是运行时真实依赖（paddle.tensor.array 会 from paddle
# import _typing），不能删；paddle/include 只是 C++ 头文件可以删。
$OcrChanged = $false
function Sync-OcrDirectory {
  param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination
  )
  if (-not (Test-Path -LiteralPath $Source)) {
    throw "OCR 组件缺失：$Source"
  }
  New-Item -ItemType Directory -Force -Path $Destination | Out-Null
  & robocopy.exe $Source $Destination /MIR /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS /NP | Out-Null
  $RobocopyExitCode = $LASTEXITCODE
  if ($RobocopyExitCode -gt 7) {
    throw "OCR 组件同步失败：$Source -> $Destination（robocopy $RobocopyExitCode）"
  }
  if ($RobocopyExitCode -ne 0) {
    $script:OcrChanged = $true
  }
}
foreach ($Name in @("paddle", "paddleocr", "paddlex")) {
  $Source = Join-Path $ProjectDir ".deps\$Name"
  Sync-OcrDirectory -Source $Source -Destination (Join-Path $OcrTarget $Name)
}
$ModelsSource = Join-Path $ProjectDir "paddle_models"
Sync-OcrDirectory -Source $ModelsSource -Destination (Join-Path $OcrTarget "paddle_models")

# v1.87.1 起：paddleocr 的运行时第三方依赖（colorlog 等）不在 exe 内，
# 必须与外置 OCR 引擎一起复制。列表由 build/ocr_deps_setup.py 的
# modulegraph 闭包分析得出；若 .deps 依赖升级，重新跑该分析脚本更新列表。
$OcrDeps = @(
  'Crypto','_distutils_hack','aiohappyeyeballs','aiohttp','aiosignal',
  'aistudio_sdk','annotated_types','anyio','attr','baidubce','bidi','certifi',
  'chardet','click','colorama','colorlog','cpuinfo','crc32c','dateutil',
  'filelock','frozenlist','fsspec','future','google','h11','httpcore','httpx',
  'huggingface_hub','idna','imagesize','modelscope','modelscope_hub','multidict',
  'opt_einsum','packaging','pandas','prettytable','propcache','pyclipper',
  'pydantic','pydantic_core','pypdfium2','pypdfium2_cfg','pypdfium2_raw',
  'requests','ruamel','safetensors','setuptools','shapely','tqdm',
  'typing_inspection','tzdata','urllib3','wcwidth','yarl'
)
foreach ($Name in $OcrDeps) {
  $Source = Join-Path $ProjectDir ".deps\$Name"
  Sync-OcrDirectory -Source $Source -Destination (Join-Path $OcrTarget $Name)
  # 兄弟 .libs 目录（pandas.libs/shapely.libs 等装包自身的 DLL 依赖）
  $Libs = Join-Path $ProjectDir ".deps\$Name.libs"
  if (Test-Path -LiteralPath $Libs) {
    Sync-OcrDirectory -Source $Libs -Destination (Join-Path $OcrTarget "$Name.libs")
  }
  # 同名 dist-info（importlib.metadata 版本检查用）
  Get-ChildItem -LiteralPath (Join-Path $ProjectDir ".deps") -Directory `
    -Filter "$($Name.Replace('_','-'))-*.dist-info" -ErrorAction SilentlyContinue |
    ForEach-Object {
      Sync-OcrDirectory -Source $_.FullName -Destination (Join-Path $OcrTarget $_.Name)
    }
}
# 固定元数据清单：目录名与包名不一致的（python_bidi/attrs/pycryptodome 等）
# 和 paddlex 的 ocr-core 附加依赖检查项（pyclipper/pypdfium2/shapely）。
$OcrMeta = @(
  'python_bidi-0.6.11.dist-info','attrs-26.1.0.dist-info',
  'pyclipper-1.4.0.dist-info','pypdfium2-5.12.1.dist-info',
  'shapely-2.1.2.dist-info','imagesize-2.0.0.dist-info',
  'opencv_contrib_python-4.10.0.84.dist-info',
  'paddleocr-3.7.0.dist-info','paddlepaddle-3.3.1.dist-info',
  'paddlex-3.7.2.dist-info'
)
foreach ($Meta in $OcrMeta) {
  $Source = Join-Path $ProjectDir ".deps\$Meta"
  Sync-OcrDirectory -Source $Source -Destination (Join-Path $OcrTarget $Meta)
}
$OcrCleanupScript = @'
import os
import shutil
root = r"{0}"
for junk in ("paddle/include",):
    path = os.path.join(root, junk)
    if os.path.isdir(path):
        shutil.rmtree(path)
for dirpath, dirnames, filenames in os.walk(root, topdown=False):
    for name in dirnames:
        if name == "__pycache__":
            shutil.rmtree(os.path.join(dirpath, name))
    for name in filenames:
        if name.endswith((".pyi", ".lib")):
            os.remove(os.path.join(dirpath, name))
'@
if ($OcrChanged) {
  $OcrCleanupScript -f $OcrTarget | & $ProjectPython -
}
$ModelMarker = Join-Path $OcrTarget "paddle_models\PP-OCRv5_mobile_det\inference.pdiparams"
if (-not (Test-Path -LiteralPath $ModelMarker)) {
  throw "OCR 模型复制失败：$ModelMarker"
}
$PaddleMarker = Join-Path $OcrTarget "paddle\__init__.py"
if (-not (Test-Path -LiteralPath $PaddleMarker)) {
  throw "OCR 引擎复制失败：$PaddleMarker"
}

Write-Host "Build complete: $ProjectDir\dist\MacroFlowStudio.exe (+ paddle_ocr/ 外置 OCR 组件)"
