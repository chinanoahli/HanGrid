from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTCollection, TTFont
from fontTools.ttLib.tables.O_S_2f_2 import Panose

from build_mono import ROOT, postscript_name, read_sans, set_names, variation_sequences
from han_grid.cli import make_parser, validate_build_arguments
from han_grid.collections import collect_fonts
from han_grid.io import (
    SCHEMA, font_metadata, font_record, new_manifest, read_inputs,
    stamp_font, workspace_lock, write_json,
)
from han_grid.nerd import capacity_check, find_patcher
from han_grid.outlines import cff1_for_fontforge, cff2_from_fontforge
from han_grid.regions import union_sans
from han_grid.sources import latest_tag, load_sources
from han_grid.validation import (
    coverage_digest, default_region, validate_identity,
)


def make_font(path, family="Example SC Subset", weight="Regular", coverage="subset",
              italic=False, nerd=False):
    characters = (
        set(range(0x20, 0x7F)) | set(range(0xFF01, 0xFF5F))
        | set(range(0xFF66, 0xFF9E)) | {0x3000, 0x301, ord("中")}
    )
    mapping = {cp: f"uni{cp:04X}" for cp in characters}
    order = [".notdef", *mapping.values()]
    glyphs, metrics, vertical = {}, {}, {}
    for glyph in order:
        pen = TTGlyphPen(None)
        pen.moveTo((50, 0))
        pen.lineTo((300, 0))
        pen.lineTo((300, 700))
        pen.lineTo((50, 700))
        pen.closePath()
        glyphs[glyph] = pen.glyph()
        cp = next((code for code, name in mapping.items() if name == glyph), 0)
        width = 500 if cp < 0x7F or 0xFF66 <= cp <= 0xFF9D else 1000
        if cp == 0x301:
            width = 0
        metrics[glyph], vertical[glyph] = (width, 50), (1000, 180)

    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(mapping)
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=880, descent=-120)
    builder.setupVerticalMetrics(vertical)
    builder.setupVerticalHeader(ascent=500, descent=-500)
    panose = Panose()
    panose.bFamilyType, panose.bProportion = 2, 9
    builder.setupOS2(
        sTypoAscender=880, sTypoDescender=-120, usWinAscent=880,
        usWinDescent=120, panose=panose, usWeightClass=700 if weight == "Bold" else 400,
    )
    style = weight + (" Italic" if italic else "")
    builder.setupNameTable({
        "familyName": family, "styleName": style,
        "typographicFamily": family, "typographicSubfamily": style,
        "fullName": f"{family} {style}",
        "psName": f"ExampleSCSubset-{style.replace(' ', '')}",
        "uniqueFontIdentifier": f"ExampleSCSubset-{style}",
        "version": "Version 1.102",
    })
    builder.setupPost(isFixedPitch=1, italicAngle=-12 if italic else 0)
    builder.setupMaxp()
    builder.font.save(path)
    with TTFont(path) as font:
        identity = {
            "schema": SCHEMA, "producer": "han-grid", "coverage": coverage,
            "languages": ["sc"], "default_region": "sc", "family_base": "Example",
            "family_name": family, "nerd": nerd, "weight": weight, "italic": italic,
            "group": "example-SC-default-SC", "version": "1.102",
            "coverage_digest": coverage_digest(font),
        }
    stamp_font(path, identity)
    return identity


class CliTests(unittest.TestCase):
    def test_defaults_use_sc_and_otf_and_optional_steps_are_off(self):
        args = make_parser().parse_args(["build"])
        self.assertIsNone(args.languages)
        self.assertEqual(args.format, "otf")
        self.assertFalse(args.nerd)
        self.assertFalse(args.collection)
        self.assertEqual(len(args.weights), 7)
        self.assertEqual(args.styles, "both")

    def test_union_defaults_and_custom_family(self):
        args = make_parser().parse_args([
            "build", "--languages", "jp", "sc", "--format", "both",
            "--family-name", "绘图 Mono", "--nerd", "--collection",
        ])
        self.assertEqual(default_region(args.languages), "sc")
        self.assertEqual(default_region(["jp", "tw"]), "jp")
        self.assertEqual(default_region(["jp", "sc"], "jp"), "jp")
        self.assertEqual(args.family_name, "绘图 Mono")
        self.assertTrue(args.nerd and args.collection)
        with self.assertRaisesRegex(ValueError, "belong"):
            default_region(["sc"], "kr")

    def test_full_nerd_is_rejected_before_building(self):
        args = make_parser().parse_args(["build", "--full", "--nerd"])
        with self.assertRaisesRegex(ValueError, "prohibited"):
            validate_build_arguments(args)

    def test_latest_release_prefers_release_over_source_tag(self):
        tags = "a\trefs/tags/2.005\nb\trefs/tags/2.005R\nc\trefs/tags/2.006-beta\nd\trefs/tags/2.006\n"
        with patch("han_grid.sources.subprocess.check_output", return_value=tags):
            self.assertEqual(latest_tag("https://example.invalid/repo", "sans"), "2.005R")

    def test_custom_postscript_names_survive_normalization_without_collisions(self):
        first = postscript_name("A B SC Subset", "Regular", custom=True)
        second = postscript_name("AB SC Subset", "Regular", custom=True)
        self.assertNotEqual(first, second)
        unicode_name = postscript_name("绘图字体 " * 30, "ExtraLight Italic", custom=True)
        self.assertTrue(unicode_name.isascii())
        self.assertLessEqual(len(unicode_name), 63)
        self.assertTrue(unicode_name.endswith("-ExtraLightItalic"))


