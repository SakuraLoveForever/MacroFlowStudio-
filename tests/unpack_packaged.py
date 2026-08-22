"""打包版文件布局（根目录重复 DLL 移除 + PYZ 排除项）下的真实 OCR 测试。

用法: python unpack_packaged.py dist/MacroFlowStudio.exe  （解包到 _meitest）
"""
import importlib.util
import marshal
import os
import shutil
import struct
import sys
import zlib

sys.path.insert(0, ".deps")
from PyInstaller.archive.readers import CArchiveReader  # noqa: E402

EXE = sys.argv[1] if len(sys.argv) > 1 else "dist/MacroFlowStudio.exe"
OUT = "_meitest"

if os.path.exists(OUT):
    shutil.rmtree(OUT)

a = CArchiveReader(EXE)

# 1. PYZ 纯模块 → 包转 __init__.pyc
raw = a.extract("PYZ.pyz")
if isinstance(raw, tuple):
    raw = raw[0]
off = struct.unpack("!i", raw[8:12])[0]
toc = dict(marshal.loads(raw[off:]))
magic = importlib.util.MAGIC_NUMBER
header = magic + struct.pack("<III", 0, 0, 0)
PYZ_ITEM_PKG, PYZ_ITEM_NSPKG = 1, 3
n = 0
for mod, (typecode, o, clen) in toc.items():
    if typecode == PYZ_ITEM_NSPKG:
        continue
    code = zlib.decompress(raw[o:o + clen])
    if typecode == PYZ_ITEM_PKG:
        rel = mod.replace(".", os.sep) + os.sep + "__init__"
    else:
        rel = mod.replace(".", os.sep)
    path = os.path.join(OUT, rel + ".pyc")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(header + code)
    n += 1

# 2. CArchive 二进制/数据
b = 0
for name in a.toc:
    if name == "PYZ.pyz":
        continue
    data = a.extract(name)
    path = os.path.join(OUT, name.replace("\\", "/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    b += 1
print(f"OK：PYZ {n} 模块 + CArchive {b} 文件 → {OUT}")
