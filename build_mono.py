#!/usr/bin/env python3
"""Build Pan-CJK CFF OTFs from the Source Han Mono and Sans submodules."""

import argparse
from concurrent.futures import ProcessPoolExecutor
from copy import copy
from io import BytesIO
import json
import math
import os
import hashlib
import re
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from fontTools import subset, unicodedata
from fontTools.cffLib.CFFToCFF2 import convertCFFToCFF2
from fontTools.cffLib.transforms import desubroutinizeCharString
from fontTools.merge import Merger
from fontTools.merge.layout import layoutPostMerge, layoutPreMerge
from fontTools.misc.roundTools import otRound
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables.otBase import OTTableReader, OTTableWriter
from fontTools.ttLib.tables import otTables

ROOT = Path(__file__).resolve().parent
WEIGHTS = ("ExtraLight", "Light", "Normal", "Regular", "Medium", "Bold", "Heavy")
REGIONS = {
    "JP": ("Japanese", "", "J", "1"),
    "KR": ("Korean", "K", "K", "3"),
    "SC": ("SimplifiedChinese", "SC", "SC", "25"),
    "TC": ("TraditionalChinese", "TC", "TC", "2"),
    "HK": ("TraditionalChineseHK", "HC", "HC", "2"),
}
CMAP_LANGUAGES = {"JP": "JP", "KR": "KR", "SC": "CN", "TC": "TW", "HK": "HK"}
MONO_FDS = {"Monospace", "MonospaceCJK", "MonospaceDigits", "Italic", "ItalicCJK", "ItalicDigits"}
LAYOUT = ("GDEF", "GSUB", "GPOS")
CJK_SCRIPTS = {"Hani", "Hira", "Kana", "Hang", "Bopo"}


def git(repo, *args):
    environment = dict(os.environ)
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], env=environment, timeout=60,
    )


def read_sans(repo, ref, region, weight, regional_subset=False):
    directory, suffix, otc_region, script_id = REGIONS[region]
    language = CMAP_LANGUAGES[region]
    stem = f"SourceHanSans{language if regional_subset else suffix}-{weight}"
    if ref:
        folder = f"SubsetOTF/{language}" if regional_subset else f"OTF/{directory}"
        blob = git(repo, "show", f"{ref}:{folder}/{stem}.otf")
        return TTFont(BytesIO(blob), recalcTimestamp=False)

    source = repo / "Masters" / weight
    if regional_subset:
        resource = language
        names = "FontMenuNameDB.SUBSET"
    else:
        source /= "OTC"
        resource = f"OTC.{otc_region}"
        names = "FontMenuNameDB"
    cmap_language = CMAP_LANGUAGES[region]
    with TemporaryDirectory(prefix="han-grid-sans-") as temporary:
        result = Path(temporary) / f"{stem}.otf"
        subprocess.run(
            [
                "makeotf", "-f", str(source / f"cidfont.ps.{resource}"),
                "-omitMacNames", "-ff", str(source / f"features.{resource}"),
                "-fi", str(source / f"cidfontinfo.{resource}"),
                "-mf", str(repo / names), "-r", "-nS", "-cs", script_id,
                "-ch", str(repo / f"UniSourceHanSans{cmap_language}-UTF32-H"),
                "-ci", str(repo / f"SourceHanSans_{cmap_language}_sequences.txt"),
                "-o", str(result),
            ],
            cwd=temporary, check=True, stdout=subprocess.DEVNULL,
        )
        return TTFont(BytesIO(result.read_bytes()), recalcTimestamp=False)


def top_dict(font):
    return font["CFF "].cff.topDictIndex[0]


def mono_codepoints(sans, mono):
    top = top_dict(mono)
    sans_cmap = sans.getBestCmap()
    mono_cmap = mono.getBestCmap()
    mono_fd_numbers = {
        index for index, fd in enumerate(top.FDArray)
        if fd.FontName.rsplit("-", 1)[-1] in MONO_FDS
    }
    if not mono_fd_numbers:
        raise ValueError("Source Han Mono is missing its monospace font dictionaries")
    return {
        cp for cp, glyph in mono_cmap.items()
        if cp not in sans_cmap or top.FDSelect[mono.getGlyphID(glyph)] in mono_fd_numbers
    }


def subset_font(font, codepoints, features, layout_scripts=None):
    # These are already CID-keyed, with self-contained glyphs. Keep CFF private
    # dictionaries and subroutines intact while subsetting the OpenType tables.
    font.getGlyphOrder()
    outlines = font["CFF "]
    del font["CFF "]
    options = subset.Options(
        layout_features=features, name_IDs=["*"], name_languages=["*"],
        name_legacy=True, notdef_outline=True, recalc_average_width=False,
    )
    if layout_scripts is not None:
        options.layout_scripts = layout_scripts
    selection = subset.Subsetter(options=options)
    sequences = {
        sequence: glyph for sequence, glyph in variation_sequences(font).items()
        if sequence[0] in codepoints or sequence[0] not in font.getBestCmap()
    }
    # JP and HK contain explicit variation sequences whose base codepoint is
    # absent from their ordinary cmap. Keep those independently encoded glyphs.
    selectors = {selector for _, selector in sequences}
    selection.populate(
        unicodes=set(codepoints) | selectors,
        glyphs=[glyph for glyph in sequences.values() if glyph is not None],
    )
    selection.subset(font)
    font["CFF "] = outlines


