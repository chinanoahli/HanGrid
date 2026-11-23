"""Check horizontal ink against the grid, and size newly added Nerd glyphs."""

from collections import Counter
from math import isfinite

from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont
from fontTools.unicodedata import category, east_asian_width

from .io import temporary_output

CELL = 500
# Allow shallow side bearings and intentional italic overhang in diagnostics.
OVERHANG_TOLERANCE = 32
# Powerline separators deliberately bleed across the adjoining cell.
POWERLINE_SEPARATORS = range(0xE0B0, 0xE0D8)
CURLY_QUOTES = tuple(map(ord, "‘’“”"))


def outline_bounds(glyphs, name):
    pen = BoundsPen(glyphs)
    glyphs[name].draw(pen)
    bounds = pen.bounds
    if bounds is not None and (not all(isfinite(value) for value in bounds)
                               or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
        raise ValueError(f"Invalid outline bounds for {name}: {bounds}")
    return bounds


def audit_geometry(font, strict_quotes=False):
    """Walk every outline, including variants, ligatures and unencoded glyphs."""
    from build_mono import variation_sequences

    metrics = font["hmtx"].metrics
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    uvs = variation_sequences(font)
    labels = {}
    for cp, name in sorted(cmap.items()):
        labels.setdefault(name, f"U+{cp:04X}")
    for (cp, selector), name in sorted(uvs.items()):
        if name is not None:
            labels.setdefault(name, f"U+{cp:04X} U+{selector:04X}")
    quotes = {}
    counts = Counter()
    expected_widths = {}
    for cp, name in cmap.items():
        width_class = east_asian_width(chr(cp))
        if 0x20 <= cp <= 0x7E or (width_class in {"H", "W", "F"}
                                 and category(chr(cp))[0] not in "MC"):
            expected_widths.setdefault(name, set()).add(1000 if width_class in {"W", "F"} else 500)
    for cp in CURLY_QUOTES:
        if cp not in cmap:
            continue
        quotes[cmap[cp]] = CELL
        for selector, width in ((0xFE00, CELL), (0xFE01, 2 * CELL)):
            if (cp, selector) in uvs:
                name = uvs[cp, selector] or cmap[cp]
                # Check per mapping when a malformed font aliases the variants.
                if metrics[name][0] != width:
                    counts["quote_variant_advance"] += 1
                    if strict_quotes:
                        raise ValueError(f"Incorrect advance for U+{cp:04X} U+{selector:04X}")
                quotes.setdefault(name, width)

    examples = []
    for name in font.getGlyphOrder():
        counts["checked"] += 1
        if name not in metrics:
            raise ValueError(f"Missing horizontal metrics for {name}")
        width, _ = metrics[name]
        bounds = outline_bounds(glyphs, name)
        reason = None
        if name in quotes and (width != quotes[name] or bounds is None
                               or bounds[0] < -1 or bounds[2] > quotes[name] + 1):
            reason = "quote_cell"
            if strict_quotes:
                raise ValueError(f"{labels.get(name, name)} escapes its {quotes[name]}-unit quote cell: "
                                 f"advance={width}, bounds={bounds}")
        if bounds is None:
            counts["empty"] += 1
        elif width == 0:
            # Combining marks frequently draw over the previous cell by design.
            counts["zero_advance_ink"] += 1
        elif reason is None and (bounds[0] < -OVERHANG_TOLERANCE or bounds[2] > width + OVERHANG_TOLERANCE):
            reason = "overhang"
        if reason is None and name in expected_widths and expected_widths[name] != {width}:
            reason = "unicode_width_mismatch"
        if reason:
            counts[reason] += 1
            if len(examples) < 16:
                examples.append({"glyph": name, "character": labels.get(name), "reason": reason,
                                 "advance": width, "bounds": list(bounds) if bounds else None})
    return {**counts, "strict_quotes": strict_quotes, "examples": examples}


def private_use(cp):
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD


def size_nerd_icons(path, original, strict_quotes=False):
    """Adjust only new private-use glyphs; leave base aliases and separators alone."""
    from build_mono import variation_sequences

    with temporary_output(path) as temporary:
        with TTFont(path, recalcTimestamp=False, recalcBBoxes=False) as font:
            cmap = font.getBestCmap()
            metrics = font["hmtx"].metrics
            protected = {
                name for table in font["cmap"].tables if table.isUnicode() and table.format != 14
                for cp, name in table.cmap.items() if cp in original or not private_use(cp)
            }
            protected.update(name for name in variation_sequences(font).values() if name is not None)
            separators = {cmap[cp] for cp in POWERLINE_SEPARATORS if cp in cmap}
            icons = {name for cp, name in cmap.items() if private_use(cp) and cp not in original}
            glyphs = font.getGlyphSet()
            counts = Counter()
            for name in sorted(icons - protected):
                bounds = outline_bounds(glyphs, name)
                if bounds is None:
                    continue
                left, _, right, _ = bounds
                previous, bearing = metrics[name]
                if (name in separators and left >= -OVERHANG_TOLERANCE
                        and max(right, right - left) <= previous + OVERHANG_TOLERANCE):
                    counts["separator_glyphs"] += 1
                    continue
                # Both the drawn span and the ink's position matter. Ignore a
                # few units of rounding on icons designed to fill one cell.
                width = 2 * CELL if max(right, right - left) > CELL + 2 else CELL
                counts["double_cell" if width == 2 * CELL else "single_cell"] += 1
                if left < -OVERHANG_TOLERANCE or right > width + OVERHANG_TOLERANCE:
                    counts["remaining_overhang"] += 1
                if width != previous:
                    metrics[name] = width, bearing
                    counts["adjusted"] += 1
            if icons:
                header = font["hhea"]
                header.advanceWidthMax = max(advance for advance, _ in metrics.values())
                # Bearings stay fixed; only the right-side metrics change.
                header.minRightSideBearing = min(
                    advance - (round(bounds[2]) - round(bounds[0])) - bearing
                    for name, (advance, bearing) in metrics.items()
                    if (bounds := outline_bounds(glyphs, name)) is not None
                )
                font["OS/2"].recalcAvgCharWidth(font)
            font.save(temporary)
        with TTFont(temporary, checkChecksums=2) as output:
            summary = audit_geometry(output, strict_quotes=strict_quotes)
    return {**summary, "nerd_icons": {**counts, "shared_base_glyphs": len(icons & protected)}}