class FontIdentityTests(unittest.TestCase):
    def test_renaming_a_full_font_cannot_enable_nerd(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "FakeSubset.ttf"
            make_font(path, coverage="pan-cjk")
            with self.assertRaisesRegex(ValueError, "Pan-CJK"):
                validate_identity(path)

    def test_only_subsets_with_consistent_metadata_are_accepted(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "Custom.ttf"
            make_font(path)
            identity = validate_identity(path)
            self.assertEqual(identity["languages"], ["sc"])
            with TTFont(path) as font:
                cmap = font.getBestCmap()
                for table in font["cmap"].tables:
                    if table.isUnicode() and table.format in {4, 12}:
                        table.cmap.pop(ord("A"), None)
                font.save(path)
            with self.assertRaisesRegex(ValueError, "coverage"):
                validate_identity(path)

    def test_false_fixed_pitch_flag_does_not_pass(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "Example.ttf"
            make_font(path)
            with TTFont(path) as font:
                font["post"].isFixedPitch = 0
                font.save(path)
            with self.assertRaisesRegex(ValueError, "fixed-pitch"):
                validate_identity(path)

    def test_manifest_detects_modified_font(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "Example.ttf"
            identity = make_font(path)
            report = new_manifest("build")
            report["fonts"] = [font_record(path, root, identity)]
            manifest = root / "build-manifest.json"
            write_json(manifest, report)
            self.assertEqual(read_inputs([manifest]), [path.resolve()])
            with path.open("ab") as stream:
                stream.write(b"changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                read_inputs([manifest])

    def test_glyph_capacity_uses_glyph_count_not_codepoint_count(self):
        font = {"maxp": type("Maxp", (), {"numGlyphs": 60000})()}
        probe = {"copy_operations": 10000, "control_glyph_reserve": 2}
        with self.assertRaisesRegex(ValueError, "70002"):
            capacity_check(font, probe)
        font["maxp"].numGlyphs = 41068
        self.assertEqual(capacity_check(font, probe)["upper_bound"], 51070)

    def test_nerd_capacity_preflight_can_be_explicitly_skipped(self):
        font = {"maxp": type("Maxp", (), {"numGlyphs": 55005})()}
        probe = {"copy_operations": 10880, "control_glyph_reserve": 2}
        with self.assertRaisesRegex(ValueError, "65887"):
            capacity_check(font, probe)
        result = capacity_check(font, probe, skip_glyph_check=True)
        self.assertEqual(result["upper_bound"], 65887)
        self.assertTrue(result["preflight_skipped"])
        for command in (["build", "--nerd"], ["nerd", "input.otf"],
                        ["trim", "input.otf", "--nerd"]):
            with self.subTest(command=command):
                parser = make_parser()
                self.assertFalse(parser.parse_args(command).skip_nerd_glyph_check)
                self.assertTrue(parser.parse_args(
                    command + ["--skip-nerd-glyph-check"]
                ).skip_nerd_glyph_check)

    def test_two_styles_round_trip_through_a_true_type_collection(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / "Example-Regular.ttf", root / "Example-Bold.ttf"]
            make_font(paths[0], weight="Regular")
            make_font(paths[1], weight="Bold")
            result = collect_fonts(paths, root / "collections")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["format"], "ttc")
            with TTCollection(result[0]["path"]) as collection:
                self.assertEqual(len(collection.fonts), 2)
                self.assertEqual({f["OS/2"].usWeightClass for f in collection.fonts}, {400, 700})
                self.assertTrue(all(font_metadata(f)["coverage"] == "subset" for f in collection.fonts))

    def test_collection_warns_about_mixed_profiles_and_uses_relative_paths(self):
        from contextlib import redirect_stderr
        from io import StringIO

        with TemporaryDirectory() as directory:
            root = Path(directory)
            regular, bold = root / "Regular.ttf", root / "Bold.ttf"
            regular_id = make_font(regular)
            bold_id = make_font(bold, weight="Bold")
            stamp_font(regular, {**regular_id, "braille": "circle"})
            stamp_font(bold, {**bold_id, "version": "1.103", "braille": "rectangle"})
            with redirect_stderr(StringIO()) as output:
                records = collect_fonts([regular, bold], root / "collections", manifest_root=root)
            self.assertIn("mixed version", output.getvalue())
            self.assertEqual({issue["field"] for issue in records[0]["warnings"]}, {"version", "braille"})
            self.assertFalse(Path(records[0]["path"]).is_absolute())
            with TTCollection(root / records[0]["path"]) as collection:
                self.assertEqual(len(collection.fonts), 2)

    def test_mismatched_filename_extension_is_rejected(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "Example.otf"
            make_font(path)
            with self.assertRaisesRegex(ValueError, "extension"):
                validate_identity(path)

    def test_nerd_collection_requires_the_repaired_fixed_pitch_flag(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "Example.ttf"
            make_font(path, family="Example Nerd Font", nerd=True)
            with TTFont(path) as font:
                font["post"].isFixedPitch = 0
                font.save(path)
            with self.assertRaisesRegex(ValueError, "fixed-pitch"):
                collect_fonts([path], Path(temporary) / "collections")

    def test_collection_rejects_duplicate_internal_styles(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / "a.ttf", root / "b.ttf"]
            make_font(paths[0])
            make_font(paths[1])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                collect_fonts(paths, root / "collections")


class OutlineCompatibilityTests(unittest.TestCase):
    def test_unicode_family_and_cubic_outlines_survive_the_fontforge_boundary(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            ttf, source = root / "base.ttf", root / "source.otf"
            cff1, restored = root / "compat.otf", root / "restored.otf"
            make_font(ttf, family="绘图 Mono JP Subset")
            with TTFont(ttf) as font:
                glyph_set = font.getGlyphSet()
                strings = {}
                for name in font.getGlyphOrder():
                    pen = T2CharStringPen(None, glyph_set)
                    glyph_set[name].draw(pen)
                    strings[name] = pen.getCharString()
                del font["glyf"]
                del font["loca"]
                font.sfntVersion = "OTTO"
                builder = FontBuilder(font=font)
                builder.setupCFF2(strings)
                builder.setupMaxp()
                font["post"].formatType = 3.0
                font.save(source)
            cff1_for_fontforge(source, cff1)
            cff2_from_fontforge(cff1, restored)
            with TTFont(source) as original, TTFont(cff1) as compatible, TTFont(restored) as result:
                self.assertEqual(compatible["name"].getDebugName(16), "绘图 Mono JP Subset")
                self.assertEqual(result["name"].getDebugName(16), "绘图 Mono JP Subset")
                self.assertEqual(original.getBestCmap().keys(), compatible.getBestCmap().keys())
                self.assertEqual(original.getBestCmap().keys(), result.getBestCmap().keys())
                for cp in original.getBestCmap():
                    pens = [RecordingPen(), RecordingPen(), RecordingPen()]
                    for font, pen in zip((original, compatible, result), pens):
                        glyph = font.getBestCmap()[cp]
                        font.getGlyphSet()[glyph].draw(pen)
                    self.assertEqual(pens[0].value, pens[1].value, cp)
                    self.assertEqual(pens[0].value, pens[2].value, cp)
                expected = [original["hmtx"][g][0] for g in original.getGlyphOrder()]
                self.assertEqual(expected, [result["hmtx"][g][0] for g in result.getGlyphOrder()])


class PatcherTests(unittest.TestCase):
    def test_missing_patcher_has_manual_download_instruction(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Download.*yourself"):
                find_patcher(Path(temporary))

    def test_zip_traversal_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "FontPatcher.zip"
            with ZipFile(archive, "w") as zipped:
                zipped.writestr("../outside.txt", "outside")
            with self.assertRaisesRegex(ValueError, "outside"):
                find_patcher(root, archive)
            self.assertFalse((root / "outside.txt").exists())

    def test_valid_zip_is_cached_and_invalidates_on_change(self):
        required = (
            "font-patcher", "glyphnames.json", "src/glyphs/original-source.otf",
            "bin/scripts/name_parser/FontnameParser.py", "bin/scripts/braille/Braille.py",
        )
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "FontPatcher.zip"
            with ZipFile(archive, "w") as zipped:
                for name in required:
                    zipped.writestr(name, "first")
            first = find_patcher(root, archive)
            self.assertEqual(find_patcher(root, archive), first)
            with ZipFile(archive, "a") as zipped:
                zipped.writestr("version.txt", "second")
            self.assertNotEqual(find_patcher(root, archive), first)

    def test_workspace_lock_rejects_a_concurrent_operation(self):
        with TemporaryDirectory() as temporary:
            with workspace_lock(temporary):
                with self.assertRaisesRegex(ValueError, "Another Han Grid"):
                    with workspace_lock(temporary):
                        pass


class RegionalUnionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "Src/SourceHanSans/.git").exists():
            raise unittest.SkipTest("Initialize source submodules for regional union regression tests")
        cls.lock = load_sources(ROOT)

    def test_internal_names_exclude_region_and_subset_labels(self):
        for base in (None, "Example Mono", "绘图 Mono"):
            with self.subTest(base=base), read_sans(
                ROOT / "Src/SourceHanSans", self.lock["sans"]["commit"], "SC", "Regular", True,
            ) as font:
                family = base or "Han Grid Mono"
                ps_name = set_names(font, font, "SC+TW", "Regular", False, "1.102", True, base)
                self.assertEqual(font["name"].getDebugName(1), family)
                self.assertEqual(font["name"].getDebugName(16), family)
                self.assertEqual(font["name"].getDebugName(4), family + " Regular")
                self.assertEqual(font["name"].getDebugName(6), ps_name)
                self.assertNotIn("Subset", ps_name)
                self.assertNotIn("SCTW", ps_name)
                top = font["CFF "].cff.topDictIndex[0]
                self.assertEqual(top.FamilyName, family)
                self.assertEqual(top.FullName, family + " Regular")

    def test_union_keeps_each_regions_coverage_and_selected_localized_glyphs(self):
        ref = self.lock["sans"]["commit"]
        originals = {}
        for region in ("SC", "JP"):
            with read_sans(ROOT / "Src/SourceHanSans", ref, region, "Regular", True) as font:
                originals[region] = (
                    dict(font.getBestCmap()), set(font.getGlyphOrder()),
                    set(variation_sequences(font)),
                )
        union = union_sans(ROOT / "Src/SourceHanSans", ref, ["SC", "JP"], "SC", "Regular")
        try:
            sc, jp = originals["SC"][0], originals["JP"][0]
            cmap = union.getBestCmap()
            self.assertEqual(cmap.keys(), sc.keys() | jp.keys())
            original_glyphs = originals["SC"][1] | originals["JP"][1]
            # A regional Unicode alias can reuse a spare CID but must use
            # the same underlying outline as one of the selected glyphs.
            strings = union["CFF "].cff.topDictIndex[0].CharStrings.charStrings
            self.assertEqual(strings[cmap[ord("骨")]], strings[sc[ord("骨")]])
            original_indices = {strings[glyph] for glyph in original_glyphs}
            self.assertTrue(all(strings[glyph] in original_indices for glyph in union.getGlyphOrder()))
            self.assertEqual(set(variation_sequences(union)), originals["SC"][2] | originals["JP"][2])
            for tag in ("GSUB", "GPOS"):
                languages = {
                    record.LangSysTag.strip()
                    for script in union[tag].table.ScriptList.ScriptRecord
                    for record in script.Script.LangSysRecord
                }
                self.assertTrue(languages <= {"JAN", "ZHS"})
                if tag == "GSUB":
                    self.assertTrue({"JAN", "ZHS"} <= languages)
        finally:
            union.close()

    def test_changing_default_region_changes_shared_glyph_choice(self):
        ref = self.lock["sans"]["commit"]
        with read_sans(ROOT / "Src/SourceHanSans", ref, "JP", "Regular", True) as source:
            expected = source.getBestCmap()[ord("骨")]
        font = union_sans(ROOT / "Src/SourceHanSans", ref, ["SC", "JP"], "JP", "Regular")
        try:
            strings = font["CFF "].cff.topDictIndex[0].CharStrings.charStrings
            self.assertEqual(strings[font.getBestCmap()[ord("骨")]], strings[expected])
        finally:
            font.close()


if __name__ == "__main__":
    unittest.main()
