"""Decoding of subtitle bytes in UTF-8, UTF-16 and legacy code pages."""

from __future__ import annotations

import codecs
import unicodedata

import charset_normalizer

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_LANGUAGE_CODEPAGES: dict[str, tuple[str, ...]] = {}


def _register(codes: str, *pages: str) -> None:
    for code in codes.split():
        _LANGUAGE_CODEPAGES[code] = pages


_register("vie", "cp1258")
_register("pol cze ces slo slk hun rum ron hrv slv bos alb sqi", "cp1250")
_register("rus ukr bul mac mkd bel", "cp1251")
_register("srp", "cp1250", "cp1251")
_register("ara per fas urd", "cp1256")
_register("gre ell", "cp1253")
_register("tur", "cp1254")
_register("heb", "cp1255")
_register("est lav lit", "cp1257")
_register("chi zho zhs", "gb18030", "big5")
_register("zht", "big5", "gb18030")
_register("jpn", "cp932")
_register("kor", "cp949")
_register("tha", "cp874")
_register("eng fre fra spa por ger deu ita dut nld swe dan nor fin", "cp1252")


def _strict(data: bytes, codec: str) -> str | None:
    try:
        return data.decode(codec)
    except (UnicodeDecodeError, LookupError):
        return None


def _decode(data: bytes, language: str | None) -> str:
    for bom, codec in _BOMS:
        if data.startswith(bom):
            text = _strict(data, codec)
            if text is not None:
                return text
    text = _strict(data, "utf-8")
    if text is not None:
        return text
    pages = _LANGUAGE_CODEPAGES.get((language or "").lower())
    if pages:
        match = charset_normalizer.from_bytes(data, cp_isolation=list(pages)).best()
        if match is not None:
            return str(match)
        for page in pages:
            text = _strict(data, page)
            if text is not None:
                return text
    match = charset_normalizer.from_bytes(data).best()
    if match is not None:
        return str(match)
    return data.decode("cp1252", errors="replace")


def decode_bytes(data: bytes, language: str | None = None) -> str:
    """Decode subtitle bytes to text with LF line endings, in composed Unicode (NFC).

    Tries the BOM, strict UTF-8, the code pages usual for `language` (a 3-letter code),
    general detection, and finally cp1252 with replacement characters.
    """
    text = _decode(data, language).lstrip("﻿")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", text)
