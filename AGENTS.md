# AGENTS.md

## Engineering principles

- Do not preserve backward compatibility. Remove obsolete paths instead of adding compatibility layers, fallbacks, or migrations.
- Choose the simplest implementation that fully meets the current requirements. Avoid speculative abstractions, configuration, and indirection.
- Grow the system in layers. Start from the smallest version that works end to end, and add each new capability on top of a product that already works. Never trade a working product for unfinished complexity.
- Keep components modular and concerns clearly separated.
- Prefer established, well-maintained libraries when they reduce overall complexity or improve reliability. Do not reimplement common functionality without a clear reason.
- Lean on the dependencies already in the project before writing your own implementation or adding packages. Do not assume a library lacks a capability without checking its documentation and types.
- Make architectural decisions for the long term. Do not accept a stopgap that only works for now and is meant to be replaced later.

## 构建与打包规则

- 每次修改代码后必须立即重新构建 exe（`.\build.ps1`），确保 `dist\MacroFlowStudio.exe` 与最新代码一致；不得只改源码不构建。
- zip 只在重大修改时打包：`.\pack.ps1`（将 `dist\MacroFlowStudio.exe` + `dist\paddle_ocr\` + README.md + CHANGELOG.md 打包为项目根目录下的 `MacroFlowStudio_latest_win64.zip`），打包内容缺失时脚本会报错。重大修改指功能级大改、需要分发/交付测试的改动，或用户明确要求打包。
- 普通修改只更新 exe，不重新打包 zip；打包后不得把旧 zip 当作最新包分发。
- 任何执行 `.\pack.ps1` 的打包流程，必须先执行并成功完成 `.\build.ps1`，确认 dist 中 exe 与外置 OCR 组件来自最新源码后，才能压缩 zip。

## 模型测试规则

- 如果当前模型是 DeepSeek，不要执行冒烟测试，也不要为了冒烟测试启动应用或打包产物。
- 一律不执行可见界面检查，不得为了检查而操作、点击、截图或目视验证应用界面。
- 一律不得启动软件界面进行测试；所有测试必须通过后台命令完成，例如单元测试、编译检查、打包和产物静态校验。
- 如果当前模型是 ChatGPT，可根据任务需要执行不涉及可见界面操作的单元测试、编译检查、打包和产物静态校验。
- 除上述差异外，仍应按用户要求和改动风险选择必要的非冒烟验证。
