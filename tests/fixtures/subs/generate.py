"""Write the encoded fixture copies from the UTF-8 reference files next to this script.

Run once with `.venv/bin/python tests/fixtures/subs/generate.py` and commit the output.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

HERE = Path(__file__).parent
TONES = "̣̀́̃̉"

# (reference, output, codec, line ending)
ENCODED = [
    ("eng", "eng.utf8bom.srt", "utf-8-sig", "\r\n"),
    ("eng", "eng.utf16le.srt", "utf-16-le-bom", "\r\n"),
    ("eng", "eng.utf16be.srt", "utf-16-be-bom", "\n"),
    ("vie", "vie.cp1258.srt", "cp1258", "\r\n"),
    ("fre", "fre.cp1252.srt", "cp1252", "\n"),
    ("pol", "pol.cp1250.srt", "cp1250", "\r\n"),
    ("rus", "rus.cp1251.srt", "cp1251", "\n"),
    ("ara", "ara.cp1256.srt", "cp1256", "\r\n"),
    ("chi", "chi.gb18030.srt", "gb18030", "\n"),
    ("zht", "zht.big5.srt", "big5", "\r\n"),
]


def encode_cp1258(text: str) -> bytes:
    """cp1258 keeps Vietnamese tones as combining marks, so encode letter by letter."""
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1258")
        except UnicodeEncodeError:
            decomposed = unicodedata.normalize("NFD", ch)
            base = unicodedata.normalize("NFC", "".join(c for c in decomposed if c not in TONES))
            out += (base + "".join(c for c in decomposed if c in TONES)).encode("cp1258")
    return bytes(out)


def encode(text: str, codec: str) -> bytes:
    if codec == "cp1258":
        return encode_cp1258(text)
    if codec == "utf-16-le-bom":
        return b"\xff\xfe" + text.encode("utf-16-le")
    if codec == "utf-16-be-bom":
        return b"\xfe\xff" + text.encode("utf-16-be")
    return text.encode(codec)


def main() -> None:
    for reference, output, codec, newline in ENCODED:
        text = (HERE / f"{reference}.srt").read_text(encoding="utf-8").replace("\r\n", "\n")
        (HERE / output).write_bytes(encode(text.replace("\n", newline), codec))


if __name__ == "__main__":
    main()
