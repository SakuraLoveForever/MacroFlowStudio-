"""统计 test_core.py 的 patch 命名空间与 import 引用面（用于评估包化成本）。"""
import re
from collections import Counter

text = open("test_core.py", encoding="utf-8").read()
patterns = re.findall(r"""patch\(['"]([a-z_]+)\.""", text)
print("=== patch 命名空间统计 ===")
for name, count in sorted(Counter(patterns).items(), key=lambda kv: -kv[1]):
    print(f"{name:12s} {count}")

print("\n=== from/import 顶层模块 ===")
for line in sorted(set(re.findall(r"^(?:from|import) ([a-z_]+)", text, re.M))):
    print(line)

print("\n=== 各源码文件被 import 的模块 ===")
for fn in ["app.py", "dialogs.py", "player.py", "storage.py", "models.py", "wininput.py",
           "input_guard.py", "recorder.py", "rawinput.py", "ocr.py", "image_match.py",
           "detect_overlay.py", "alerts.py"]:
    src = open(fn, encoding="utf-8").read()
    imports = sorted(set(re.findall(r"^(?:from|import) ([a-z_]+)", src, re.M)))
    print(f"{fn:16s} -> {imports}")
