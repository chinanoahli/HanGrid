"""Static CFF conversions at the FontForge compatibility boundary."""

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.cffLib.CFFToCFF2 import convertCFFToCFF2
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables.DefaultTable import DefaultTable

from build_mono import mappings_by_gid, variation_sequences
from .io import temporary_output


def signature(font):
    return {
        "cmap": mappings_by_gid(font, font.getBestCmap(), variation_sequences(font)),
        "metrics": [font["hmtx"][glyph] for glyph in font.getGlyphOrder()],
        "vertical": [font["vmtx"][glyph] for glyph in font.getGlyphOrder()] if "vmtx" in font else None,
        "glyphs": font["maxp"].numGlyphs,
    }


def cff1_for_fontforge(source, destination):
    """Create a name-keyed compatibility font with the same cubic outlines."""
    with TTFont(source, recalcTimestamp=False, recalcBBoxes=False) as font:
        outline_tag = "CFF2" if "CFF2" in font else "CFF "
        if outline_tag not in font:
            raise ValueError(f"Expected CFF outlines: {source}")
        names = font.getGlyphOrder()
        expected = signature(font)
        postscript_name = font["name"].getDebugName(6)
        originals = {tag: font.reader[tag] for tag in font.reader.keys() if tag != outline_tag}
        glyph_set = font.getGlyphSet()
        strings = {}
        for name in names:
            width = font["hmtx"][name][0]
            pen = T2CharStringPen(width, glyph_set, roundTolerance=0)
            glyph_set[name].draw(pen)
            strings[name] = pen.getCharString()
        del font[outline_tag]
        builder = FontBuilder(font=font)
        builder.setupCFF(postscript_name, {
            # CFF1 strings are Latin-1. Unicode menu names remain in the
            # original name table and are restored after FontForge processing.
            "FullName": postscript_name,
            "FamilyName": postscript_name.rsplit("-", 1)[0],
            "isFixedPitch": True,
            "FontBBox": [font["head"].xMin, font["head"].yMin, font["head"].xMax, font["head"].yMax],
            "ItalicAngle": font["post"].italicAngle,
        }, strings, {})
        # Non-outline tables are GID-based; preserve their encoded data.
        for tag, data in originals.items():
            table = DefaultTable(tag)
            table.data = data
            font[tag] = table
        font.setGlyphOrder(names)
        with temporary_output(destination) as temporary:
            font.save(temporary)
            with TTFont(temporary) as result:
                if signature(result) != expected:
                    raise ValueError("CFF1 compatibility conversion changed mappings or metrics")


def cff2_from_fontforge(source, destination, vertical_reference=None):
    with TTFont(source, recalcTimestamp=False, recalcBBoxes=False) as font:
        if "CFF " not in font:
            raise ValueError(f"FontForge did not generate a CFF OTF: {source}")
        font.getGlyphOrder()
        expected = signature(font)
        if vertical_reference:
            with TTFont(vertical_reference) as original:
                if "VORG" in original:
                    before = original["VORG"]
                    vorg = font["VORG"] = newTable("VORG")
                    vorg.majorVersion, vorg.minorVersion = before.majorVersion, before.minorVersion
                    vorg.defaultVertOriginY = before.defaultVertOriginY
                    new_cmap = font.getBestCmap()
                    names = set(font.getGlyphOrder())
                    matches = {name: name for name in before.VOriginRecords if name in names}
                    matches.update({old: new_cmap[cp] for cp, old in original.getBestCmap().items()
                                    if cp in new_cmap and old in before.VOriginRecords})
                    new_uvs = variation_sequences(font)
                    for sequence, old in variation_sequences(original).items():
                        new = new_uvs.get(sequence) if old in before.VOriginRecords else None
                        if new:
                            matches[old] = new
                    missing = set(before.VOriginRecords) - matches.keys()
                    if missing:
                        raise ValueError(f"Unable to restore {len(missing)} vertical glyph origins")
                    vorg.VOriginRecords = {matches[name]: origin for name, origin in before.VOriginRecords.items()}
        convertCFFToCFF2(font)
        font["post"].isFixedPitch = 1
        with temporary_output(destination) as temporary:
            font.save(temporary)
            with TTFont(temporary) as result:
                if signature(result) != expected:
                    raise ValueError("CFF2 output conversion changed mappings or metrics")
