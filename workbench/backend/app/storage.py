"""本地对象存储：原图 / 生成图 / 导出包的落盘与读取。

生产应替换为 S3 兼容存储；MVP 用本地文件 + 固定目录，路径不作为权限凭证。
所有对外 ID 由服务端生成，返回文件内容时不暴露真实路径。
"""
import os
import hashlib
import io
import time
from PIL import Image

Image.MAX_IMAGE_PIXELS = 40_000_000

UPLOAD_DIR = os.environ.get("WB_UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "uploads"))


def ensure_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _sanitize_key(object_key: str) -> str:
    """清洗 object_key：拒绝路径穿越，统一为相对路径（用 / 分隔）。"""
    if not object_key:
        raise ValueError("非法存储路径")
    key = object_key.replace("\\", "/")
    parts = [p for p in key.split("/") if p and p not in (".", "..")]
    if not parts:
        raise ValueError("非法存储路径")
    return "/".join(parts)


def save_upload(project_id: str, filename: str, data: bytes) -> dict:
    """保存上传字节，返回 (object_key, sha256, width, height, mime)。"""
    ensure_dirs()
    sha = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    key = _sanitize_key(f"{project_id}/{sha}{ext}")
    path = os.path.join(UPLOAD_DIR, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    width = height = 0
    mime = "application/octet-stream"
    try:
        im = Image.open(io.BytesIO(data))
        width, height = im.size
        mime = f"image/{im.format.lower()}" if im.format else "image/png"
    except Exception:
        pass
    return {"object_key": key, "sha256": sha, "width": width, "height": height, "mime": mime}


def save_bytes(object_key: str, data: bytes) -> str:
    ensure_dirs()
    path = os.path.join(UPLOAD_DIR, _sanitize_key(object_key))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def read_bytes(object_key: str) -> bytes:
    path = os.path.join(UPLOAD_DIR, _sanitize_key(object_key))
    with open(path, "rb") as f:
        return f.read()


def read_pillow(object_key: str) -> Image.Image:
    return Image.open(os.path.join(UPLOAD_DIR, _sanitize_key(object_key))).convert("RGBA")


def delete(object_key: str) -> None:
    try:
        os.remove(os.path.join(UPLOAD_DIR, _sanitize_key(object_key)))
    except FileNotFoundError:
        pass
