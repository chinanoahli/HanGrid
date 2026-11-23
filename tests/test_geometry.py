"""Regression checks for the previously observed quote and icon overlaps."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

from han_grid.geometry import audit_geometry, outline_bounds, size_nerd_icons
from test_han_grid import make_font


def add_glyph(font, cp, left, right, width):
    name = f"uni{cp:04X}"
    pen = TTGlyphPen(None)
    pen.moveTo((left, 0))
    pen.lineTo((right, 0))
    pen.lineTo((right, 500))
    pen.lineTo((left, 500))
    pen.closePath()
    order = [*font.getGlyphOrder(), name]
    font["glyf"][name] = pen.glyph()
    font.setGlyphOrder(order)
    font["hmtx"][name] = width, left
    font["vmtx"][name] = 1000, 0
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap[cp] = name


class GeometryTests(unittest.TestCase):
    def test_quote_ink_outside_first_cell_rejects_a_valid_advance(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "input.ttf"
            make_font(path)
            with TTFont(path) as font:
                add_glyph(font, 0x201C, 624, 963, 500)
                result = audit_geometry(font)
                self.assertEqual(result["checked"], len(font.getGlyphOrder()))
                self.assertEqual(result["quote_cell"], 1)
                # The fixture also has a visible combining mark with zero advance.
                self.assertEqual(result["zero_advance_ink"], 1)
                with self.assertRaisesRegex(ValueError, "U\\+201C escapes"):
                    audit_geometry(font, strict_quotes=True)

    def test_resize_only_new_icons_keep_outline_and_connector(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "patched.ttf"
            make_font(path)
            with TTFont(path) as font:
                # Settings icon, typical narrow icon, Powerline, pre-existing PUA.
                for cp, left, right in ((0xF013, 0, 872), (0xF014, 10, 470),
                                        (0xE0B0, -30, 500), (0xE0C2, 1, 1025), (0xE123, 0, 900)):
                    add_glyph(font, cp, left, right, 500)
                # A new PUA alias must not give ordinary text a wider advance.
                for table in font["cmap"].tables:
                    table.cmap[0xE124] = "uni0041"
                font.save(path)
            result = size_nerd_icons(path, {0xE123, ord("A")})
            self.assertEqual(result["nerd_icons"]["adjusted"], 2)
            self.assertEqual(result["nerd_icons"]["separator_glyphs"], 1)
            with TTFont(path) as font:
                cmap = font.getBestCmap()
                for cp, expected in ((0xF013, 1000), (0xF014, 500), (0xE0B0, 500), (0xE0C2, 1000),
                                     (0xE123, 500), (ord("A"), 500), (0xE124, 500)):
                    self.assertEqual(font["hmtx"][cmap[cp]][0], expected)
                self.assertEqual(outline_bounds(font.getGlyphSet(), cmap[0xF013]), (0, 0, 872, 500))
