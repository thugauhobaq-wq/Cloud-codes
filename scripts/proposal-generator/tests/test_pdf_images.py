"""Разбор PNG и JPEG."""

from __future__ import annotations

import struct
import zlib

import pytest

from proposals.pdf.images import MAX_PIXELS, ImageError, load_image
from proposals.pdf.objects import PDFFile, serialize
from proposals.sample import demo_logo


def png(chunks: list[tuple[bytes, bytes]]) -> bytes:
    out = b"\x89PNG\r\n\x1a\n"
    for kind, body in chunks:
        out += (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )
    return out


def ihdr(width: int, height: int, depth: int, color_type: int, interlace: int = 0) -> bytes:
    return struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, interlace)


def rows(lines: list[bytes], filter_type: int = 0) -> bytes:
    return zlib.compress(b"".join(bytes([filter_type]) + line for line in lines))


def build(width, height, depth, color_type, lines, *, palette=b"", trns=b"", filter_type=0):
    chunks = [(b"IHDR", ihdr(width, height, depth, color_type))]
    if palette:
        chunks.append((b"PLTE", palette))
    if trns:
        chunks.append((b"tRNS", trns))
    chunks.append((b"IDAT", rows(lines, filter_type)))
    chunks.append((b"IEND", b""))
    return png(chunks)


# --- PNG -----------------------------------------------------------------------


def test_rgb():
    image = load_image(build(2, 1, 8, 2, [b"\xff\x00\x00\x00\xff\x00"]))
    assert (image.width, image.height) == (2, 1)
    assert image.colorspace == "DeviceRGB"
    assert image.data == b"\xff\x00\x00\x00\xff\x00"
    assert image.alpha is None


def test_grayscale():
    image = load_image(build(2, 1, 8, 0, [b"\x00\xff"]))
    assert image.colorspace == "DeviceGray"
    assert image.data == b"\x00\xff"


def test_rgba_splits_alpha_into_smask():
    """Прозрачность обязана уехать в SMask, иначе логотип ляжет с белым фоном."""
    image = load_image(build(2, 1, 8, 6, [b"\xff\x00\x00\x80\x00\xff\x00\x40"]))
    assert image.data == b"\xff\x00\x00\x00\xff\x00"
    assert image.alpha == b"\x80\x40"


def test_gray_with_alpha():
    image = load_image(build(2, 1, 8, 4, [b"\x10\x20\x30\x40"]))
    assert image.colorspace == "DeviceGray"
    assert image.data == b"\x10\x30"
    assert image.alpha == b"\x20\x40"


def test_palette_is_expanded_to_rgb():
    palette = b"\xff\x00\x00" + b"\x00\x00\xff"
    image = load_image(build(2, 1, 8, 3, [b"\x00\x01"], palette=palette))
    assert image.colorspace == "DeviceRGB"
    assert image.data == b"\xff\x00\x00\x00\x00\xff"


def test_palette_transparency():
    palette = b"\xff\x00\x00" + b"\x00\x00\xff"
    image = load_image(build(2, 1, 8, 3, [b"\x00\x01"], palette=palette, trns=b"\x00"))
    assert image.alpha == b"\x00\xff"


def test_four_bit_palette():
    palette = b"\xff\x00\x00" + b"\x00\xff\x00" + b"\x00\x00\xff"
    # Два пикселя по 4 бита в одном байте: индексы 2 и 1.
    image = load_image(build(2, 1, 4, 3, [b"\x21"], palette=palette))
    assert image.data == b"\x00\x00\xff\x00\xff\x00"


def test_sixteen_bit_is_downsampled_to_eight():
    image = load_image(build(1, 1, 16, 2, [b"\xff\xff\x00\x00\x80\x80"]))
    assert image.data == b"\xff\x00\x80"


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_all_row_filters_round_trip(filter_type):
    """Sub/Up/Average/Paeth на однотонной картинке дают тот же результат."""
    width, height = 4, 3
    line = b"\x00" * (width * 3)
    image = load_image(build(width, height, 8, 2, [line] * height, filter_type=filter_type))
    assert image.data == b"\x00" * (width * height * 3)


def test_sub_filter_reconstructs_gradient():
    # Фильтр Sub: каждый байт — приращение к соседу слева.
    line = bytes([10, 10, 10, 5, 5, 5, 5, 5, 5])
    image = load_image(build(3, 1, 8, 2, [line], filter_type=1))
    assert image.data == bytes([10, 10, 10, 15, 15, 15, 20, 20, 20])


