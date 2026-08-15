# AGENTS.md

## Engineering principles

- Do not preserve backward compatibility. Remove obsolete paths instead of adding compatibility layers, fallbacks, or migrations.
- Choose the simplest implementation that fully meets the current requirements. Avoid speculative abstractions, configuration, and indirection.
- Grow the system in layers. Start from the smallest version that works end to end, and add each new capability on top of a product that already works. Never trade a working product for unfinished complexity.
- Keep components modular and concerns clearly separated.
- Prefer established, well-maintained libraries when they reduce overall complexity or improve reliability. Do not reimplement common functionality without a clear reason.
- Lean on the dependencies already in the project before writing your own implementation or adding packages. Do not assume a library lacks a capability without checking its documentation and types.
- Make architectural decisions for the long term. Do not accept a stopgap that only works for now and is meant to be replaced later.

## 模型测试规则

- 如果当前模型是 DeepSeek，不要执行冒烟测试，也不要为了冒烟测试启动应用或打包产物。
- 一律不执行可见界面检查，不得为了检查而操作、点击、截图或目视验证应用界面。
- 如果当前模型是 ChatGPT，可根据任务需要执行不涉及可见界面操作的单元测试、编译检查、打包和产物静态校验。
- 除上述差异外，仍应按用户要求和改动风险选择必要的非冒烟验证。
