"""CJK-only pruning with layout closure, UVS retention and auditable provenance."""
import copy
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.pens.recordingPen import DecomposingRecordingPen

from build_mono import variation_sequences
from .geometry import audit_geometry
from .io import temporary_output, stamp_font, sha256, font_record, new_manifest, write_json
from .naming import filename
from .validation import coverage_digest, check_base_font, validate_identity, variation_advances
from .windows import ALIASES, is_cjk, load_whitelist, unicode_coverage


def trim_file(source, destination, allowed):
    with TTFont(source, recalcTimestamp=False) as font:
        before = unicode_coverage(font)
        before_tables = {
            (table.platformID, table.platEncID, table.language, table.format): table.cmap.copy()
            for table in font["cmap"].tables if table.isUnicode() and table.format != 14
        }
        keep = {cp for cp in before if not is_cjk(cp) or cp in allowed}
        sequences = {pair: glyph for pair, glyph in variation_sequences(font).items()
                     if not is_cjk(pair[0]) or pair[0] in allowed}
        sequence_widths = {pair: width for pair, width in variation_advances(font).items() if pair in sequences}
        metrics = {cp: font['hmtx'][glyph][0] for cp, glyph in font.getBestCmap().items() if cp in keep}
        glyphs = font.getGlyphSet()
        non_cjk_outlines = {}
        for cp in keep:
            if not is_cjk(cp) and cp in font.getBestCmap():
                pen = DecomposingRecordingPen(glyphs)
                glyphs[font.getBestCmap()[cp]].draw(pen)
                non_cjk_outlines[cp] = pen.value
        count = font['maxp'].numGlyphs
        # Remove rejected UVS before closure so they cannot pull unwanted glyphs in.
        for table in font['cmap'].tables:
            if table.format == 14:
                table.uvsDict = {selector: [(cp, glyph) for cp, glyph in entries if (cp, selector) in sequences]
                                 for selector, entries in table.uvsDict.items()}
        options = subset.Options(layout_features=['*'], layout_closure=True,
            name_IDs=['*'], name_languages=['*'], name_legacy=True,
            notdef_outline=True, recalc_average_width=False)
        options.legacy_cmap = True
        options.drop_tables += ['DSIG']
        selection = subset.Subsetter(options=options)
        selection.populate(unicodes=keep | {selector for _, selector in sequences},
                           glyphs=[glyph for glyph in sequences.values() if glyph is not None])
        selection.subset(font)
        # Explicit UVS glyph retention may retain ordinary cmap aliases of the
        # same glyph. Remove those aliases without removing the variation glyph.
        for table in font['cmap'].tables:
            if table.isUnicode() and table.format != 14:
                table.cmap = {cp: glyph for cp, glyph in table.cmap.items() if cp in keep}
        for table in font['cmap'].tables:
            key = table.platformID, table.platEncID, table.language, table.format
            if key in before_tables and table.format != 14:
                expected = {cp: glyph for cp, glyph in before_tables[key].items() if cp in keep}
                if table.cmap != expected:
                    raise ValueError(f'CJK trim changed retained mappings in cmap {key}')
        font['OS/2'].recalcUnicodeRanges(font)
        if 'CFF2' in font and {'vhea', 'vmtx'} <= set(font.keys()):
            from build_mono import recalc_geometry
            recalc_geometry(font)
        with temporary_output(destination) as temporary:
            font.save(temporary)
            with TTFont(temporary) as result:
                if unicode_coverage(result) != keep:
                    raise ValueError(f'CJK trim changed coverage: lost {sorted(keep - unicode_coverage(result))[:8]}, added {sorted(unicode_coverage(result) - keep)[:8]}')
                if set(variation_sequences(result)) != set(sequences):
                    raise ValueError('CJK trim changed retained variation sequences')
                if variation_advances(result) != sequence_widths:
                    raise ValueError('CJK trim changed variation advances')
                if any(result['hmtx'][result.getBestCmap()[cp]][0] != width for cp, width in metrics.items()):
                    raise ValueError('CJK trim changed character advances')
                glyphs = result.getGlyphSet()
                for cp, expected in non_cjk_outlines.items():
                    pen = DecomposingRecordingPen(glyphs)
                    glyphs[result.getBestCmap()[cp]].draw(pen)
                    if pen.value != expected:
                        raise ValueError(f'CJK trim changed non-CJK outline U+{cp:04X}')
                check_base_font(result, temporary, allow_filtered=True)
                return {'before_glyphs': count, 'after_glyphs': result['maxp'].numGlyphs,
                        'removed_codepoints': len(before - keep), 'retained_codepoints': len(keep)}


def trim_identity(identity, catalog_info, summary, source_sha):
    identity = copy.deepcopy(identity)
    identity['windows_trim'] = {**catalog_info, **summary, 'source_sha256': source_sha}
    if not identity['group'].endswith('-WinTrim'):
        identity['group'] += '-WinTrim'
    return identity


def trim_fonts(paths, output, catalog):
    report = new_manifest('trim-windows')
    manifest = Path(output) / 'trim-manifest.json'
    planned = []
    whitelists = {}
    sources = [Path(path).resolve() for path in paths]
    for source in sources:
        identity = validate_identity(source, require_subset=False)
        full = identity['coverage'] == 'pan-cjk'
        key = () if full else tuple(sorted({ALIASES.get(region, region) for region in identity['languages']}))
        if key not in whitelists:
            whitelists[key] = load_whitelist(catalog, identity['languages'], full)
        allowed, info = whitelists[key]
        source_sha = sha256(source)
        provisional = trim_identity(identity, info, {}, source_sha)
        extension = source.suffix.lower()
        destination = Path(output) / provisional['group'] / 'base' / extension[1:] / filename(provisional, extension)
        from .io import check_destination, same_output
        check_destination(destination, output)
        if any(same_output(destination, original) for original in sources):
            raise ValueError('Trim requires a separate output path; input will not be overwritten')
        if any(same_output(destination, item[1]) for item in planned):
            raise ValueError('Duplicate trim destination; select one font per style and format')
        planned.append((source, destination, identity, allowed, info, source_sha))
    write_json(manifest, report)
    for source, destination, identity, allowed, info, source_sha in planned:
        # Complete subset, metadata, validation before publishing the destination.
        try:
            with temporary_output(destination) as temporary:
                summary = trim_file(source, temporary, allowed)
                identity = trim_identity(identity, info, summary, source_sha)
                with TTFont(temporary) as font:
                    identity['coverage_digest'] = coverage_digest(font)
                    identity['glyph_geometry'] = audit_geometry(
                        font, strict_quotes=identity.get('glyph_geometry', {}).get('strict_quotes', False)
                    )
                stamp_font(temporary, identity)
            report['fonts'].append(font_record(destination, output, identity))
            print(f'Trimmed {source.name}: {summary["before_glyphs"]} -> {summary["after_glyphs"]} glyphs', flush=True)
        except (ValueError, OSError) as error:
            report['failures'].append({'input': str(source), 'error': str(error)})
        write_json(manifest, report)
    # Retain upstream license text even when inputs were copied from their original directory.
    import shutil
    for name in ('OFL-SourceHanSans.txt', 'OFL-SourceHanMono.txt'):
        target = Path(output) / name
        if not target.exists():
            shutil.copyfile(Path(__file__).parent / 'licenses' / name, target)
    return report, manifest
