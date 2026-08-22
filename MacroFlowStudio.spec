# -*- mode: python ; coding: utf-8 -*-
# 注意：本 spec 包含体积优化逻辑（重复 DLL 去重、排除 pip/networkx/hf_xet），
# PyInstaller 命令行参数模式会覆盖同目录 spec 文件，构建必须走 build.ps1
# （spec 模式）。
#
# 结构说明（v1.87.0 起）：
#   - 主 exe 不再包含 OCR 引擎。paddle/paddleocr/paddlex 及 paddle_models/
#     由 build.ps1 复制到 dist/paddle_ocr/（exe 旁边），ocr.py 在首次使用
#     OCR 时把它加入 sys.path 按需加载：exe 体积约 253MB -> 约 70MB，
#     启动不再解压全部内容，OCR 功能不变。
#   - 移除了仅 paddle 家族使用的元数据依赖（shapely/pyclipper/imagesize/
#     pypdfium2/python-bidi），PIL 被 app/dialogs/ttkbootstrap 直接使用保留。
import os
import sys
from pathlib import Path

import pefile
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('ttkbootstrap')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pynput')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pystray')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# ---------- OCR 运行期缺失的 stdlib 补齐 ----------
# paddle 闭包（build/ocr_closure_modules.txt）引用、但当前 exe 的
# PYZ/base_library.zip 里没有的 stdlib 模块（如 http.cookies——主程序从不
# import 它，PyInstaller 分析不到），全部作为 hiddenimports 打进新 exe：
# 否则打包版 OCR 首次使用会报 "No module named 'http.cookies'"。
def _ocr_missing_stdlib_hiddenimports():
    from pathlib import Path

    # PyInstaller 6.x 的 spec 执行环境不提供 __file__，用 SPECPATH（spec 所在目录）。
    spec_dir = Path(SPECPATH).resolve()
    closure_file = spec_dir / "build" / "ocr_closure_modules.txt"
    if not closure_file.is_file():
        return ["http.cookies"]
    closure = set(closure_file.read_text(encoding="utf-8").split())
    # 返回闭包引用的全部 stdlib 模块（幂等，不依赖旧 exe 状态）：主程序
    # 从不 import 它们（如 http.cookies/unittest.mock），PyInstaller 自然
    # 分析不会打包；每次构建都是全新 Analysis，缺失计算必须以闭包为基准
    # 而非旧 exe——否则补丁会随旧 exe 状态交替丢失（657↔741 回归）。
    return sorted(n for n in closure if n.split(".")[0] in sys.stdlib_module_names)


hiddenimports += _ocr_missing_stdlib_hiddenimports()

# ---------- 体积精简（不影响功能）----------
# cv2 视频解码 DLL 与 OpenCV 自带级联分类器数据：软件只用图像匹配
# （模板查找/截图），从不打开视频或做人脸检测。
binaries = [
    (s, d) for s, d in binaries if 'opencv_videoio_ffmpeg' not in os.path.basename(d).lower()
]
datas = [(s, d) for s, d in datas if not d.replace('\\', '/').startswith('cv2/data/')]


a = Analysis(
    ['src/macroflow/ui/app.py'],
    pathex=[str(Path(SPECPATH).resolve() / 'src')],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'pip', 'networkx', 'hf_xet',
        # OCR 引擎外置：paddle 全家不在 exe 内，运行时由 ocr.py 从 exe 同目录
        # 的 paddle_ocr/ 按需加载（否则模块分析会把函数级 import 也打包进来）。
        'paddle', 'paddleocr', 'paddlex',
    ],
    noarchive=False,
    optimize=0,
)

# 依赖分析器把 numpy.libs 里的 DLL 又在根目录复制了一份（内容完全相同）。
# numpy 在导入时 add_dll_directory 指向包内目录，根目录副本是冗余，
# 删除（已在构建前逐一比对 sha256 确认相同）。
def _drop_root_duplicates(bins):
    subdir_basenames = set()
    for dest, _source, _typecode in bins:
        norm = dest.replace('\\', '/')
        if '/' in norm:
            subdir_basenames.add(norm.rsplit('/', 1)[-1])
    kept = []
    for dest, source, typecode in bins:
        norm = dest.replace('\\', '/')
        if '/' in norm or norm not in subdir_basenames:
            kept.append((dest, source, typecode))
    return kept


a.binaries = _drop_root_duplicates(a.binaries)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MacroFlowStudio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # PyInstaller 的 Windows GUI bootloader 会静态导入 COMCTL32 序数 380
    # (LoadIconMetric)。若 Windows 激活上下文暂时退回 Common Controls v5，
    # 程序会在进入 Python 前直接报“找不到序数 380”。改用无该导入的 console
    # bootloader，再把最终 PE 子系统改回 GUI；运行时仍不会出现控制台窗口。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)


def _open_rw_with_retry(path, attempts=10, delay=0.5):
    """以读写方式打开 exe，短暂重试。

    杀毒软件会对刚写出的 exe 做实时扫描，构建尾声立即以写模式打开会偶发
    PermissionError；重试几次等扫描释放文件，避免整次构建白跑。
    """
    import time
    last_error = None
    for _ in range(attempts):
        try:
            return open(path, "r+b")
        except OSError as exc:
            last_error = exc
            time.sleep(delay)
    raise last_error


def _set_windows_gui_subsystem(path):
    image = pefile.PE(path, fast_load=True)
    subsystem_offset = image.OPTIONAL_HEADER.get_field_absolute_offset("Subsystem")
    checksum_offset = image.OPTIONAL_HEADER.get_field_absolute_offset("CheckSum")
    image.close()
    with _open_rw_with_retry(path) as stream:
        stream.seek(subsystem_offset)
        stream.write((2).to_bytes(2, "little"))
        stream.seek(checksum_offset)
        stream.write((0).to_bytes(4, "little"))
    image = pefile.PE(path, fast_load=True)
    checksum = image.generate_checksum()
    image.close()
    with _open_rw_with_retry(path) as stream:
        stream.seek(checksum_offset)
        stream.write(checksum.to_bytes(4, "little"))


_set_windows_gui_subsystem(exe.name)
