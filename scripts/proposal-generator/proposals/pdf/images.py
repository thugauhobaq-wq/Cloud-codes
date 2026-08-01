"""Логотипы: PNG и JPEG → графический объект PDF.

JPEG кладётся в файл как есть — PDF умеет DCTDecode, перекодировать нечего.
PNG приходится распаковывать самим: PDF не понимает ни палитру, ни канал
прозрачности внутри потока. Альфу выносим в /SMask — тогда логотип с
прозрачным фоном ложится на цветную подложку без белого прямоугольника.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .objects import Name, PDFFile, Ref, Stream

# Логотипы приходят и на 4000 px. В КП картинка занимает от силы 200 pt,
# то есть ~830 px при 300 dpi — всё, что крупнее, только раздувает файл.
MAX_PIXELS = 1000


class ImageError(ValueError):
    """Файл не картинка, повреждён или в неподдерживаемом формате."""


@dataclass(slots=True)
class RasterImage:
    """Разобранная картинка, готовая лечь в PDF."""

    width: int
    height: int
    data: bytes
    """Либо пиксели (по 8 бит на канал), либо исходный JPEG."""
    colorspace: str
    """DeviceGray, DeviceRGB или DeviceCMYK."""
    alpha: bytes | None = None
    """Канал прозрачности, по байту на пиксель."""
    jpeg: bool = False
    decode: list[int] | None = None

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0

    def write(self, pdf: PDFFile) -> Ref:
        extra: dict[str, object] = {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": self.width,
            "Height": self.height,
            "ColorSpace": Name(self.colorspace),
            "BitsPerComponent": 8,
        }
        if self.decode:
            extra["Decode"] = self.decode
        if self.jpeg:
            extra["Filter"] = Name("DCTDecode")
        if self.alpha is not None:
            extra["SMask"] = pdf.add(
                Stream(
                    self.alpha,
                    {
                        "Type": Name("XObject"),
                        "Subtype": Name("Image"),
                        "Width": self.width,
                        "Height": self.height,
                        "ColorSpace": Name("DeviceGray"),
                        "BitsPerComponent": 8,
                    },
                )
            )
        return pdf.add(Stream(self.data, extra, compress=not self.jpeg))


def load_image(data: bytes) -> RasterImage:
    """Распознать формат по сигнатуре и разобрать."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return _shrink(_load_png(data))
    if data[:2] == b"\xff\xd8":
        return _load_jpeg(data)
    raise ImageError("поддерживаются только PNG и JPEG (логотип в SVG сохраните в PNG)")


# --- JPEG ----------------------------------------------------------------------

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _load_jpeg(data: bytes) -> RasterImage:
    at = 2
    adobe_inverted = False
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        if at + 4 > len(data):
            break
        length = struct.unpack_from(">H", data, at + 2)[0]
        segment = data[at + 4 : at + 2 + length]
        if marker == 0xEE and segment[:5] == b"Adobe":
            # APP14: у CMYK-джипегов от Adobe каналы записаны инвертированными.
            adobe_inverted = True
        if marker in _SOF_MARKERS:
            if length < 8:
                raise ImageError("повреждённый JPEG: короткий заголовок кадра")
            _, height, width, components = struct.unpack_from(">BHHB", data, at + 4)
            colorspace = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(components)
            if colorspace is None:
                raise ImageError(f"JPEG с {components} каналами не поддерживается")
            decode = [1, 0] * 4 if (components == 4 and adobe_inverted) else None
            if not width or not height:
                raise ImageError("повреждённый JPEG: нулевой размер")
            return RasterImage(width, height, data, colorspace, jpeg=True, decode=decode)
        at += 2 + length
    raise ImageError("в JPEG не нашёлся заголовок кадра — файл повреждён")


