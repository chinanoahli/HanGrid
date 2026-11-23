#!/usr/bin/env python3
"""Convert standalone Han Grid Mono OTFs into static TrueType fonts."""

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import NamedTemporaryFile

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable

from build_mono import ROOT, mappings_by_gid, variation_sequences, verify_terminal_grid


def cmap_ids(font):
    return mappings_by_gid(font, font.getBestCmap(), variation_sequences(font))


def verify_ttf(path, expected_cmap, horizontal, vertical, preserved):
    with TTFont(path, checkChecksums=2) as font:
        if font.sfntVersion != "\x00\x01\x00\x00" or not {"glyf", "loca"} <= set(font.keys()):
            raise ValueError(f"Expected TrueType outlines in {path.name}")
        if cmap_ids(font) != expected_cmap:
            raise ValueError(f"Changed character mappings in {path.name}")
        for tag, metrics in (("hmtx", horizontal), ("vmtx", vertical)):
            if [font[tag][glyph] for glyph in font.getGlyphOrder()] != list(metrics.values()):
                raise ValueError(f"Changed {tag} metrics in {path.name}")
        for tag, data in preserved.items():
            if font.reader[tag] != data:
                raise ValueError(f"Changed {tag} table in {path.name}")


def convert(source, destination, allow_filtered=False):
    with TTFont(source, recalcTimestamp=False, checkChecksums=2) as font:
        outline_tables = {"CFF ", "CFF2"} & set(font.keys())
        if len(outline_tables) != 1 or "fvar" in font:
            raise ValueError(f"Expected a static CFF OpenType font in {source.name}")
        if not (font["post"].isFixedPitch == 1 and font["OS/2"].panose.bProportion == 9):
            raise ValueError(f"Expected fixed-pitch metadata in {source.name}")
        order = font.getGlyphOrder()
        expected_cmap = cmap_ids(font)
        verify_terminal_grid(font, source, allow_filtered)

        # These tables refer to the same glyph IDs in both formats. Keep their
        # encoded data, including language-specific lookups and style links.
        preserved = {tag: font.reader[tag] for tag in ("BASE", "GDEF", "GPOS", "GSUB", "OS/2", "name", "post")}
        glyph_set = font.getGlyphSet()
        horizontal, vertical, glyphs = {}, {}, {}
        max_error = font["head"].unitsPerEm / 1000
        origins = font["VORG"]
        for name in order:
            pen = TTGlyphPen(None)
            glyph_set[name].draw(Cu2QuPen(pen, max_err=max_error, reverse_direction=True))
            glyph = pen.glyph()
            glyph.recalcBounds(None)
            glyphs[name] = glyph
            horizontal[name] = (font["hmtx"][name][0], glyph.xMin)
            # TrueType expresses the same vertical origin as yMax plus the
            # top side bearing. CFF stores it directly in VORG.
            vertical[name] = (font["vmtx"][name][0], origins[name] - glyph.yMax)

        del font[outline_tables.pop()]
        del font["VORG"]
        if "DSIG" in font:
            del font["DSIG"]
        font.sfntVersion = "\x00\x01\x00\x00"
        font["loca"] = newTable("loca")
        glyf = font["glyf"] = newTable("glyf")
        glyf.glyphs = glyphs
        glyf.glyphOrder = order
        builder = FontBuilder(font=font)
        builder.setupMaxp()
        builder.setupHorizontalMetrics(horizontal)
        builder.setupVerticalMetrics(vertical)

        with NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.stem}-",
                                suffix=".tmp", delete=False) as temp:
            temporary = Path(temp.name)
        try:
            font.save(temporary)
            verify_ttf(temporary, expected_cmap, horizontal, vertical, preserved)
            temporary.chmod(0o644)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    print(f"Built {destination.name} ({len(expected_cmap[0])} codepoints)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fonts", nargs="*", type=Path, help="OTF files, default: dist/HanGridMono*.otf")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    sources = list(dict.fromkeys(path.resolve() for path in args.fonts))
    if not sources:
        sources = sorted((ROOT / "dist").glob("HanGridMono*.otf"))
    if not sources or any(source.suffix.lower() != ".otf" or not source.is_file() for source in sources):
        parser.error("Provide one or more existing Han Grid Mono OTF files")
    destinations = [args.output / source.with_suffix(".ttf").name for source in sources]
    if len(set(destinations)) != len(destinations):
        parser.error("OTF filenames must be unique")
    args.output.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for _ in pool.map(convert, sources, destinations):
            pass


if __name__ == "__main__":
    main()
