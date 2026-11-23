"""Native eight-dot Braille outlines, independent of Nerd Fonts or FontForge."""
from fontTools.ttLib import TTFont
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.recordingPen import DecomposingRecordingPen
from .io import temporary_output

STYLES = ('circle', 'rectangle', 'gapless', 'preserve')


def draw(pen, bits, style, line=None):
    # Two columns, four rows, within one 500-unit cell and the standard em.
    for bit, (column, row) in enumerate(((0,0),(0,1),(0,2),(1,0),(1,1),(1,2),(0,3),(1,3))):
        if not bits & (1 << bit):
            continue
        x, y = 125 + 250 * column, 750 - 250 * row
        r = 70
        if style == 'circle':
            k = r * 0.5522847498307936
            pen.moveTo((x+r,y))
            pen.curveTo((x+r,y+k),(x+k,y+r),(x,y+r))
            pen.curveTo((x-k,y+r),(x-r,y+k),(x-r,y))
            pen.curveTo((x-r,y-k),(x-k,y-r),(x,y-r))
            pen.curveTo((x+k,y-r),(x+r,y-k),(x+r,y))
        else:
            rx, ry = (125,125) if style == 'gapless' else (70,90)
            bottom, top = y - ry, y + ry
            if style == 'gapless' and line:
                upper, lower = line
                top = round(upper - row * (upper - lower) / 4)
                bottom = round(upper - (row + 1) * (upper - lower) / 4)
            pen.moveTo((x - rx, bottom))
            pen.lineTo((x + rx, bottom))
            pen.lineTo((x + rx, top))
            pen.lineTo((x - rx, top))
        pen.closePath()


def verify(font, style):
    cmap, glyphs = font.getBestCmap(), font.getGlyphSet()
    signatures = set()
    for bits in range(256):
        cp = 0x2800 + bits
        if cp not in cmap or font['hmtx'][cmap[cp]][0] != 500:
            raise ValueError(f'Braille missing or not single-cell: U+{cp:04X}')
        pen = DecomposingRecordingPen(glyphs)
        glyphs[cmap[cp]].draw(pen)
        if sum(op == 'closePath' for op, _ in pen.value) != bits.bit_count():
            raise ValueError(f'Incorrect Braille dot count: U+{cp:04X}')
        if bits and style == 'circle' and not any(op in {'curveTo', 'qCurveTo'} for op, _ in pen.value):
            raise ValueError('Round Braille lost curved outlines')
        signatures.add(repr(pen.value))
    if len(signatures) != 256:
        raise ValueError('Braille patterns are not distinct')


def apply(path, style='circle'):
    if style == 'preserve':
        return
    if style not in STYLES:
        raise ValueError(f'Unknown Braille style: {style}')
    with TTFont(path, recalcTimestamp=False) as font:
        hhea = font['hhea']
        line = (hhea.ascent, hhea.descent - hhea.lineGap)
        cmap, order = dict(font.getBestCmap()), list(font.getGlyphOrder())
        missing = sum(cp not in cmap for cp in range(0x2800,0x2900))
        if len(order) + missing > 65535:
            raise ValueError('No room for 256 Braille patterns; enable Windows trimming or use --braille preserve')
        cff_tag = next((tag for tag in ('CFF2','CFF ') if tag in font), None)
        if cff_tag:
            top = font[cff_tag].cff.topDictIndex[0]
            strings = top.CharStrings
            # Materialize before extending a lazy INDEX, preserving existing charstrings.
            if strings.charStringsAreIndexed:
                for index in range(len(strings.charStringsIndex)):
                    strings.charStringsIndex[index]
        for bits in range(256):
            cp = 0x2800 + bits
            name = cmap.get(cp)
            if name is None:
                name = f'hgrdBraille{cp:04X}'
                while name in order:
                    name += '_'
                order.append(name)
                cmap[cp] = name
                if cff_tag and strings.charStringsAreIndexed:
                    strings.charStrings[name] = len(strings.charStringsIndex)
                    strings.charStringsIndex.append(None)
                    if hasattr(top, 'FDSelect'):
                        top.FDSelect.gidArray.append(0)
            if cff_tag:
                private = top.FDArray[0].Private if hasattr(top,'FDArray') else top.Private
                pen = T2CharStringPen(None if cff_tag == 'CFF2' else 500, None)
                draw(pen, bits, style, line)
                strings[name] = pen.getCharString(private=private, globalSubrs=font[cff_tag].cff.GlobalSubrs)
                if hasattr(top,'FDSelect'):
                    top.FDSelect.gidArray[order.index(name)] = 0
            else:
                pen = TTGlyphPen(None)
                draw(Cu2QuPen(pen, max_err=1, reverse_direction=True), bits, style, line)
                font['glyf'][name] = pen.glyph()
            left = bool(bits & 0x47)
            xmin = (0 if style == 'gapless' else 55) + (0 if left else 250) if bits else 0
            font['hmtx'][name] = (500, xmin)
            if 'vmtx' in font:
                origin = font['VORG'][name] if 'VORG' in font else 880
                rows = [row for bit, row in enumerate((0,1,2,0,1,2,3,3)) if bits & (1 << bit)]
                radius_y = 70 if style == 'circle' else 125 if style == 'gapless' else 90
                ymax = 750 - 250 * min(rows) + radius_y if rows else 0
                if rows and style == 'gapless':
                    ymax = round(line[0] - min(rows) * (line[0] - line[1]) / 4)
                font['vmtx'][name] = (1000, origin - ymax)
        font.setGlyphOrder(order)
        font['maxp'].numGlyphs = len(order)
        if cff_tag == 'CFF ':
            top.charset = order
        for table in font['cmap'].tables:
            if table.isUnicode() and table.format in (4,12):
                table.cmap.update({cp:cmap[cp] for cp in range(0x2800,0x2900)})
        font['OS/2'].recalcUnicodeRanges(font)
        with temporary_output(path) as temporary:
            font.save(temporary)
            with TTFont(temporary) as result:
                verify(result, style)
