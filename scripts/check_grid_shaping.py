"""Check HarfBuzz layout of base and NF faces from an SC+JP Regular build."""

import argparse
import json
from pathlib import Path
import subprocess

from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont


def shape(path, text, *options):
    command = ["hb-shape", str(path), text, "--no-glyph-names", "-O", "json", *options]
    return json.loads(subprocess.check_output(command, text=True))


def check_curly_quotes(path):
    with TTFont(path) as font:
        glyph_set = font.getGlyphSet()
        for quote in "“”‘’":
            ordinary, following = shape(path, quote + "A")
            pen = BoundsPen(glyph_set)
            glyph_set[font.getGlyphName(ordinary["g"])].draw(pen)
            left, _, right, _ = pen.bounds
            if not (0 <= left + ordinary["dx"] < right + ordinary["dx"] <= 500):
                raise ValueError(f"{path.name}: U+{ord(quote):04X} ink [{left}, {right}] escapes the first cell")
            if (ordinary["ax"], following["ax"], following["dx"]) != (500, 500, 0):
                raise ValueError(f"{path.name}: U+{ord(quote):04X} shifts the following letter")
            halfwidth, = shape(path, quote + "\ufe00")
            fullwidth, = shape(path, quote + "\ufe01")
            featured, = shape(path, quote, "--features=fwid")
            if (halfwidth["ax"], fullwidth["ax"], featured["ax"]) != (500, 1000, 1000):
                raise ValueError(f"{path.name}: U+{ord(quote):04X} has incorrect width variants")
            if (ordinary["g"], featured["g"]) != (halfwidth["g"], fullwidth["g"]):
                raise ValueError(f"{path.name}: U+{ord(quote):04X} selects the wrong variant")


def check(path):
    check_curly_quotes(path)
    tests = (
        ("x\u0304A", [500, 0, 500]),
        ("あて", [1000, 1000]),
        ("A♥B", [500, 1000, 500]),
        ("\uffa1\u3131", [500, 1000]),
    )
    for text, expected in tests:
        advances = [glyph["ax"] for glyph in shape(path, text)]
        if advances != expected:
            raise ValueError(f"{path.name}: {text!r} advances {advances}, expected {expected}")
    letter, mark, _ = shape(path, "x\u0304A")
    with TTFont(path) as font:
        glyph_set = font.getGlyphSet()
        mark_pen = BoundsPen(glyph_set)
        glyph_set[font.getGlyphName(mark["g"])].draw(mark_pen)
        left, bottom, right, _ = mark_pen.bounds
        start = letter["ax"] + mark["dx"]
        letter_pen = BoundsPen(glyph_set)
        glyph_set[font.getGlyphName(letter["g"])].draw(letter_pen)
        if not (0 <= start + left < start + right <= 500 and bottom + mark["dy"] > letter_pen.bounds[3]):
            raise ValueError(f"{path.name}: combining macron is not above its base letter")
    glyphs = shape(path, "。骨", "--direction=ttb", "--language=ja")
    if [(glyph["dy"], glyph["ay"]) for glyph in glyphs] != [(-880, -1000)] * 2:
        raise ValueError(f"{path.name}: incorrect Japanese vertical positions")
    print(f"PASS {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fonts", type=Path, nargs="+")
    parser.add_argument("--quotes-only", action="store_true")
    args = parser.parse_args()
    try:
        for font in args.fonts:
            if args.quotes_only:
                check_curly_quotes(font)
                print(f"PASS {font}")
            else:
                check(font)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
