"""Проверка и подготовка фото профиля."""

from io import BytesIO

from PIL import Image, ImageOps
from werkzeug.datastructures import FileStorage

AVATAR_MAX_MB = 3
AVATAR_MAX_BYTES = AVATAR_MAX_MB * 1024 * 1024
AVATAR_MAX_PIXELS = 20_000_000
AVATAR_SIZE = (384, 384)


def prepare_avatar(upload: FileStorage) -> bytes:
    data = upload.stream.read(AVATAR_MAX_BYTES + 1)
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError(f"Загрузите фото размером до {AVATAR_MAX_MB} МБ.")

    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "WEBP")) as image:
            if image.width * image.height > AVATAR_MAX_PIXELS or getattr(image, "is_animated", False):
                raise ValueError("Выберите статичное фото до 20 мегапикселей.")
            ImageOps.exif_transpose(image, in_place=True)
            image.thumbnail(AVATAR_SIZE)
            rgba = image.convert("RGBA")
            clean = Image.new("RGB", image.size, "#f3e9e9")
            clean.paste(rgba, mask=rgba.getchannel("A"))
            output = BytesIO()
            clean.save(output, format="JPEG", quality=88)
            return output.getvalue()
    except (OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("Не удалось открыть фото. Выберите изображение JPG, PNG или WebP.") from error
