"""Group and package standalone subset fonts into verified TTC/OTC collections."""

from collections import defaultdict
import hashlib
import shutil
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont

from .io import check_destination, same_output, sha256, temporary_output
from .validation import semantic_signature, validate_identity


def normalized_table(tag, data):
    if tag == "head":
        # Font collection members have no individual checkSumAdjustment.
        return data[:8] + data[12:]
    return data


def table_digest(font):
    digest = hashlib.sha256()
    for tag in sorted(font.reader.keys()):
        digest.update(tag.encode("ascii"))
        digest.update(normalized_table(tag, font.reader[tag]))
    return digest.hexdigest()


def collect_fonts(paths, output, manifest_root=None):
    if manifest_root is not None:
        manifest_root = Path(manifest_root).resolve()
        check_destination(output, manifest_root)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = list(map(Path, paths))
    with TemporaryDirectory(prefix=".collection-", dir=output) as directory:
        staging = Path(directory)
        records = stage_collections(paths, staging)
        for record in records:
            source = Path(record["path"])
            destination = output / source.relative_to(staging)
            check_destination(destination, output)
            if any(same_output(destination, path) for path in paths):
                raise ValueError(f"Collection output overlaps an input: {destination}")
        if manifest_root is not None:
            copy_collection_licenses(paths, Path(manifest_root))
        for record in records:
            source = Path(record["path"])
            destination = output / source.relative_to(staging)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            record["path"] = (destination.relative_to(manifest_root).as_posix()
                              if manifest_root is not None else str(destination))
            print(f"Packed {len(record['members'])} fonts into {destination}", flush=True)
        return records


def copy_collection_licenses(paths, root):
    for name in ("OFL-SourceHanSans.txt", "OFL-SourceHanMono.txt", "NerdFont-Licenses"):
        target = root / name
        if target.exists():
            continue
        source = next((parent / name for path in paths for parent in path.resolve().parents
                       if (parent / name).exists()), None)
        if source is None and name != "NerdFont-Licenses":
            source = Path(__file__).parent / "licenses" / name
        if source is not None:
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copyfile(source, target)


def stage_collections(paths, output):
    grouped = defaultdict(list)
    identities = {}
    for path in paths:
        path = Path(path)
        identity = validate_identity(path, require_subset=True, allow_nerd=True)
        key = (identity["group"], identity.get("nerd", False), path.suffix.lower())
        grouped[key].append(path)
        identities[path] = identity
    records = []
    for (group, nerd, extension), members in sorted(grouped.items()):
        from .naming import filename
        identity = {**identities[members[0]], "weight": "Collection", "italic": False}
        destination = Path(output) / group / filename(identity, 'ttc' if extension == '.ttf' else 'otc')
        members = sorted(set(members))
        warnings = []
        for field in ("version", "braille"):
            values = {str(path): identities[path].get(field) for path in members}
            if len(set(values.values())) > 1:
                warnings.append({"field": field, "members": values})
                print(f"Warning: mixed {field} in {destination.name}: {values}", file=sys.stderr)
        fonts = [TTFont(path, lazy=True, recalcTimestamp=False, recalcBBoxes=False) for path in members]
        try:
            names = [font["name"].getDebugName(6) for font in fonts]
            if len(set(names)) != len(names):
                raise ValueError("Collection contains duplicate PostScript names; select one font per style")
            signatures = [semantic_signature(font) for font in fonts]
            digests = [table_digest(font) for font in fonts]
            # Use fresh lazy readers for serialization. Inspections above load
            # cmap/post/layout tables and could normalize their binary encoding.
            for font in fonts:
                font.close()
            fonts = [
                TTFont(path, lazy=True, recalcTimestamp=False, recalcBBoxes=False)
                for path in members
            ]
            collection = TTCollection()
            collection.fonts = fonts
            with temporary_output(destination) as temporary:
                collection.save(temporary, shareTables=True)
                with TTCollection(temporary, lazy=True, checkChecksums=2) as result:
                    if len(result.fonts) != len(fonts):
                        raise ValueError(f"Collection member count changed: {destination}")
                    for index, member in enumerate(result.fonts):
                        if semantic_signature(member) != signatures[index]:
                            raise ValueError(f"Collection changed font metrics or mappings: {names[index]}")
                        if table_digest(member) != digests[index]:
                            with TTFont(members[index], lazy=True) as source:
                                changed = [
                                    tag for tag in source.reader.keys()
                                    if normalized_table(tag, member.reader[tag]) != normalized_table(tag, source.reader[tag])
                                ]
                            raise ValueError(f"Collection changed font tables for {names[index]}: {changed}")
            records.append({
                "path": str(destination.resolve()), "sha256": sha256(destination),
                "group": group, "nerd": nerd, "format": destination.suffix[1:],
                "members": names, "warnings": warnings, "member_sha256": [sha256(path) for path in members],
            })
        finally:
            for font in fonts:
                font.close()
    return records
