#!/usr/bin/env python3
"""Restore the fixed-pitch flag on Nerd-patched Han Grid Mono static outline fonts."""

import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables.DefaultTable import DefaultTable


def validate_grid(font, trusted=False):
    if not ({"glyf", "CFF ", "CFF2"} & set(font.keys())) or "fvar" in font:
        raise ValueError("Only static outline fonts are supported")
    family = font["name"].getDebugName(1) or ""
    if not trusted and not (family.startswith(("HanGridMono", "Han Grid Mono")) and ("Nerd Font" in family or family.endswith(" NF"))):
        raise ValueError("Expected a Nerd-patched Han Grid Mono font")
    panose = font["OS/2"].panose
    if (panose.bFamilyType, panose.bProportion) != (2, 9):
        raise ValueError("Expected the existing monospaced PANOSE classification")
    cmap = font.getBestCmap()
    metrics = font["hmtx"].metrics
    for char, width in ((" ", 500), ("A", 500), ("0", 500), ("中", 1000)):
        if trusted and char == "中" and ord(char) not in cmap:
            continue
        if ord(char) not in cmap or metrics[cmap[ord(char)]][0] != width:
            raise ValueError(f"Expected {char!r} to have advance {width}")
    mapped = set(cmap.values())
    for glyph, (width, _) in metrics.items():
        if width in (0, 500, 1000):
            continue
        # FontForge adds this unmapped, empty control glyph with advance 333.
        # Leave it intact: CoreText accepts the font once post is restored.
        if width == 333 and glyph not in mapped:
            from fontTools.pens.boundsPen import BoundsPen
            glyph_set = font.getGlyphSet()
            pen = BoundsPen(glyph_set)
            glyph_set[glyph].draw(pen)
            if pen.bounds is None:
                continue
        raise ValueError(f"Unexpected advance {width} for {glyph}; refusing to label it fixed-pitch")


def repair(source, destination, trusted=False):
    if source.resolve() == destination.resolve():
        raise ValueError("Use a separate output file; the input must remain intact")
    if destination.exists():
        raise FileExistsError(f"Output already exists: {destination}")
    with TTFont(source, checkChecksums=2) as font:
        validate_grid(font, trusted)
    # Use a fresh lazy reader after inspection. Recompiling decoded FontForge
    # tables can otherwise normalize cmap/name or recalculate glyf bounds.
    with TTFont(source, lazy=True, recalcTimestamp=False,
                recalcBBoxes=False, checkChecksums=2) as font:
        original = {tag: font.reader[tag] for tag in font.reader.keys()}
        # Preserve the original glyph-name encoding in post format 2.
        post = DefaultTable("post")
        post.data = original["post"][:12] + b"\x00\x00\x00\x01" + original["post"][16:]
        font["post"] = post
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=destination.parent, prefix=".nerd-mono-",
                                suffix=".tmp", delete=False) as temp:
            temporary = Path(temp.name)
        try:
            font.save(temporary)
            with TTFont(temporary, recalcTimestamp=False, checkChecksums=2) as result:
                validate_grid(result, trusted)
                if result["post"].isFixedPitch != 1:
                    raise ValueError("Fixed-pitch flag was not saved")
                if set(result.reader.keys()) != set(original):
                    raise ValueError("Font table set changed")
                for tag, data in original.items():
                    actual = result.reader[tag]
                    if tag == "head":
                        # checkSumAdjustment necessarily changes after editing post.
                        data, actual = data[:8] + data[12:], actual[:8] + actual[12:]
                    elif tag == "post":
                        # isFixedPitch is the four-byte field at offset 12.
                        data, actual = data[:12] + data[16:], actual[:12] + actual[16:]
                    if actual != data:
                        raise ValueError(f"Unexpected change to {tag}")
            temporary.chmod(0o644)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Repaired {destination}; outlines, advances, mappings and layout tables preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Nerd-patched Han Grid Mono static OTF/TTF")
    parser.add_argument("--output", type=Path, required=True, help="New OTF/TTF file; must not exist")
    args = parser.parse_args()
    try:
        repair(args.source, args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
