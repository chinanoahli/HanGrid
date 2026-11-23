"""Build pinned source faces, retain requested formats, and emit integrity-checked manifests."""

from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fontTools.ttLib import TTFont

from build_mono import build_face, verify_terminal_grid
from build_ttf import convert
from . import __version__ as tool_version
from .geometry import audit_geometry
from .io import SCHEMA, check_destination, font_record, new_manifest, sha256, stamp_font, write_json
from .naming import filename as output_filename
from .regions import PUBLIC_REGIONS
from .sources import copy_licenses, load_sources
from .validation import coverage_digest, default_region, group_name, normalize_regions

FONT_VERSION = "1.102"


def face_task(workspace, lock, output, languages, preferred, full, weight, italic,
              formats, family_base, version, compare, trim_windows=False, catalog=None, braille="circle"):
    workspace, output = Path(workspace), Path(output)
    coverage = "pan-cjk" if full else "subset"
    group = group_name(languages, preferred, coverage, family_base)
    if trim_windows:
        group += "-WinTrim"
    group_directory = output / group
    check_destination(group_directory, output)
    group_directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".face-", dir=group_directory) as temporary:
        temporary = Path(temporary)
        allowed = None
        if trim_windows:
            from .windows import load_whitelist
            allowed, catalog_info = load_whitelist(catalog, languages, full)
        filename, summary = build_face(
            workspace / lock["sans"]["repository"], lock["sans"]["commit"],
            workspace / lock["mono"]["repository"], temporary, PUBLIC_REGIONS[preferred],
            weight, italic, version, "cff2", compare, not full,
            None if full else [PUBLIC_REGIONS[region] for region in languages],
            lock["mono"]["commit"], family_base, allowed,
        )
        source_summary = summary
        otf = temporary / filename
        formats = ("otf", "ttf") if formats == "both" else (formats,)
        metadata = {
            "schema": SCHEMA, "producer": "han-grid", "tool_version": tool_version,
            "coverage": coverage,
            "languages": languages, "default_region": preferred,
            "group": group, "family_base": family_base, "version": version,
            "nerd": False, "weight": weight, "italic": italic, "sources": lock,
            "source_summary": {key: value for key, value in summary.items() if not isinstance(value, (list, dict))},
        }
        if trim_windows:
            from .trimming import trim_file
            source_sha = sha256(otf)
            summary = trim_file(otf, otf, allowed)
            summary["removed_codepoints"] += source_summary["capacity_trimmed_codepoints"]
            metadata["windows_trim"] = {**catalog_info, **summary, "source_sha256": source_sha}
        from .braille import apply
        apply(otf, braille)
        metadata["braille"] = braille
        identity_config = {key: metadata[key] for key in (
            "coverage", "languages", "default_region", "family_base", "version",
            "weight", "italic", "sources", "tool_version"
        )}
        metadata["build_id"] = hashlib.sha256(
            json.dumps({**identity_config, "windows_trim": metadata.get("windows_trim"), "braille": braille}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with TTFont(otf) as font:
            metadata["family_name"] = font["name"].getDebugName(16)
            metadata["coverage_digest"] = coverage_digest(font)
            verify_terminal_grid(font, otf, allow_filtered=trim_windows, check_extended=True)

        # TTF conversion uses a transient OTF when only TTF was requested.
        paths = {"otf": otf}
        if "ttf" in formats:
            ttf = otf.with_suffix(".ttf")
            convert(otf, ttf, allow_filtered=trim_windows)
            paths["ttf"] = ttf

        differences = {key: value for key, value in source_summary.items() if isinstance(value, (list, dict))}
        if differences:
            artifact = group_directory / f"{metadata['build_id']}-source-differences.json"
            write_json(artifact, differences)
            metadata["source_differences"] = {
                "sha256": sha256(artifact),
                "counts": {key: len(value) for key, value in differences.items()},
            }
        prepared = []
        for extension in formats:
            path = paths[extension]
            with TTFont(path) as font:
                identity = {**metadata, "glyph_geometry": audit_geometry(font, strict_quotes=True)}
            stamp_font(path, identity)
            destination = group_directory / "base" / extension / output_filename(metadata, extension)
            check_destination(destination, output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            record = font_record(path, output, identity)
            record["path"] = destination.relative_to(output).as_posix()
            if differences:
                record["source_differences"] = artifact.relative_to(output).as_posix()
            prepared.append((path, destination, record))
        # Finish conversions, metadata and integrity checks for both requested
        # formats before publishing either file.
        records = []
        for path, destination, record in prepared:
            path.replace(destination)
            records.append(record)
    return records


def build_fonts(args, workspace):
    lock = load_sources(workspace)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    languages = normalize_regions(args.languages or ["sc"])
    if args.full:
        if args.languages is not None or args.default_region:
            raise ValueError("--full uses --regions; --languages and --default-region select subset builds")
        regions = normalize_regions(args.regions or ["sc", "tw", "hk", "jp", "kr"])
        profiles = [([region], region) for region in regions]
    else:
        if args.regions:
            raise ValueError("--regions selects full-font default variants; use --languages for subset unions")
        profiles = [(languages, default_region(languages, args.default_region))]
    styles = {"both": (False, True), "upright": (False,), "italic": (True,)}[args.styles]
    weights = list(dict.fromkeys(args.weights))
    comparison_weight = "Regular" if "Regular" in weights else weights[0]
    report = new_manifest("build", lock)
    report["configuration"] = {
        "full": args.full, "profiles": profiles, "format": args.format,
        "weights": weights, "styles": args.styles, "family_name": args.family_name,
        "version": args.version, "trim_windows": args.trim_windows, "braille": args.braille,
    }
    manifest_path = output / "build-manifest.json"
    # Snapshot this operation before publishing any new output, so an older
    # manifest cannot silently describe a partly rebuilt directory.
    write_json(manifest_path, report)
    tasks = [
        (workspace, lock, output, regions, preferred, args.full, weight, italic,
         args.format, args.family_name, args.version, weight == comparison_weight and italic == styles[0],
         args.trim_windows, args.windows_catalog, args.braille)
        for regions, preferred in profiles for weight in weights for italic in styles
    ]
    failures = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(face_task, *task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            try:
                report["fonts"].extend(future.result())
            except Exception as error:
                report["failures"].append({
                    "languages": task[3], "default_region": task[4],
                    "weight": task[6], "italic": task[7], "error": str(error),
                })
                failures.append(error)
            report["fonts"].sort(key=lambda entry: entry["path"])
            write_json(manifest_path, report)
    copy_licenses(workspace, lock, output)
    if failures:
        raise ValueError(f"{len(failures)} face(s) failed. See {manifest_path}; completed fonts were retained")
    print(f"Built {len(report['fonts'])} standalone fonts. Manifest: {manifest_path}", flush=True)
    return report, manifest_path
