@echo off
setlocal
cd /d "%~dp0"
REM =======================================================
REM  MacroFlow Studio 源码启动器
REM  用构建环境（C:\Python313 + .deps + src）启动 macroflow 包。
REM  默认 Python（3.12）缺少 PaddleOCR 依赖，"识别文字"会报错，
REM  不要直接用 python -m macroflow.ui.app 运行（除非装了 .deps）。
REM =======================================================
set "PYTHONPATH=%CD%\.deps;%CD%\src;%PYTHONPATH%"
if exist "C:\Python313\python.exe" (
  "C:\Python313\python.exe" -m macroflow.ui.app
) else (
  python -m macroflow.ui.app
)
if errorlevel 1 pause
endlocal
