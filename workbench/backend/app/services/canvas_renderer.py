"""画布渲染器：按图层顺序把画布元素合成 PNG。"""
import io
from PIL import Image, ImageDraw
from ..generators.image import _font, _hex


def render_canvas(doc: dict, asset_loader) -> bytes:
    w = int(doc.get("width") or 768)
    h = int(doc.get("height") or 1024)
    canvas = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    for layer in doc.get("layers", []):
        t = layer.get("type")
        x = int(layer.get("x", 0)); y = int(layer.get("y", 0))
        lw = int(layer.get("width", 0) or 0); lh = int(layer.get("height", 0) or 0)
        if t == "image":
            im = asset_loader(layer.get("asset_id"))
            if im is None:
                continue
            im = im.convert("RGBA")
            if lw and lh:
                im = im.resize((max(1, lw), max(1, lh)))
            canvas.alpha_composite(im, (max(0, x), max(0, y)))
        elif t == "sticky":
            dr = ImageDraw.Draw(canvas)
            dr.rectangle([x, y, x + lw, y + lh], fill=_hex(layer.get("color", "#FEF3C7")) + (255,))
            dr.text((x + 10, y + 10), layer.get("text", ""), font=_font(20), fill=(40, 40, 40, 255))
        elif t == "rect":
            dr = ImageDraw.Draw(canvas)
            dr.rectangle([x, y, x + lw, y + lh], fill=_hex(layer.get("color", "#9CA3AF")) + (255,))
        elif t == "text":
            dr = ImageDraw.Draw(canvas)
            dr.text((x, y), layer.get("text", ""), font=_font(int(layer.get("font_size", 32))),
                    fill=_hex(layer.get("fill", "#0F172A")) + (255,))
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
