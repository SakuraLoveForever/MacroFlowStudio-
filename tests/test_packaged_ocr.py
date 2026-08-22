"""在解包出的打包布局 _meitest 下做真实 OCR 识别测试（发布门禁）。"""
import os
import sys
import time

os.environ["PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT"] = "False"
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_PDX_MODEL_SOURCE"] = "bos"
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

sys.path.insert(0, os.path.abspath("_meitest"))

t0 = time.time()
from paddleocr import PaddleOCR  # noqa: E402

print(f"import paddleocr OK ({time.time() - t0:.1f}s)")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

img = Image.new("RGB", (420, 90), "white")
d = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/simhei.ttf", 44)
except Exception:
    font = ImageFont.load_default()
d.text((15, 12), "游戏第3关", fill="black", font=font)
img.save("_ocr_test.png")

EX = os.path.abspath("_meitest")
t0 = time.time()
eng = PaddleOCR(
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    text_detection_model_name="PP-OCRv5_mobile_det",
    text_detection_model_dir=os.path.join(EX, "paddle_models", "PP-OCRv5_mobile_det"),
    text_recognition_model_name="PP-OCRv5_mobile_rec",
    text_recognition_model_dir=os.path.join(EX, "paddle_models", "PP-OCRv5_mobile_rec"),
    textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
    textline_orientation_model_dir=os.path.join(EX, "paddle_models", "PP-LCNet_x1_0_textline_ori"),
)
print(f"引擎初始化 OK ({time.time() - t0:.1f}s)")

t0 = time.time()
result = eng.predict("_ocr_test.png")
texts = []
for page in result:
    if isinstance(page, dict):
        texts.extend(page.get("rec_texts") or [])
print(f"识别 OK ({time.time() - t0:.1f}s): {texts}")
joined = "".join(str(t) for t in texts)
assert "游戏" in joined, f"识别结果异常: {texts}"
print(f"PASS：打包布局（重复 DLL 已移除）下真实 OCR 识别成功：{joined}")