# --- PNG -----------------------------------------------------------------------

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _load_png(data: bytes) -> RasterImage:
    at = 8
    header: tuple[int, int, int, int, int] | None = None
    idat = bytearray()
    palette = b""
    trns = b""
    while at + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, at)
        body = data[at + 8 : at + 8 + length]
        at += 12 + length
        if kind == b"IHDR":
            width, height, depth, color_type, _comp, _filter, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            header = (width, height, depth, color_type, interlace)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    if header is None:
        raise ImageError("повреждённый PNG: нет заголовка IHDR")
    width, height, depth, color_type, interlace = header
    if not width or not height:
        raise ImageError("повреждённый PNG: нулевой размер")
    if color_type not in _CHANNELS:
        raise ImageError(f"PNG с типом цвета {color_type} не поддерживается")
    if depth not in (1, 2, 4, 8, 16):
        raise ImageError(f"PNG с глубиной {depth} бит не поддерживается")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as error:
        raise ImageError(f"повреждённый PNG: {error}") from error

    channels = _CHANNELS[color_type]
    if interlace == 1:
        pixels = _deinterlace(raw, width, height, depth, channels)
    elif interlace == 0:
        pixels = _unfilter(raw, width, height, depth, channels)
    else:
        raise ImageError(f"PNG с чередованием {interlace} не поддерживается")

    samples = _to_bytes(pixels, width * height * channels, depth)
    return _assemble_png(samples, width, height, color_type, palette, trns, depth)