def test_interlaced_png():
    """Adam7 встречается редко, но «Сохранить для веба» иногда его выдаёт."""
    source = [bytes(range(24)) for _ in range(8)]
    expected = load_image(build(8, 8, 8, 2, source))

    # Тот же растр, разложенный по семи проходам Adam7.
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    lines = []
    for start_x, start_y, step_x, step_y in passes:
        for y in range(start_y, 8, step_y):
            row = bytearray()
            for x in range(start_x, 8, step_x):
                row += source[y][x * 3 : x * 3 + 3]
            if row:
                lines.append(bytes(row))

    interlaced = png(
        [(b"IHDR", ihdr(8, 8, 8, 2, 1)), (b"IDAT", rows(lines)), (b"IEND", b"")]
    )
    image = load_image(interlaced)
    assert (image.width, image.height) == (8, 8)
    assert image.data == expected.data


def test_large_image_is_downscaled():
    """Логотип на 3000 px не должен уезжать в PDF целиком."""
    side = MAX_PIXELS * 3
    line = b"\x40\x80\xc0" * side
    image = load_image(build(side, 4, 8, 2, [line] * 4))
    assert image.width == side // 3
    assert image.data[:3] == b"\x40\x80\xc0", "усреднение однотонных пикселей меняет цвет"


def test_demo_logo_round_trips():
    image = load_image(demo_logo("ТУ"))
    assert image.colorspace == "DeviceRGB"
    assert image.alpha is not None, "у логотипа должен остаться прозрачный фон"
    assert len(image.data) == image.width * image.height * 3


def test_broken_png_is_reported():
    with pytest.raises(ImageError):
        load_image(png([(b"IHDR", ihdr(2, 2, 8, 2)), (b"IDAT", b"not zlib"), (b"IEND", b"")]))


def test_truncated_png_is_reported():
    with pytest.raises(ImageError, match="меньше"):
        load_image(build(8, 8, 8, 2, [b"\x00" * 24]))


def test_unknown_format_is_reported():
    with pytest.raises(ImageError, match="PNG и JPEG"):
        load_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")


# --- JPEG ----------------------------------------------------------------------


def jpeg(width: int, height: int, components: int, extra: bytes = b"") -> bytes:
    sof = struct.pack(">BHHB", 8, height, width, components) + b"\x00" * (components * 3)
    return (
        b"\xff\xd8"
        + extra
        + b"\xff\xc0"
        + struct.pack(">H", len(sof) + 2)
        + sof
        + b"\xff\xda\x00\x08\x01\x00\x00\x3f\x00"
        + b"\xff\xd9"
    )


def test_jpeg_is_embedded_untouched():
    data = jpeg(320, 240, 3)
    image = load_image(data)
    assert (image.width, image.height) == (320, 240)
    assert image.colorspace == "DeviceRGB"
    assert image.jpeg and image.data == data


def test_jpeg_grayscale():
    assert load_image(jpeg(10, 10, 1)).colorspace == "DeviceGray"


def test_adobe_cmyk_jpeg_is_inverted():
    """У CMYK-джипегов Adobe каналы записаны наоборот — правим массивом Decode."""
    app14 = b"\xff\xee" + struct.pack(">H", 16) + b"Adobe" + b"\x00" * 9
    image = load_image(jpeg(10, 10, 4, extra=app14))
    assert image.colorspace == "DeviceCMYK"
    assert image.decode == [1, 0, 1, 0, 1, 0, 1, 0]


def test_jpeg_without_frame_header_is_reported():
    with pytest.raises(ImageError, match="повреждён"):
        load_image(b"\xff\xd8\xff\xd9")


# --- запись в PDF --------------------------------------------------------------


def test_write_adds_smask_object():
    pdf = PDFFile()
    image = load_image(build(2, 1, 8, 6, [b"\xff\x00\x00\x80\x00\xff\x00\x40"]))
    ref = image.write(pdf)
    dump = serialize(pdf._objects[ref.num - 1])
    assert b"/SMask" in dump
    assert b"/ColorSpace /DeviceRGB" in dump


def test_write_marks_jpeg_with_dct_filter():
    pdf = PDFFile()
    ref = load_image(jpeg(4, 4, 3)).write(pdf)
    assert b"/Filter /DCTDecode" in serialize(pdf._objects[ref.num - 1])
