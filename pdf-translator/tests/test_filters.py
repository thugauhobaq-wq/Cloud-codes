"""Распаковка потоков."""

from __future__ import annotations

import zlib

import pytest

from pdftr.pdf.filters import FilterError, apply_predictor, decode


def test_flate():
    payload = zlib.compress(b"hello world" * 10)
    assert decode(payload, ["FlateDecode"], [])[0] == b"hello world" * 10


def test_flate_truncated_stream_returns_what_it_can():
    # Принтеры и «оптимизаторы» регулярно обрубают поток — половина данных
    # лучше, чем пустая страница.
    payload = zlib.compress(b"a" * 5000)[:-6]
    out, _ = decode(payload, ["FlateDecode"], [])
    assert out.startswith(b"aaa")


def test_ascii_hex():
    assert decode(b"48656C6C6F>", ["ASCIIHexDecode"], [])[0] == b"Hello"
    # Нечётное число цифр: недостающая по стандарту считается нулём.
    assert decode(b"4>", ["ASCIIHexDecode"], [])[0] == b"@"


def test_ascii85():
    assert decode(b"87cURD]i,\"Ebo80~>", ["ASCII85Decode"], [])[0] == b"Hello World!"
    assert decode(b"z~>", ["ASCII85Decode"], [])[0] == b"\x00\x00\x00\x00"


def test_run_length():
    assert decode(bytes([2, 65, 66, 67, 254, 88, 128]), ["RunLengthDecode"], [])[0] == b"ABCXXX"


def test_chain_of_filters():
    payload = zlib.compress(b"twice packed").hex().upper().encode() + b">"
    out, _ = decode(payload, ["ASCIIHexDecode", "FlateDecode"], [])
    assert out == b"twice packed"


def test_image_filter_stops_the_chain():
    out, stopped = decode(b"\xff\xd8raw jpeg", ["DCTDecode"], [])
    assert stopped == "DCTDecode"
    assert out == b"\xff\xd8raw jpeg"


def test_unknown_filter_is_an_error():
    with pytest.raises(FilterError):
        decode(b"data", ["MagicDecode"], [])


def test_png_predictor_up():
    # Каждая строка кодируется как разница с предыдущей (фильтр 2 — Up).
    rows = bytes([2, 1, 1, 1]) + bytes([2, 1, 1, 1])
    out = apply_predictor(rows, {"Predictor": 12, "Columns": 3, "Colors": 1})
    assert out == bytes([1, 1, 1, 2, 2, 2])


def test_tiff_predictor():
    out = apply_predictor(bytes([1, 1, 1, 1]), {"Predictor": 2, "Columns": 4, "Colors": 1})
    assert out == bytes([1, 2, 3, 4])


def test_predictor_one_changes_nothing():
    assert apply_predictor(b"abc", {"Predictor": 1}) == b"abc"
