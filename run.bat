@echo off
setlocal
cd /d "%~dp0"
REM =======================================================
REM  MacroFlow Studio 源码启动器
REM  用构建环境（C:\Python313 + .deps）启动 app.py。
REM  默认 Python（3.12）缺少 PaddleOCR 依赖，"识别文字"会报错，
REM  不要直接用 python app.py 运行。
REM =======================================================
set "PYTHONPATH=%CD%\.deps;%PYTHONPATH%"
if exist "C:\Python313\python.exe" (
  "C:\Python313\python.exe" app.py
) else (
  python app.py
)
if errorlevel 1 pause
endlocal
