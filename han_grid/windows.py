"""Auditable CJK repertoires extracted from local Windows fonts, never distributed fonts."""
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json

from fontTools.ttLib import TTCollection, TTFont
from fontTools.unicodedata import Blocks

from build_mono import variation_sequences
from .io import sha256, write_json, read_json

FOLDERS = {'sc': 'zh-cn', 'tw': 'zh-tw', 'jp': 'ja-jp', 'kr': 'ko-kr'}
ALIASES = {'hk': 'tw'}
# Block data comes from the pinned FontTools distribution (Unicode 17).
BLOCK_NAMES = {
    'Kangxi Radicals', 'Kanbun', 'Yijing Hexagram Symbols', 'Vertical Forms',
    'Small Form Variants', 'Halfwidth and Fullwidth Forms',
}
RANGES = tuple(
    (start, Blocks.RANGES[i + 1] - 1, name)
    for i, (start, name) in enumerate(zip(Blocks.RANGES, Blocks.VALUES))
    if name in BLOCK_NAMES or any(term in name for term in
        ('CJK', 'Hangul', 'Hiragana', 'Katakana', 'Bopomofo', 'Ideographic', 'Kana'))
)


def is_cjk(cp):
    return any(start <= cp <= end for start, end, _ in RANGES)


def digest_points(points):
    return hashlib.sha256('\n'.join(f'{cp:06X}' for cp in sorted(points)).encode('ascii')).hexdigest()


def default_catalog(workspace, for_extraction=False):
    local = Path(workspace) / 'data' / 'windows-cjk'
    if for_extraction or (local / 'catalog.json').is_file():
        return local
    return Path(__file__).parent / 'data' / 'windows-cjk'


def unicode_coverage(font):
    # Inspect every Unicode subtable, including supplementary planes; UVS are separate.
    return {cp for table in font['cmap'].tables if table.isUnicode() and table.format != 14
            for cp in table.cmap}


def extract(source, destination):
    source, destination = Path(source), Path(destination)
    results, records = {}, []
    # Gather and verify every member before publishing any catalog.
    with TemporaryDirectory(prefix='han-grid-windows-') as temporary:
        for region, folder in FOLDERS.items():
            directory = source / folder
            files = sorted(p for p in directory.rglob('*') if p.suffix.lower() in {'.ttf', '.otf', '.ttc', '.otc'})
            if not files:
                raise ValueError(f'No Windows reference fonts for {region}: {directory}')
            points, sequences = set(), set()
            for path in files:
                source_digest = sha256(path)
                collection = TTCollection(path, lazy=True) if path.suffix.lower() in {'.ttc', '.otc'} else None
                fonts = collection.fonts if collection else [TTFont(path)]
                try:
                    for index, font in enumerate(fonts):
                        original = unicode_coverage(font)
                        original_uvs = set(variation_sequences(font))
                        if collection:
                            extension = '.ttf' if 'glyf' in font else '.otf'
                            unpacked = Path(temporary) / f'{region}-{path.stem}-{index}{extension}'
                            font.save(unpacked)
                            with TTFont(unpacked) as member:
                                if unicode_coverage(member) != original or set(variation_sequences(member)) != original_uvs:
                                    raise ValueError(f'Collection extraction changed mappings: {path} member {index}')
                            unpacked.unlink()
                        selected = {cp for cp in original if is_cjk(cp)}
                        selected_uvs = {pair for pair in original_uvs if is_cjk(pair[0])}
                        points.update(selected)
                        sequences.update(selected_uvs)
                        records.append({
                            'region': region, 'file': path.relative_to(source).as_posix(),
                            'sha256': source_digest, 'member': index,
                            'family': font['name'].getDebugName(16) or font['name'].getDebugName(1),
                            'postscript_name': font['name'].getDebugName(6),
                            'unicode_count': len(original), 'cjk_count': len(selected),
                            'cjk_sha256': digest_points(selected), 'variation_count': len(selected_uvs),
                            'variation_sha256': hashlib.sha256(json.dumps(sorted(selected_uvs)).encode('ascii')).hexdigest(),
                        })
                finally:
                    for font in fonts:
                        font.close()
            results[region] = {'codepoints': sorted(points), 'variation_sequences': sorted(sequences),
                               'sha256': digest_points(points)}
    report = {'schema': 1, 'operation': 'windows-cjk-extraction', 'unicode_version': '17.0.0',
              'aliases': ALIASES, 'ranges': RANGES, 'regions': results, 'members': records}
    # Catalog is authoritative and atomic; plain text files are human-readable views.
    destination.mkdir(parents=True, exist_ok=True)
    from .io import temporary_output
    for region, result in results.items():
        with temporary_output(destination / f'{region}.txt') as temporary:
            temporary.write_text(''.join(f'U+{cp:04X}\n' for cp in result['codepoints']), encoding='ascii')
    write_json(destination / 'catalog.json', report)
    print(f'Extracted {len(records)} font members into {destination}', flush=True)
    return report


def load_whitelist(directory, languages=None, full=False):
    path = Path(directory) / 'catalog.json'
    if not path.is_file():
        raise ValueError(f'Windows CJK catalog missing: {path}. Run han-grid extract-windows first')
    catalog = read_json(path)
    if catalog.get('schema') != 1 or catalog.get('operation') != 'windows-cjk-extraction':
        raise ValueError(f'Invalid Windows CJK catalog: {path}')
    if catalog.get('ranges') != [list(item) for item in RANGES]:
        raise ValueError('Windows catalog CJK definition differs from the tool; re-extract references')
    regions = list(FOLDERS) if full else list(dict.fromkeys(ALIASES.get(r, r) for r in languages))
    points = set()
    for region in regions:
        result = catalog['regions'].get(region)
        if not result:
            raise ValueError(f'Windows CJK catalog has no {region} repertoire')
        selected = result['codepoints']
        if selected != sorted(set(selected)) or any(not is_cjk(cp) for cp in selected):
            raise ValueError(f'Invalid or duplicate codepoints in {region} catalog')
        if digest_points(selected) != result['sha256']:
            raise ValueError(f'Windows CJK catalog digest mismatch: {region}')
        points.update(selected)
    return points, {'catalog_sha256': sha256(path), 'regions': regions,
                    'aliases': ALIASES, 'codepoints': len(points), 'sha256': digest_points(points)}