def variation_sequences(font):
    return {
        (codepoint, selector): glyph
        for table in font["cmap"].tables if table.format == 14
        for selector, entries in table.uvsDict.items()
        for codepoint, glyph in entries
    }


def outline_signature(charstring):
    pen = RecordingPen()
    charstring.draw(pen)
    return hash(tuple(pen.value))


def cjk_changes(sans, mono, mono_points, old_codepoints=None, old_variations=None):
    """Match by Unicode and selector, since CIDs changed across versions."""
    sans_cmap, mono_cmap = sans.getBestCmap(), mono.getBestCmap()
    sans_cs = top_dict(sans).CharStrings
    mono_cs = top_dict(mono).CharStrings
    old_codepoints = set(mono_cmap) if old_codepoints is None else old_codepoints
    cjk = {
        cp for cp in sans_cmap.keys() | old_codepoints
        if cp not in mono_points and (
            unicodedata.script(chr(cp)) in CJK_SCRIPTS or 0x3000 <= cp <= 0x303F
        )
    }
    old_uvs = variation_sequences(mono)
    old_variations = set(old_uvs) if old_variations is None else old_variations
    new, changed, cache = [], [], {}
    for cp in sorted(cjk & sans_cmap.keys()):
        if cp not in old_codepoints:
            new.append(f"U+{cp:04X}")
        if cp not in mono_cmap:
            continue
        pair = sans_cmap[cp], mono_cmap[cp]
        if pair not in cache:
            cache[pair] = outline_signature(sans_cs[pair[0]]) != outline_signature(mono_cs[pair[1]])
        if cache[pair]:
            changed.append(f"U+{cp:04X}")
    current_uvs = variation_sequences(sans)
    new_uvs = current_uvs.keys() - old_variations
    changed_uvs = []
    for cp, selector in sorted(current_uvs.keys() & old_uvs.keys()):
        sequence = cp, selector
        pair = current_uvs[sequence] or sans_cmap[cp], old_uvs[sequence] or mono_cmap[cp]
        if pair not in cache:
            cache[pair] = outline_signature(sans_cs[pair[0]]) != outline_signature(mono_cs[pair[1]])
        if cache[pair]:
            changed_uvs.append(f"U+{cp:04X} U+{selector:04X}")
    return {
        "added_cjk_codepoints": new,
        "removed_cjk_codepoints": [f"U+{cp:04X}" for cp in sorted((cjk & old_codepoints) - sans_cmap.keys())],
        "removed_variation_sequences": [f"U+{cp:04X} U+{selector:04X}"
                                        for cp, selector in sorted(old_variations - current_uvs.keys())],
        "changed_variant_contours": changed_uvs,
        "changed_default_cjk_contours": changed,
        "added_variation_sequences": [
            f"U+{cp:04X} U+{selector:04X}" for cp, selector in sorted(new_uvs)
        ],
    }


def rename_layout(font, mapping):
    # Encode using donor GIDs and decode using the final names for those GIDs.
    # This also renames coverage and class references in contextual lookups.
    data = {tag: font.getTableData(tag) for tag in LAYOUT if tag in font}
    font.setGlyphOrder([mapping[glyph] for glyph in font.getGlyphOrder()])
    for tag, blob in data.items():
        table = newTable(tag)
        table.decompile(blob, font)
        table.ensureDecompiled()
        font[tag] = table


def merge_layout(sans, mono):
    merger = Merger()
    merger.beyond64k = False
    merger.duplicateGlyphsPerFont = [{}, {}]
    layoutPreMerge(sans)
    layoutPreMerge(mono)
    for tag in LAYOUT:
        tables = [font.get(tag, NotImplemented) for font in (sans, mono)]
        if any(table is not NotImplemented for table in tables):
            sans[tag] = newTable(tag).merge(merger, tables)
    layoutPostMerge(sans)


def normalized_charstring(charstring, metrics, donor):
    width, _ = metrics
    # Fit the upstream halfwidth and fullwidth glyphs to the same terminal grid.
    target = 0 if width == 0 else (500 if width < 750 else 1000)

    if target == width:
        if donor:
            desubroutinizeCharString(charstring)
        return charstring, metrics

    scale = target / width if target < width else 1
    shift = (target - width * scale) / 2
    pen = T2CharStringPen(target - charstring.private.nominalWidthX, None)
    charstring.draw(TransformPen(pen, (scale, 0, 0, 1, shift, 0)))
    adjusted = pen.getCharString(charstring.private, charstring.globalSubrs)
    bounds = BoundsPen(None)
    adjusted.draw(bounds)
    new_lsb = otRound(bounds.bounds[0]) if bounds.bounds else 0
    return adjusted, (target, new_lsb)


