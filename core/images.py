"""Уменьшение снимков перед сохранением.

Снимки к шагам технологических карт делают телефоном, и с телефона
приезжает файл на 4000×3000 и четыре мегабайта. Программа живёт
на Raspberry Pi с картой памяти, а копии уходят наружу по обычному
домашнему каналу: двадцать карт по десять шагов в исходном размере —
это гигабайт, который потом ещё и выгружается каждую ночь.

Полтора мегапикселя достаточно: снимок смотрят на экране планшета или
печатают на четверти листа A4. Различить на нём, какой из двух
конденсаторов вздулся, можно и так.

Формат сохраняется у PNG и теряется у остальных: PNG обычно приходит
скриншотом схемы, и перегон его в JPEG размыл бы подписи на выводах.
Фотография в PNG, наоборот, весила бы втрое больше без всякой пользы.
"""
import io

from PIL import Image, ImageOps, UnidentifiedImageError
from django.core.files.base import ContentFile

# Больше этого файл не принимается вовсе — до всякого уменьшения:
# уменьшать нечего, если файл не доехал. Телефонный снимок в это
# укладывается с запасом, а сорокамегабайтная схема — нет, и её место
# на Диске ссылкой (см. материалы модели).
MAX_UPLOAD_BYTES = 15 * 1024 * 1024

# Длинная сторона после уменьшения
MAX_SIDE = 1600

JPEG_QUALITY = 82


def shrink_photo(uploaded, max_side=MAX_SIDE, quality=JPEG_QUALITY):
    """Уменьшенная копия загруженного снимка.

    Возвращает `ContentFile` с прежним именем — его можно присвоить
    полю модели вместо исходного файла. Файл, который не открывается
    как картинка, возвращается как есть: разбираться с ним — дело
    проверки формы, а не этой функции.
    """
    try:
        image = Image.open(uploaded)
        image.load()
    except (UnidentifiedImageError, OSError, ValueError):
        uploaded.seek(0)
        return uploaded

    # Формат запоминаем ДО поворота: exif_transpose возвращает новую
    # картинку, и `format` у неё пустой. Пока проверка стояла после,
    # скриншот схемы уезжал в JPEG — с размытыми подписями на выводах,
    # то есть ровно там, где их и читают
    keep_png = (image.format == 'PNG')

    # Телефон не поворачивает снимок, а дописывает к нему пометку
    # об ориентации. Без этого шага фотография ложится на бок — и мастер
    # видит плату боком, а не так, как она лежала перед ним
    image = ImageOps.exif_transpose(image)
    if not keep_png and image.mode not in ('RGB', 'L'):
        # JPEG не умеет прозрачность: без этого сохранение падает,
        # а на снимке с альфа-каналом появляется чёрный фон
        image = image.convert('RGB')

    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.LANCZOS)

    buffer = io.BytesIO()
    if keep_png:
        image.save(buffer, format='PNG', optimize=True)
        suffix = '.png'
    else:
        image.save(buffer, format='JPEG', quality=quality, optimize=True)
        suffix = '.jpg'

    name = uploaded.name.rsplit('.', 1)[0] + suffix
    return ContentFile(buffer.getvalue(), name=name)
