"""
Утилиты для LiftTeam v2.6.0.
"""
import barcode
from barcode.writer import ImageWriter
from io import BytesIO
import base64
import json


def generate_barcode_image(text, width=300, height=100):
    """Генерация штрихкода Code 128 из текста адреса ячейки.
    Оптимизировано для этикеток 43x25 мм (термопринтер XP-365)."""
    try:
        code128 = barcode.get_barcode_class('code128')
        barcode_obj = code128(text, writer=ImageWriter())
        buffer = BytesIO()
        barcode_obj.write(buffer, options={
            'write_text': True,
            'module_width': 0.25,
            'module_height': 12,
            'font_size': 9,
            'text_distance': 2,
            'quiet_zone': 2,
        })
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.read()).decode('utf-8')
        return f"data:image/png;base64,{img_base64}"
    except Exception:
        return None


def generate_qr_image(data, size=200):
    """Генерация QR-кода с данными о детали.
    Оптимизировано для этикеток 43x25 мм."""
    try:
        import qrcode
        from qrcode.image.styledpil import StyledPilImage

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6,
            border=1,
        )
        qr.add_data(data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.read()).decode('utf-8')
        return f"data:image/png;base64,{img_base64}"
    except ImportError:
        # Fallback: штрихкод вместо QR
        return generate_barcode_image(data[:50])
    except Exception:
        return None