def merge_outlines(sans, mono, mapping):
    sans_top, mono_top = top_dict(sans), top_dict(mono)
    sans_order = list(sans.getGlyphOrder())
    used_donor_fds = {}
    # Resolve all original indexed charstrings before changing the CFF index.
    glyph_data = [(glyph, *sans_top.CharStrings.getItemAndSelector(glyph), False)
                  for glyph in sans_order]
    for original, glyph in mapping.items():
        if glyph == ".notdef":
            continue
        charstring, source_fd = mono_top.CharStrings.getItemAndSelector(original)
        if source_fd not in used_donor_fds:
            used_donor_fds[source_fd] = len(sans_top.FDArray)
            fd = mono_top.FDArray[source_fd]
            # Make the donor independent of its global and local subroutine sets
            # when added below. The private dict still provides its hint zones.
            fd.FontName = f"HanGrid-Mono-{fd.FontName.rsplit('-', 1)[-1]}"
            sans_top.FDArray.append(fd)
        glyph_data.append((glyph, charstring, used_donor_fds[source_fd], True))

    donor_names = {final: original for original, final in mapping.items()}
    charstrings, selectors = [], []
    seen = set()
    for glyph, charstring, fd, donor in glyph_data:
        # CFF conversion mutates individual programs. Keep aliases independent.
        if id(charstring) in seen:
            charstring = copy(charstring)
            charstring.program = charstring.program.copy()
        seen.add(id(charstring))
        if donor:
            original = donor_names[glyph]
            metrics = mono["hmtx"][original]
            sans["vmtx"][glyph] = mono["vmtx"][original]
            sans["VORG"][glyph] = mono["VORG"][original]
        else:
            metrics = sans["hmtx"][glyph]
        charstring, sans["hmtx"][glyph] = normalized_charstring(charstring, metrics, donor)
        charstrings.append(charstring)
        selectors.append(fd)

    for source_fd in used_donor_fds:
        private = mono_top.FDArray[source_fd].Private
        if hasattr(private, "Subrs"):
            del private.Subrs
            private.rawDict.pop("Subrs", None)

    order = [entry[0] for entry in glyph_data]
    index = sans_top.CharStrings.charStringsIndex
    index.items = charstrings
    sans_top.CharStrings.charStrings = {glyph: index for index, glyph in enumerate(order)}
    sans_top.FDSelect.gidArray = selectors
    sans_top.FDSelect.format = None
    sans_top.charset = order
    sans_top.numGlyphs = sans["maxp"].numGlyphs = len(order)
    sans_top.CIDCount = 1 + max(int(glyph[3:]) for glyph in order[1:])
    sans.setGlyphOrder(order)


def single_substitutions(font, tag):
    table = font["GSUB"].table
    indices = {
        index for feature in table.FeatureList.FeatureRecord if feature.FeatureTag == tag
        for index in feature.Feature.LookupListIndex
    }
    pairs = {}
    for index in sorted(indices):
        for subtable in table.LookupList.Lookup[index].SubTable:
            subtable = getattr(subtable, "ExtSubTable", subtable)
            pairs.update(getattr(subtable, "mapping", {}))
    return pairs


def bridge_fullwidth(sans, fullwidth, original_cmap, donor_cmap, mapping, mono_points):
    """Reconnect fwid substitutions from the new Latin glyphs to Sans CJK."""
    sans_cmap = sans.getBestCmap()
    translations = {}
    for cp, glyph in original_cmap.items():
        if cp in sans_cmap and cp not in mono_points:
            translations.setdefault(glyph, set()).add(sans_cmap[cp])
    pairs = {}
    for original in set(donor_cmap.values()):
        candidates = translations.get(fullwidth.get(original), set())
        if len(candidates) == 1:
            pairs[mapping[original]] = next(iter(candidates))
    add_single_substitutions(sans, "fwid", pairs)


def add_single_substitutions(font, tag, pairs):
    if not pairs:
        return
    table = font["GSUB"].table
    subtable = otTables.SingleSubst()
    subtable.mapping = pairs
    lookup = otTables.Lookup()
    lookup.LookupType, lookup.LookupFlag = 1, 0
    lookup.SubTable, lookup.SubTableCount = [subtable], 1
    index = len(table.LookupList.Lookup)
    table.LookupList.Lookup.append(lookup)
    table.LookupList.LookupCount += 1
    for feature in table.FeatureList.FeatureRecord:
        if feature.FeatureTag == tag:
            feature.Feature.LookupListIndex.append(index)
            feature.Feature.LookupCount += 1