def _unfilter(raw: bytes, width: int, height: int, depth: int, channels: int) -> bytearray:
    """Снять построчные фильтры PNG (Sub/Up/Average/Paeth)."""
    bits_per_pixel = depth * channels
    stride = (width * bits_per_pixel + 7) // 8
    step = max(1, bits_per_pixel // 8)  # смещение до «левого» пикселя
    needed = height * (stride + 1)
    if len(raw) < needed:
        raise ImageError("повреждённый PNG: данных меньше, чем обещает заголовок")

    out = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        at = row * (stride + 1)
        filter_type = raw[at]
        line = bytearray(raw[at + 1 : at + 1 + stride])
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for index in range(step, stride):
                line[index] = (line[index] + line[index - step]) & 0xFF
        elif filter_type == 2:
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif filter_type == 3:
            for index in range(stride):
                left = line[index - step] if index >= step else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index in range(stride):
                left = line[index - step] if index >= step else 0
                up = previous[index]
                upper_left = previous[index - step] if index >= step else 0
                estimate = left + up - upper_left
                da = abs(estimate - left)
                db = abs(estimate - up)
                dc = abs(estimate - upper_left)
                if da <= db and da <= dc:
                    nearest = left
                elif db <= dc:
                    nearest = up
                else:
                    nearest = upper_left
                line[index] = (line[index] + nearest) & 0xFF
        else:
            raise ImageError(f"повреждённый PNG: неизвестный фильтр строки {filter_type}")
        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


# Adam7: (начальный x, начальный y, шаг x, шаг y) для каждого из семи проходов.
_ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def _deinterlace(raw: bytes, width: int, height: int, depth: int, channels: int) -> bytearray:
    """Собрать чересстрочный PNG в обычную растровую сетку."""
    stride = (width * depth * channels + 7) // 8
    canvas = bytearray(height * stride)
    at = 0
    for start_x, start_y, step_x, step_y in _ADAM7:
        pass_width = (width - start_x + step_x - 1) // step_x
        pass_height = (height - start_y + step_y - 1) // step_y
        if pass_width <= 0 or pass_height <= 0:
            continue
        pass_stride = (pass_width * depth * channels + 7) // 8
        chunk = raw[at : at + pass_height * (pass_stride + 1)]
        at += pass_height * (pass_stride + 1)
        rows = _unfilter(chunk, pass_width, pass_height, depth, channels)
        for row in range(pass_height):
            line = rows[row * pass_stride : (row + 1) * pass_stride]
            target_y = start_y + row * step_y
            for column in range(pass_width):
                target_x = start_x + column * step_x
                for channel in range(channels):
                    value = _sample(line, (column * channels + channel), depth)
                    _put_sample(
                        canvas,
                        target_y * stride,
                        (target_x * channels + channel),
                        depth,
                        value,
                    )
    return canvas


def _sample(line: bytearray | bytes, index: int, depth: int) -> int:
    if depth == 8:
        return line[index]
    if depth == 16:
        return line[index * 2] << 8 | line[index * 2 + 1]
    per_byte = 8 // depth
    byte = line[index // per_byte]
    shift = 8 - depth * (index % per_byte + 1)
    return (byte >> shift) & ((1 << depth) - 1)


def _put_sample(canvas: bytearray, row_at: int, index: int, depth: int, value: int) -> None:
    if depth == 8:
        canvas[row_at + index] = value
    elif depth == 16:
        canvas[row_at + index * 2] = value >> 8
        canvas[row_at + index * 2 + 1] = value & 0xFF
    else:
        per_byte = 8 // depth
        at = row_at + index // per_byte
        shift = 8 - depth * (index % per_byte + 1)
        mask = ((1 << depth) - 1) << shift
        canvas[at] = (canvas[at] & ~mask & 0xFF) | ((value << shift) & mask)


def _to_bytes(pixels: bytearray, count: int, depth: int) -> bytearray:
    """Любую глубину привести к 8 битам на канал."""
    if depth == 8:
        return pixels[:count]
    if depth == 16:
        return bytearray(pixels[index * 2] for index in range(count))
    # 1/2/4 бита: растягиваем до полного диапазона 0..255.
    top = (1 << depth) - 1
    out = bytearray(count)
    per_byte = 8 // depth
    for index in range(count):
        byte = pixels[index // per_byte]
        shift = 8 - depth * (index % per_byte + 1)
        out[index] = ((byte >> shift) & top) * 255 // top
    return out


def _assemble_png(
    samples: bytearray,
    width: int,
    height: int,
    color_type: int,
    palette: bytes,
    trns: bytes,
    depth: int,
) -> RasterImage:
    count = width * height
    if color_type == 0:
        return RasterImage(width, height, bytes(samples), "DeviceGray")
    if color_type == 2:
        return RasterImage(width, height, bytes(samples), "DeviceRGB")
    if color_type == 4:
        gray = bytes(samples[index * 2] for index in range(count))
        alpha = bytes(samples[index * 2 + 1] for index in range(count))
        return RasterImage(width, height, gray, "DeviceGray", alpha=alpha)
    if color_type == 6:
        rgb = bytearray(count * 3)
        alpha = bytearray(count)
        for index in range(count):
            source = index * 4
            rgb[index * 3 : index * 3 + 3] = samples[source : source + 3]
            alpha[index] = samples[source + 3]
        return RasterImage(width, height, bytes(rgb), "DeviceRGB", alpha=bytes(alpha))

    # Палитра: индексы разворачиваем в честный RGB — так проще, а логотипы
    # в палитре всё равно маленькие.
    if not palette:
        raise ImageError("повреждённый PNG: палитра объявлена, но не приложена")
    entries = len(palette) // 3
    rgb = bytearray(count * 3)
    alpha = bytearray(b"\xff") * count if trns else None
    top = (1 << depth) - 1
    for index in range(count):
        # После _to_bytes индексы палитры растянуты до 0..255 — вернём обратно.
        raw_index = samples[index] * top // 255 if depth < 8 else samples[index]
        if raw_index >= entries:
            raw_index = 0
        rgb[index * 3 : index * 3 + 3] = palette[raw_index * 3 : raw_index * 3 + 3]
        if alpha is not None:
            alpha[index] = trns[raw_index] if raw_index < len(trns) else 0xFF
    return RasterImage(
        width, height, bytes(rgb), "DeviceRGB", alpha=bytes(alpha) if alpha else None
    )


def _shrink(image: RasterImage) -> RasterImage:
    """Уменьшить слишком крупный логотип усреднением по блокам."""
    factor = max(image.width, image.height) // MAX_PIXELS
    if factor < 2:
        return image

    channels = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}[image.colorspace]
    new_width = max(1, image.width // factor)
    new_height = max(1, image.height // factor)
    area = factor * factor

    def resample(source: bytes, planes: int) -> bytes:
        out = bytearray(new_width * new_height * planes)
        for row in range(new_height):
            for column in range(new_width):
                sums = [0] * planes
                for dy in range(factor):
                    line = ((row * factor + dy) * image.width + column * factor) * planes
                    for dx in range(factor):
                        at = line + dx * planes
                        for plane in range(planes):
                            sums[plane] += source[at + plane]
                target = (row * new_width + column) * planes
                for plane in range(planes):
                    out[target + plane] = sums[plane] // area
        return bytes(out)

    return RasterImage(
        new_width,
        new_height,
        resample(image.data, channels),
        image.colorspace,
        alpha=resample(image.alpha, 1) if image.alpha else None,
        decode=image.decode,
    )
