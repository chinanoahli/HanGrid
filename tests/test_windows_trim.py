"""Regression checks for CJK-only pruning, collections, naming and Braille geometry."""
import json
from pathlib import Path
import unittest
from tempfile import TemporaryDirectory
from fontTools.ttLib import TTFont, TTCollection, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from test_han_grid import make_font
from han_grid.braille import apply, verify
from han_grid.cli import make_parser, validate_build_arguments
from han_grid.io import font_metadata
from han_grid.naming import filename
from han_grid.trimming import trim_file, trim_fonts
from han_grid.validation import validate_identity
from han_grid.windows import extract, load_whitelist, is_cjk, FOLDERS


class WindowsTrimTests(unittest.TestCase):
    def test_cjk_scope_includes_supplementary_scripts_and_excludes_non_cjk(self):
        for cp in (0x2F00, 0x3000, 0x31F0, 0x1100, 0x3100, 0x20000, 0x323B0, 0xFF21):
            self.assertTrue(is_cjk(cp), hex(cp))
        for cp in (ord('A'), 0x301, 0x2801, 0xA000, 0x1F600, 0xE0100):
            self.assertFalse(is_cjk(cp), hex(cp))

    def test_extract_every_collection_member_and_hk_alias(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for region, folder in FOLDERS.items():
                directory = root / 'sources' / folder
                directory.mkdir(parents=True)
                first, second = directory/'one.ttf', directory/'two.ttf'
                make_font(first)
                make_font(second)
                with TTFont(second) as font:
                    for table in font['cmap'].tables:
                        if table.isUnicode():
                            table.cmap[0x20001] = font.getBestCmap()[ord('中')]
                    # Create supplementary cmap with FontBuilder's cmap setup.
                    from fontTools.fontBuilder import FontBuilder
                    mapping = dict(font.getBestCmap())
                    mapping[0x20001] = mapping[ord('中')]
                    FontBuilder(font=font).setupCharacterMap(mapping)
                    font.save(second)
                collection = TTCollection()
                collection.fonts = [TTFont(first), TTFont(second)]
                collection.save(directory/'faces.ttc')
                for font in collection.fonts: font.close()
                first.unlink(); second.unlink()
            report = extract(root/'sources', root/'catalog')
            self.assertEqual(len(report['members']), 8)
            points, _ = load_whitelist(root/'catalog', ['hk'])
            self.assertIn(0x20001, points)
            self.assertNotIn(ord('A'), points)
            tw, _ = load_whitelist(root/'catalog', ['tw'])
            self.assertEqual(points, tw)
            catalog = root/'catalog'/'catalog.json'
            damaged = json.loads(catalog.read_text())
            damaged['regions']['tw']['codepoints'].append(0x20001)
            catalog.write_text(json.dumps(damaged))
            with self.assertRaises(ValueError): load_whitelist(root/'catalog', ['tw'])

    def test_prune_only_cjk_and_keep_independent_uvs(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)/'input.ttf'; result=Path(temp)/'trim.ttf'
            make_font(path, coverage='pan-cjk')
            with TTFont(path) as font:
                table = CmapSubtable.newSubtable(14)
                table.platformID, table.platEncID, table.language = 0, 5, 0
                table.cmap = {}
                table.uvsDict = {0xE0100: [(0x4E01, font.getBestCmap()[ord('中')])]}
                font['cmap'].tables.append(table)
                font.save(path)
            allowed = {0x4E01}
            trim_file(path, result, allowed)
            from build_mono import variation_sequences
            with TTFont(path) as original, TTFont(result) as trimmed:
                before, after = set(original.getBestCmap()), set(trimmed.getBestCmap())
                self.assertEqual(after, {cp for cp in before if not is_cjk(cp)})
                self.assertEqual(set(variation_sequences(trimmed)), {(0x4E01,0xE0100)})
                self.assertIn(ord('A'), after)
                self.assertIn(0x301, after)
                self.assertNotIn(ord('中'), after)

    def test_trim_preserves_nonpreferred_unicode_subtable(self):
        with TemporaryDirectory() as directory:
            original = Path(directory) / "input.ttf"
            trimmed = Path(directory) / "trim.ttf"
            make_font(original)
            with TTFont(original) as font:
                extra = CmapSubtable.newSubtable(4)
                extra.platformID, extra.platEncID, extra.language = 0, 1, 0
                extra.cmap = {0x2603: font.getBestCmap()[ord("A")]}
                font["cmap"].tables.append(extra)
                font.save(original)
            trim_file(original, trimmed, {0x3000, ord("中")})
            with TTFont(trimmed) as font:
                self.assertNotIn(0x2603, font.getBestCmap())
                self.assertEqual(font["cmap"].getcmap(0, 1).cmap[0x2603], font.getBestCmap()[ord("A")])

    def test_gapless_pattern_fills_default_line_advance(self):
        from fontTools.pens.boundsPen import BoundsPen

        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ttf"
            make_font(path)
            apply(path, "gapless")
            with TTFont(path) as font:
                glyphs = font.getGlyphSet()
                bounds = BoundsPen(glyphs)
                glyphs[font.getBestCmap()[0x28FF]].draw(bounds)
                left, bottom, right, top = bounds.bounds
                hhea = font["hhea"]
                self.assertEqual(right - left, 500)
                self.assertEqual(top - bottom, hhea.ascent - hhea.descent + hhea.lineGap)

    def test_circle_rectangle_gapless_have_distinct_complete_patterns(self):
        with TemporaryDirectory() as temp:
            for style in ('circle','rectangle','gapless'):
                path = Path(temp)/f'{style}.ttf'
                make_font(path)
                apply(path, style)
                with TTFont(path) as font:
                    verify(font, style)
                    self.assertEqual(font['hmtx'][font.getBestCmap()[0x2801]][0], 500)

    def test_nerd_internal_name_is_short_nf(self):
        from han_grid.nerd import name_nerd_font
        with TemporaryDirectory() as temp:
            source, result = Path(temp)/'base.ttf', Path(temp)/'patched.ttf'
            make_font(source, family='Example Mono')
            make_font(result, family='Patcher Temporary Name')
            name_nerd_font(source, result)
            with TTFont(result) as font:
                self.assertEqual(font['name'].getDebugName(16),'Example Mono NF')
                for name_id in (1,3,4,6,16):
                    value=font['name'].getDebugName(name_id)
                    self.assertNotIn('Subset',value)
                    self.assertNotIn('WinTrim',value)
                    self.assertNotIn('Nerd Font',value)

    def test_trimmed_full_identity_and_already_nerd_rejection(self):
        from han_grid.io import stamp_font
        with TemporaryDirectory() as temp:
            path=Path(temp)/'renamed.ttf'
            identity=make_font(path,coverage='pan-cjk')
            with self.assertRaisesRegex(ValueError,'Untrimmed Pan-CJK'):
                validate_identity(path)
            identity['windows_trim']={'sha256':'catalog-test'}
            stamp_font(path,identity)
            self.assertEqual(validate_identity(path)['coverage'],'pan-cjk')
            identity['nerd']=True
            stamp_font(path,identity)
            with self.assertRaisesRegex(ValueError,'already contains Nerd'):
                validate_identity(path,require_subset=False)

    def test_new_filename_order_and_full_guard(self):
        identity = {'family_base':'Example Mono','weight':'Bold','italic':True,
                    'languages':['sc','tw'],'coverage':'subset','windows_trim':{'sha256':'abc'},'nerd':True}
        self.assertEqual(filename(identity,'otf'),'ExampleMono-BoldItalic-SC+TW-WinTrim-NF.otf')
        long_identity = {**identity, 'family_base': '绘图字体' * 50}
        self.assertLess(len(filename(long_identity, 'otf').encode('utf-8')), 255)
        parser=make_parser()
        with self.assertRaisesRegex(ValueError,'prohibited'):
            validate_build_arguments(parser.parse_args(['build','--full','--nerd']))
        validate_build_arguments(parser.parse_args(['build','--full','--trim-windows','--nerd','--collection']))
        self.assertEqual(parser.parse_args(['build']).braille,'circle')


if __name__ == '__main__': unittest.main()