def use_terminal_quotes(font):
    """Use the narrow forms for ambiguous characters on a one-cell grid."""
    cmap = dict(font.getBestCmap())
    variants = variation_sequences(font)
    replacements, fullwidth, original_forms = {}, {}, {}
    for cp in (0x2018, 0x2019, 0x201C, 0x201D):
        original = cmap.get(cp)
        if original is None or (cp, 0xFE00) not in variants or (cp, 0xFE01) not in variants:
            continue
        narrow = variants[cp, 0xFE00] or original
        wide = variants[cp, 0xFE01] or original
        if font["hmtx"][narrow][0] != 500 or font["hmtx"][wide][0] != 1000:
            raise ValueError(f"Unexpected width variants for U+{cp:04X}")
        replacements[cp] = narrow
        fullwidth[narrow] = wide
        original_forms[narrow] = original
    for table in font["cmap"].tables:
        if table.format == 14:
            for selector, entries in table.uvsDict.items():
                # Resolve default UVS against the original cmap before changing it.
                table.uvsDict[selector] = [
                    (cp, glyph or cmap[cp]) if cp in replacements else (cp, glyph)
                    for cp, glyph in entries
                ]
        elif table.isUnicode():
            table.cmap.update({cp: glyph for cp, glyph in replacements.items() if cp in table.cmap})
    add_single_substitutions(font, "fwid", fullwidth)
    # Add aliases in the original lookups so regional variants stay separate.
    table = font["GSUB"].table
    lookups = {index for record in table.FeatureList.FeatureRecord
               if record.FeatureTag in {"vert", "vrt2"}
               for index in record.Feature.LookupListIndex}
    for index in lookups:
        for subtable in table.LookupList.Lookup[index].SubTable:
            vertical = getattr(getattr(subtable, "ExtSubTable", subtable), "mapping", {})
            for narrow, original in original_forms.items():
                if original in vertical:
                    vertical.setdefault(narrow, vertical[original])


def postscript_name(family, style, custom=False):
    compact = re.sub(r"[^A-Za-z0-9]", "", family)
    if custom or not compact or any(ord(char) > 127 for char in family):
        compact = (compact or "HanGrid") + hashlib.sha256(family.encode("utf-8")).hexdigest()[:8]
    style_compact = re.sub(r"[^A-Za-z0-9]", "", style)
    if len(compact) + len(style_compact) + 1 > 63:
        digest = hashlib.sha256(compact.encode("utf-8")).hexdigest()[:8]
        compact = compact[:63 - len(style_compact) - 10] + digest
    return f"{compact}-{style_compact}"


def set_names(font, mono, region, weight, italic, version, regional_subset=False, family_name=None):
    family = family_name or "Han Grid Mono"
    style = weight + (" Italic" if italic else "")
    legacy_link = weight in {"Regular", "Bold"}
    legacy_family = family if legacy_link else f"{family} {weight}"
    legacy_style = style.removeprefix("Regular ") if legacy_link else ("Italic" if italic else "Regular")
    fullname = f"{family} {style}"
    ps_name = postscript_name(family, style, custom=family_name not in (None, "Han Grid Mono"))
    names = font["name"]
    renamed_ids = set(range(1, 8)) | {16, 17, 18, 20, 21, 22, 25}
    names.names = [name for name in names.names if name.nameID not in renamed_ids]
    names.setName(
        "Based on Source Han Sans and Source Han Mono by Adobe. "
        "See OFL-SourceHanSans.txt and OFL-SourceHanMono.txt for copyright and license.",
        10, 3, 1, 0x409,
    )
    for name_id, value in {
        1: legacy_family, 2: legacy_style, 3: f"{version};HGMO;{ps_name}",
        4: fullname, 5: f"Version {version}", 6: ps_name, 16: family, 17: style,
    }.items():
        names.setName(value, name_id, 3, 1, 0x409)

    font["head"].fontRevision = float(version)
    font["head"].macStyle = mono["head"].macStyle
    font["OS/2"].version = max(font["OS/2"].version, 4)
    font["OS/2"].achVendID = "HGMO"
    font["OS/2"].fsSelection = mono["OS/2"].fsSelection
    font["OS/2"].usWeightClass = mono["OS/2"].usWeightClass
    font["OS/2"].panose.bProportion = 9
    font["OS/2"].panose.bLetterForm = 9 if italic else 2
    font["OS/2"].recalcAvgCharWidth(font)
    font["OS/2"].recalcUnicodeRanges(font)

    post = font["post"]
    post.isFixedPitch = 1
    post.italicAngle = mono["post"].italicAngle
    font["hhea"].caretSlopeRise = mono["hhea"].caretSlopeRise
    font["hhea"].caretSlopeRun = mono["hhea"].caretSlopeRun
    font["hhea"].caretOffset = mono["hhea"].caretOffset
    top = top_dict(font)
    font["CFF "].cff.fontNames = [ps_name]
    top.FullName, top.FamilyName = fullname, family
    top.CIDFontVersion = float(version)
    top.isFixedPitch = 1
    top.ItalicAngle = post.italicAngle
    for fd in top.FDArray:
        fd.FontName = f"{ps_name}-{fd.FontName.rsplit('-', 1)[-1]}"
    return ps_name


def verify_terminal_grid(font, path, allow_filtered=False, check_extended=False):
    cmap = font.getBestCmap()
    metrics = font["hmtx"].metrics
    widths = {width for width, _ in metrics.values()}
    if (not widths <= {0, 500, 1000}) or (not allow_filtered and widths != {0, 500, 1000}):
        raise ValueError(f"Unexpected advance widths {widths} in {path.name}")
    narrow = set(range(0x20, 0x7F)) | set(range(0xFF66, 0xFF9E))
    if check_extended:
        narrow.update(cp for cp in cmap if unicodedata.east_asian_width(chr(cp)) == "H"
                      and unicodedata.category(chr(cp)) != "Mn")
    wide = (0x3000, *range(0xFF01, 0xFF5F),
            *(cp for cp in map(ord, "你好한") if cp in cmap))
    for codepoints, expected in ((narrow, 500), (wide, 1000)):
        for cp in codepoints:
            glyph = cmap.get(cp)
            if glyph is None and allow_filtered and cp > 0x7F:
                continue
            if glyph is None or metrics[glyph][0] != expected:
                raise ValueError(f"Expected advance {expected} for U+{cp:04X} in {path.name}")


