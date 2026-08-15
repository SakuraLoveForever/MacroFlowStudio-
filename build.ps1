# 用 Continue 而非 Stop：PowerShell 5.1 在 "Stop" 下会把原生命令
# （python.exe）写在 stderr 的任何输出当作错误并中断脚本，而
# PyInstaller 的日志全走 stderr；错误改由 $LASTEXITCODE 显式检查。
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
try {
  & $ProjectPython -m PyInstaller --noconfirm --clean MacroFlowStudio.spec
} finally {
  $env:PYTHONPATH = $PreviousModulePath
}

if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller failed with exit code $LASTEXITCODE"
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
# 注意：不要用 Copy-Item -Recurse -Exclude（PowerShell 5.1 会漏拷大量文件），
# 全量复制后再用 Python 统一清理冗余（__pycache__、paddle/include、*.pyi、*.lib）。
# 注意：paddle/_typing 是运行时真实依赖（paddle.tensor.array 会 from paddle
# import _typing），不能删；paddle/include 只是 C++ 头文件可以删。
foreach ($Name in @("paddle", "paddleocr", "paddlex")) {
  $Source = Join-Path $ProjectDir ".deps\$Name"
  if (-not (Test-Path -LiteralPath $Source)) {
    throw "OCR 组件缺失：$Source"
  }
  Copy-Item -LiteralPath $Source -Destination $OcrTarget -Recurse -Force
}
$ModelsSource = Join-Path $ProjectDir "paddle_models"
if (-not (Test-Path -LiteralPath $ModelsSource)) {
  throw "OCR 组件缺失：$ModelsSource"
}
Copy-Item -LiteralPath $ModelsSource -Destination $OcrTarget -Recurse -Force

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
  if (-not (Test-Path -LiteralPath $Source)) {
    throw "OCR 依赖缺失：$Source"
  }
  Copy-Item -LiteralPath $Source -Destination $OcrTarget -Recurse -Force
  # 兄弟 .libs 目录（pandas.libs/shapely.libs 等装包自身的 DLL 依赖）
  $Libs = Join-Path $ProjectDir ".deps\$Name.libs"
  if (Test-Path -LiteralPath $Libs) {
    Copy-Item -LiteralPath $Libs -Destination $OcrTarget -Recurse -Force
  }
  # 同名 dist-info（importlib.metadata 版本检查用）
  Get-ChildItem -LiteralPath (Join-Path $ProjectDir ".deps") -Directory `
    -Filter "$($Name.Replace('_','-'))-*.dist-info" -ErrorAction SilentlyContinue |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $OcrTarget -Recurse -Force }
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
  if (-not (Test-Path -LiteralPath $Source)) {
    throw "OCR 元数据缺失：$Source"
  }
  Copy-Item -LiteralPath $Source -Destination $OcrTarget -Recurse -Force
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
$OcrCleanupScript -f $OcrTarget | & $ProjectPython -
$ModelMarker = Join-Path $OcrTarget "paddle_models\PP-OCRv5_mobile_det\inference.pdiparams"
if (-not (Test-Path -LiteralPath $ModelMarker)) {
  throw "OCR 模型复制失败：$ModelMarker"
}
$PaddleMarker = Join-Path $OcrTarget "paddle\__init__.py"
if (-not (Test-Path -LiteralPath $PaddleMarker)) {
  throw "OCR 引擎复制失败：$PaddleMarker"
}

Write-Host "Build complete: $ProjectDir\dist\MacroFlowStudio.exe (+ paddle_ocr/ 外置 OCR 组件)"