def verify(path, expected, expected_uvs, outline_format):
    with TTFont(path, checkChecksums=2) as font:
        cmap = font.getBestCmap()
        if cmap != expected or variation_sequences(font) != expected_uvs:
            uvs = variation_sequences(font)
            differences = list(expected.items() - cmap.items())[:5]
            uv_differences = list(expected_uvs.items() - uvs.items())[:5]
            raise ValueError(
                f"Changed mappings in {path.name}: {len(cmap)}/{len(expected)} cmap, "
                f"{len(uvs)}/{len(expected_uvs)} UVS; {differences}; {uv_differences}"
            )
        outline_tag = "CFF " if outline_format == "cff1" else "CFF2"
        if outline_tag not in font or "fvar" in font:
            raise ValueError(f"Invalid static {outline_format} font {path.name}")
        if not (font["post"].isFixedPitch == 1 and font["OS/2"].panose.bProportion == 9
                and (outline_format != "cff1" or top_dict(font).isFixedPitch == 1)):
            raise ValueError(f"Incorrect fixed-pitch metadata in {path.name}")
        verify_terminal_grid(font, path)
        for tag in (*LAYOUT, "BASE", "VORG"):
            if tag in font and hasattr(font[tag], "ensureDecompiled"):
                font[tag].ensureDecompiled()


def normalize_encoded_widths(font):
    """Give halfwidth aliases and spacing nonspacing marks their own metrics."""
    top = top_dict(font)
    cmap = font.getBestCmap()
    order = list(font.getGlyphOrder())
    used = set(order)
    replacements = {}
    free_cids = (f"cid{i:05d}" for i in range(1, 65535) if f"cid{i:05d}" not in used)
    for cp, glyph in sorted(cmap.items()):
        width, _ = font["hmtx"][glyph]
        target = width
        if unicodedata.category(chr(cp)) == "Mn":
            target = 0
        elif unicodedata.east_asian_width(chr(cp)) == "H":
            target = 500
        if target == width:
            continue
        if len(order) >= 65535:
            raise ValueError("No room to normalize halfwidth and combining glyphs; enable Windows trimming or select fewer regions")
        cid = next(free_cids, None)
        if cid is None:
            raise ValueError("No free CFF CID for a width alias")
        source, selector = top.CharStrings.getItemAndSelector(glyph)
        bounds = source.calcBounds(top.CharStrings)
        scale = min(1, target / width) if target and width else 1
        shift = 0
        if target == 0 and bounds is not None:
            # With no GPOS attachment, position the mark over the previous cell.
            shift = -250 - (bounds[0] + bounds[2]) / 2
        pen = T2CharStringPen(target - source.private.nominalWidthX, None)
        source.draw(TransformPen(pen, (scale, 0, 0, 1, shift, 0)))
        top.CharStrings.charStrings[cid] = len(order)
        top.CharStrings.charStringsIndex.append(pen.getCharString(source.private, source.globalSubrs))
        top.FDSelect.gidArray.append(selector)
        order.append(cid)
        used.add(cid)
        replacements[cp] = cid
        font["hmtx"][cid] = (target, otRound(bounds[0] * scale + shift) if bounds else 0)
        font["vmtx"][cid] = font["vmtx"][glyph]
        font["VORG"][cid] = font["VORG"][glyph]
    for table in font["cmap"].tables:
        if table.isUnicode() and table.format != 14:
            table.cmap.update({cp: glyph for cp, glyph in replacements.items() if cp in table.cmap})
    top.charset = order
    top.numGlyphs = font["maxp"].numGlyphs = len(order)
    top.CIDCount = 1 + max(int(glyph[3:]) for glyph in order[1:])
    font.setGlyphOrder(order)


def recalc_geometry(font):
    """Calculate horizontal, vertical and global bounds in one CFF traversal."""
    cff_tag = "CFF2" if "CFF2" in font else "CFF "
    strings = font[cff_tag].cff.topDictIndex[0].CharStrings
    hhea, vhea = font["hhea"], font["vhea"]
    h_metrics, v_metrics = font["hmtx"].metrics, font["vmtx"].metrics
    hhea.advanceWidthMax = max(advance for advance, _ in h_metrics.values())
    vhea.advanceHeightMax = max(advance for advance, _ in v_metrics.values())
    x_min, y_min = math.inf, math.inf
    x_max, y_max = -math.inf, -math.inf
    min_lsb, min_rsb, min_tsb, min_bsb = math.inf, math.inf, math.inf, math.inf
    x_extent, y_extent = -math.inf, -math.inf
    for glyph, (width, lsb) in h_metrics.items():
        bounds = strings[glyph].calcBounds(strings)
        if bounds is None:
            continue
        left, bottom, right, top = bounds
        x_min, y_min = min(x_min, left), min(y_min, bottom)
        x_max, y_max = max(x_max, right), max(y_max, top)
        outline_width = math.ceil(right) - math.floor(left)
        outline_height = math.ceil(top) - math.floor(bottom)
        height, tsb = v_metrics[glyph]
        min_lsb, min_rsb = min(min_lsb, lsb), min(min_rsb, width - lsb - outline_width)
        min_tsb, min_bsb = min(min_tsb, tsb), min(min_bsb, height - tsb - outline_height)
        x_extent, y_extent = max(x_extent, lsb + outline_width), max(y_extent, tsb + outline_height)
    head = font["head"]
    bbox = [math.floor(x_min), math.floor(y_min), math.ceil(x_max), math.ceil(y_max)]
    head.xMin, head.yMin, head.xMax, head.yMax = bbox
    if "CFF " in font:
        top_dict(font).FontBBox = bbox
    hhea.minLeftSideBearing, hhea.minRightSideBearing, hhea.xMaxExtent = min_lsb, min_rsb, x_extent
    vhea.minTopSideBearing, vhea.minBottomSideBearing, vhea.yMaxExtent = min_tsb, min_bsb, y_extent
    font.recalcBBoxes = False


def mappings_by_gid(font, cmap, variations):
    return (
        {cp: font.getGlyphID(glyph) for cp, glyph in cmap.items()},
        {sequence: font.getGlyphID(glyph) if glyph is not None else None
         for sequence, glyph in variations.items()},
    )


def verify_cff2(path, expected_ids):
    with TTFont(path, checkChecksums=2) as font:
        cmap, variations = font.getBestCmap(), variation_sequences(font)
        if mappings_by_gid(font, cmap, variations) != expected_ids:
            raise ValueError(f"Glyph IDs changed during CFF2 conversion for {path.name}")
    verify(path, cmap, variations, "cff2")


def build_face(sans_repo, sans_ref, mono_repo, output, region, weight, italic,
               version, outline_format, compare_outlines, regional_subset=False,
               selected_regions=None, mono_ref=None, family_name=None, capacity_whitelist=None):
    def read_mono(region_):
        suffix_ = REGIONS[region_][1]
        filename = f"SourceHanMono{suffix_}-{weight}{'It' if italic else ''}.otf"
        relative = Path(weight) / "OTC" / filename
        source = BytesIO(git(mono_repo, "show", f"{mono_ref}:{relative.as_posix()}")) if mono_ref else mono_repo / relative
        return TTFont(source, recalcTimestamp=False)

    mono = read_mono(region)
    if selected_regions and len(selected_regions) > 1:
        from han_grid.regions import union_sans
        sans = union_sans(sans_repo, sans_ref, selected_regions, region, weight)
    else:
        sans = read_sans(sans_repo, sans_ref, region, weight, regional_subset)
    if sans["head"].unitsPerEm != 1000 or mono["head"].unitsPerEm != 1000:
        raise ValueError("Expected a 1000-unit em in both upstream fonts")

    mono_points = mono_codepoints(sans, mono)
    if regional_subset:
        mono_points &= sans.getBestCmap().keys()
    mono_cmap = dict(mono.getBestCmap())
    sans_points = sans.getBestCmap().keys() - mono_points
    # The Mono fork shares one CFF per weight across regional and italic faces.
    # Its face-specific cmap and layout select the correct upright/italic CIDs.
    differences = None
    if compare_outlines:
        if selected_regions and len(selected_regions) > 1:
            old_coverage, old_variations = set(), set()
            by_region = {}
            for selected in selected_regions:
                with read_mono(selected) as old, read_sans(sans_repo, sans_ref, selected, weight, True) as new:
                    old_coverage.update(old.getBestCmap())
                    old_variations.update(variation_sequences(old))
                    changes = cjk_changes(new, old, mono_codepoints(new, old))
                    by_region[selected] = {key: changes[key] for key in (
                        "changed_default_cjk_contours", "changed_variant_contours"
                    )}
            differences = cjk_changes(sans, mono, mono_points, old_coverage, old_variations)
            differences.pop("changed_default_cjk_contours")
            differences.pop("changed_variant_contours")
            differences["changed_by_region"] = by_region
        else:
            differences = cjk_changes(sans, mono, mono_points)
    upstream_uvs = variation_sequences(sans)
    overlap = {cp for cp, _ in upstream_uvs} & mono_points
    # Retain the upstream base outline for an explicit default-style variant
    # when a plain codepoint (notably curly quotes) uses an old Mono glyph.
    original_sans_cmap = sans.getBestCmap()
    for table in sans["cmap"].tables:
        if table.format == 14:
            for selector, entries in table.uvsDict.items():
                table.uvsDict[selector] = [
                    (cp, original_sans_cmap[cp] if cp in overlap and glyph is None else glyph)
                    for cp, glyph in entries
                ]
    expected_uvs = variation_sequences(sans)
    fullwidth = single_substitutions(mono, "fwid")
    sans_version = sans["name"].getDebugName(5)
    # Prefer Sans for CJK alternates and avoid importing a second set of
    # enclosed/fullwidth glyphs and proportional Latin alternates.
    sans_features = {
        record.FeatureTag for tag in ("GSUB", "GPOS")
        for record in sans[tag].table.FeatureList.FeatureRecord
    } - {"aalt", "calt", "liga", "pwid", "halt", "kern"}
    subset_font(sans, sans_points | overlap, sorted(sans_features))
    # Match donor script/language coverage to the regional source so no
    # cross-region donor lookups can bring foreign alternates back in.
    scripts = None
    if regional_subset:
        scripts = [
            f"{record.ScriptTag}.{language}"
            for record in sans["GSUB"].table.ScriptList.ScriptRecord
            for language in ["dflt", *(entry.LangSysTag.strip() for entry in record.Script.LangSysRecord)]
        ]
    subset_font(mono, mono_points, ["ccmp", "hist", "mark", "vert", "vrt2", "zero"], scripts)
    remaining_cids = set(sans.getGlyphOrder())
    available = iter(f"cid{cid:05d}" for cid in range(1, 65535)
                     if f"cid{cid:05d}" not in remaining_cids)
    mapping = {".notdef": ".notdef"}
    for glyph in mono.getGlyphOrder()[1:]:
        new_name = next(available, None)
        if new_name is None:
            raise ValueError(
                f"Combined font exceeds the CFF1 limit: {len(remaining_cids)} Sans glyphs "
                f"and {len(mono.getGlyphOrder()) - 1} Mono glyphs"
            )
        mapping[glyph] = new_name

    bridge_fullwidth(sans, fullwidth, mono_cmap, mono.getBestCmap(), mapping, mono_points)
    for table in sans["cmap"].tables:
        if table.isUnicode() and table.format in {4, 12}:
            table.cmap.update({
                cp: mapping[glyph] for cp, glyph in mono.getBestCmap().items()
                if cp <= 0xFFFF or table.format == 12
            })
    for (cp, selector), glyph in variation_sequences(mono).items():
        if (cp, selector) in expected_uvs:
            continue
        glyph = mapping[glyph] if glyph is not None else None
        for table in sans["cmap"].tables:
            if table.format == 14:
                table.uvsDict.setdefault(selector, []).append((cp, glyph))
        expected_uvs[cp, selector] = glyph
    expected = dict(sans.getBestCmap())
    if expected.keys() != sans_points | mono_points:
        raise ValueError("Combined cmap does not contain all expected codepoints")

    # Rename donor layout after capturing its original glyph metrics and outlines.
    donor = TTFont()
    donor.setGlyphOrder(mono.getGlyphOrder())
    for tag in LAYOUT:
        if tag in mono:
            donor[tag] = mono[tag]
    rename_layout(donor, mapping)
    merge_layout(sans, donor)
    merge_outlines(sans, mono, mapping)
    label = "TW" if family_name is not None and region == "TC" else region
    if selected_regions:
        priority = [region, *(item for item in selected_regions if item != region)]
        label = "+".join("TW" if item == "TC" else item for item in priority)
    capacity_removed = 0
    if capacity_whitelist is not None and len(sans.getGlyphOrder()) > 65000:
        # Prune the same repertoire earlier when the untrimmed intermediate
        # leaves too few GIDs for regional aliases, narrow forms, and Braille.
        from han_grid.windows import is_cjk
        keep = {cp for cp in expected if not is_cjk(cp) or cp in capacity_whitelist}
        retained_uvs = {pair: glyph for pair, glyph in expected_uvs.items()
                        if not is_cjk(pair[0]) or pair[0] in capacity_whitelist}
        for table in sans["cmap"].tables:
            if table.format == 14:
                for selector, entries in table.uvsDict.items():
                    table.uvsDict[selector] = [(cp, glyph) for cp, glyph in entries if (cp, selector) in retained_uvs]
        sub = subset.Subsetter(options=subset.Options(
            layout_features=["*"], name_IDs=["*"], name_languages=["*"],
            name_legacy=True, notdef_outline=True,
        ))
        sub.populate(unicodes=keep | {selector for _, selector in retained_uvs},
                     glyphs=[glyph for glyph in retained_uvs.values() if glyph is not None])
        cff = sans["CFF "]
        del sans["CFF "]
        sub.subset(sans)
        sans["CFF "] = cff
        top = top_dict(sans)
        order = list(sans.getGlyphOrder())
        selected = [top.CharStrings.getItemAndSelector(glyph) for glyph in order]
        top.CharStrings.charStringsIndex.items = [charstring for charstring, _ in selected]
        top.CharStrings.charStrings = {glyph: index for index, glyph in enumerate(order)}
        top.FDSelect.gidArray = [selector for _, selector in selected]
        top.charset = order
        top.numGlyphs = sans["maxp"].numGlyphs = len(order)
        for table in sans["cmap"].tables:
            if table.isUnicode() and table.format != 14:
                table.cmap = {cp: glyph for cp, glyph in table.cmap.items() if cp in keep}
        if sans.getBestCmap().keys() != keep or variation_sequences(sans).keys() != retained_uvs.keys():
            raise ValueError("Early Windows pruning changed the retained repertoire")
        capacity_removed = len(expected) - len(keep)
        expected_uvs = variation_sequences(sans)
    normalize_encoded_widths(sans)
    use_terminal_quotes(sans)
    expected = dict(sans.getBestCmap())
    if variation_sequences(sans).keys() != expected_uvs.keys():
        raise ValueError("Terminal quote mapping changed variation sequence coverage")
    expected_uvs = variation_sequences(sans)
    set_names(sans, mono, label, weight, italic, version, regional_subset, family_name)
    # Coverage labels belong to the filename, not the installed font identity.
    file_family = f"{family_name or 'Han Grid Mono'} {label}" + (" Subset" if regional_subset else "")
    style = weight + (" Italic" if italic else "")
    file_stem = postscript_name(file_family, style, custom=family_name not in (None, "Han Grid Mono"))

    recalc_geometry(sans)
    expected_ids = mappings_by_gid(sans, expected, expected_uvs)
    if outline_format == "cff2":
        convertCFFToCFF2(sans)
    path = output / f"{file_stem}.otf"
    temp_path = path.with_suffix(".tmp")
    try:
        sans.save(temp_path)
        if outline_format == "cff2":
            verify_cff2(temp_path, expected_ids)
        else:
            verify(temp_path, expected, expected_uvs, "cff1")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)
        sans.close()
        mono.close()
    print(f"Built {path.name} ({len(expected)} codepoints)", flush=True)
    return path.name, {
        "region": "TW" if regional_subset and region == "TC" else region, "regional_subset": regional_subset, "weight": weight, "italic": italic, "spacing": "dual",
        "outline_format": outline_format,
        "glyphs": len(sans.getGlyphOrder()), "codepoints": len(expected),
        "mono_codepoints": len(mono_points), "variation_sequences": len(expected_uvs),
        "sans_version": sans_version, "capacity_trimmed_codepoints": capacity_removed,
        **(differences or {}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sans", type=Path, default=ROOT / "Src/SourceHanSans")
    parser.add_argument("--mono", type=Path, default=ROOT / "Src/SourceHanMonoOldVersion")
    parser.add_argument("--sans-ref", help="Read compiled OTFs from this local Git ref instead of building Sans sources")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--regions", nargs="+", choices=[*REGIONS, "TW"], default=list(REGIONS))
    parser.add_argument("--weights", nargs="+", choices=WEIGHTS, default=list(WEIGHTS))
    parser.add_argument("--subset", action="store_true", help="Use upstream region-specific glyph and character subsets")
    parser.add_argument("--styles", choices=("both", "upright", "italic"), default="both")
    parser.add_argument("--outline-format", choices=("cff1", "cff2"), default="cff2")
    parser.add_argument("--version", default="1.101", help="Output version with three decimal places")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    args.regions = ["TC" if region == "TW" else region for region in args.regions]
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if not args.sans_ref and shutil.which("makeotf") is None:
        parser.error("Install Adobe AFDKO for source builds, or use --sans-ref 2.005R with the local release tag")
    if not args.version.partition(".")[0].isdecimal() or not (
        len(args.version.partition(".")[2]) == 3 and args.version.partition(".")[2].isdecimal()
    ):
        parser.error("--version must have three decimal places")
    args.sans, args.mono = args.sans.resolve(), args.mono.resolve()
    report = {
        "sans_checkout": git(args.sans, "rev-parse", "HEAD").decode().strip(),
        "sans_release_commit": git(args.sans, "rev-parse", f"{args.sans_ref}^{{commit}}").decode().strip()
        if args.sans_ref else None,
        "mono_checkout": git(args.mono, "rev-parse", "HEAD").decode().strip(),
        "font_version": args.version,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    styles = {"both": (False, True), "upright": (False,), "italic": (True,)}[args.styles]
    comparison_weight = "Regular" if "Regular" in args.weights else args.weights[0]
    tasks = [
        (args.sans, args.sans_ref, args.mono, args.output, region, weight, italic,
         args.version, args.outline_format,
         weight == comparison_weight and italic == styles[0], args.subset)
        for weight in dict.fromkeys(args.weights) for region in dict.fromkeys(args.regions)
        for italic in styles
    ]
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(build_face, *task) for task in tasks]
        report["fonts"] = dict(future.result() for future in futures)
    from han_grid.io import SCHEMA, stamp_font
    from han_grid.validation import coverage_digest, group_name
    for filename, summary in report["fonts"].items():
        path = args.output / filename
        region = summary["region"].lower().replace("tc", "tw")
        coverage = "subset" if summary["regional_subset"] else "pan-cjk"
        family = "Han Grid Mono"
        with TTFont(path) as font:
            digest = coverage_digest(font)
        stamp_font(path, {
            "schema": SCHEMA, "producer": "han-grid", "coverage": coverage,
            "languages": [region], "default_region": region,
            "family_base": family, "family_name": family,
            "group": group_name([region], region, coverage, family), "version": args.version,
            "weight": summary["weight"], "italic": summary["italic"], "nerd": False,
            "coverage_digest": digest, "braille": "preserve",
        })
    shutil.copyfile(args.sans / "LICENSE.txt", args.output / "OFL-SourceHanSans.txt")
    shutil.copyfile(args.mono / "LICENSE.md", args.output / "OFL-SourceHanMono.txt")
    (args.output / "build-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
