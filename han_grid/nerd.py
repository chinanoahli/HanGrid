"""Local Nerd Fonts patching, capacity preflight, and fixed-pitch postprocessing."""

import copy
import json
import os
import re
from pathlib import Path
import shutil
import stat
import struct
import subprocess
from tempfile import TemporaryDirectory
import uuid
from zipfile import BadZipFile, ZipFile

from fontTools import subset
from fontTools.pens.boundsPen import BoundsPen
from fontTools.unicodedata import category
from fontTools.ttLib import TTFont, TTLibError

from repair_nerd_mono import repair, validate_grid
from .geometry import size_nerd_icons
from .io import (
    temporary_output, font_record, new_manifest, read_json, sha256, stamp_font, write_json,
)
from .outlines import cff1_for_fontforge, cff2_from_fontforge
from .validation import (
    MAX_GLYPHS, coverage_digest, validate_identity, variation_advances,
)

DOWNLOAD_MESSAGE = (
    "Download the Nerd Fonts FontPatcher yourself, then extract it into "
    "Tools/FontPatcher or place FontPatcher.zip in Tools/"
)


def validate_patcher(directory):
    required = (
        "font-patcher", "glyphnames.json", "src/glyphs/original-source.otf",
        "bin/scripts/name_parser/FontnameParser.py", "bin/scripts/braille/Braille.py",
    )
    missing = [name for name in required if not (directory / name).is_file()]
    if missing:
        raise ValueError(f"Incomplete FontPatcher at {directory}: {', '.join(missing)}. {DOWNLOAD_MESSAGE}")
    return directory


def find_patcher(workspace, override=None):
    workspace = Path(workspace)
    candidate = Path(override).resolve() if override else workspace / "Tools" / "FontPatcher"
    if candidate.is_dir():
        return validate_patcher(candidate)
    archive = candidate if override else workspace / "Tools" / "FontPatcher.zip"
    if not archive.is_file():
        raise ValueError(DOWNLOAD_MESSAGE)
    cache = workspace / ".han-grid" / "patcher" / sha256(archive)
    if cache.exists():
        try:
            return validate_patcher(cache)
        except ValueError:
            # Only this content-addressed tool-owned extraction is replaced.
            if cache.is_dir():
                shutil.rmtree(cache)
            else:
                cache.unlink()
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix=".extract-", dir=cache.parent) as temporary:
            root = Path(temporary)
            with ZipFile(archive) as zipped:
                for member in zipped.infolist():
                    path = (root / member.filename).resolve()
                    if not path.is_relative_to(root.resolve()):
                        raise ValueError("FontPatcher ZIP contains a path outside the extraction directory")
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError("FontPatcher ZIP contains a symbolic link")
                zipped.extractall(root)
            possibilities = [root, root / "FontPatcher"]
            extracted = next((path for path in possibilities if (path / "font-patcher").is_file()), None)
            if extracted is None:
                raise ValueError(f"FontPatcher ZIP has no font-patcher script. {DOWNLOAD_MESSAGE}")
            validate_patcher(extracted)
            shutil.copytree(extracted, cache)
    except (BadZipFile, OSError) as error:
        shutil.rmtree(cache, ignore_errors=True)
        raise ValueError(f"Cannot read FontPatcher ZIP: {error}. {DOWNLOAD_MESSAGE}") from error
    return cache


def patcher_fingerprint(directory):
    import hashlib
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.name.endswith(".log"):
            continue
        if path.name == "font-patcher" or path.suffix.lower() in {".py", ".ttf", ".otf", ".sfd", ".json"}:
            digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
            digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def run_fontforge(arguments, workspace, log_path, cwd=None):
    executable = arguments[0]
    if shutil.which(executable) is None and not Path(executable).is_file():
        raise ValueError("FontForge is required for Nerd processing. Install FontForge with Python support")
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(Path(workspace) / ".han-grid" / "fontforge-config")
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.run(
            list(map(str, arguments)), cwd=cwd, env=environment,
            stdout=log, stderr=subprocess.STDOUT,
        )
    if process.returncode:
        tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-14:])
        raise ValueError(f"FontForge failed (exit {process.returncode}). Log: {log_path}\n{tail}")


def prepared_input(source, temporary):
    """FontForge currently accepts CFF1 and glyf but ignores CFF2."""
    with TTFont(source) as font:
        needs_conversion = "CFF2" in font or (
            "CFF " in font and hasattr(font["CFF "].cff.topDictIndex[0], "ROS")
        )
    if not needs_conversion:
        return source
    compatible = Path(temporary) / "input-cff1.otf"
    cff1_for_fontforge(source, compatible)
    return compatible


def preflight_info(source, directory, workspace, fontforge, fingerprint, ff_version, temporary):
    with TTFont(source) as font:
        key = coverage_digest(font)
    cache = Path(workspace) / ".han-grid" / "patcher-probes" / fingerprint / f"careful-{key}-{ff_version}.json"
    if cache.is_file():
        return read_json(cache)
    # Probe the patch sets with the same ASCII, box-drawing and braille
    # coverage, without converting a near-capacity CJK font to name-keyed CFF.
    probe_input = Path(temporary) / f"probe-input{source.suffix.lower()}"
    with TTFont(source, recalcTimestamp=False) as font:
        points = set(range(0x20, 0x7F)) | set(range(0x2500, 0x2600)) | set(range(0x2800, 0x2900))
        points |= {ord("中"), ord("水")}
        options = subset.Options(
            layout_features=[], layout_closure=False, name_IDs=["*"],
            name_languages=["*"], name_legacy=True, notdef_outline=True,
        )
        selection = subset.Subsetter(options=options)
        selection.populate(unicodes=points & font.getBestCmap().keys())
        selection.subset(font)
        font.save(probe_input)
    compatible = prepared_input(probe_input, Path(temporary) / "probe-compatible")
    result = Path(temporary) / "probe.json"
    log = Path(workspace) / ".han-grid" / "logs" / f"probe-{uuid.uuid4().hex}.log"
    run_fontforge(
        [fontforge, "-lang=py", "-script", Path(__file__).with_name("fontforge_probe.py"),
         directory / "font-patcher", compatible, result],
        workspace, log, cwd=directory,
    )
    info = read_json(result)
    info["fingerprint"] = fingerprint
    info["script_sha256"] = sha256(directory / "font-patcher")
    info["resource_sha256"] = {
        Path(path).relative_to(directory).as_posix(): sha256(path) for path in info.pop("resources")
    }
    write_json(cache, info)
    return info


def capacity_check(font, probe, skip_glyph_check=False):
    current = font["maxp"].numGlyphs
    # All copy operations plus FontForge's two possible control glyphs are an
    # upper bound. Replacements and overlapping icon sets reduce actual usage.
    projected = current + probe["copy_operations"] + probe["control_glyph_reserve"]
    if projected > MAX_GLYPHS and not skip_glyph_check:
        raise ValueError(
            f"Complete Nerd patch may require {projected} glyphs ({current} base plus "
            f"up to {probe['copy_operations']} icon copies and control glyphs), "
            f"exceeding {MAX_GLYPHS}. Reduce the selected regions"
        )
    return {"base_glyphs": current, "upper_bound": projected, "limit": MAX_GLYPHS,
            "preflight_skipped": skip_glyph_check}


def name_nerd_font(source, destination):
    from build_mono import postscript_name
    with temporary_output(destination) as temporary:
        with TTFont(source) as original:
            source_names = original["name"]
            base = source_names.getDebugName(16) or source_names.getDebugName(1)
            family = base + " NF"
            style = source_names.getDebugName(17) or source_names.getDebugName(2)
            legacy = source_names.getDebugName(1).replace(base, family, 1)
            fullname = source_names.getDebugName(4).replace(base, family, 1)
            legacy_style = source_names.getDebugName(2)
            ps_name = postscript_name(family, style, custom=True)
        with TTFont(destination, lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
            names = font["name"]
            replaced = {1, 2, 3, 4, 6, 16, 17, 18, 20, 21, 22, 25}
            names.names = [record for record in names.names if record.nameID not in replaced]
            for name_id, value in {
                1: legacy, 2: legacy_style, 3: f"HGMO;{ps_name}", 4: fullname,
                6: ps_name, 16: family, 17: style,
            }.items():
                names.setName(value, name_id, 3, 1, 0x409)
            font.save(temporary)
        with TTFont(temporary) as result:
            if result["name"].getDebugName(16) != family:
                raise ValueError("Failed to save the Nerd family name")


def glyph_bounds(font, codepoint):
    glyphs = font.getGlyphSet()
    pen = BoundsPen(glyphs)
    glyphs[font.getBestCmap()[codepoint]].draw(pen)
    return pen.bounds


def patch_one(source, destination, workspace, directory, fontforge, fingerprint, ff_version, braille="circle",
              skip_glyph_check=False):
    metadata = validate_identity(source, require_subset=True, allow_nerd=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TTFont(source) as original:
        base_coverage = set(original.getBestCmap())
        from .windows import unicode_coverage
        original_unicode = unicode_coverage(original)
        from build_mono import variation_sequences
        base_variations = set(variation_sequences(original))
        base_variation_advances = variation_advances(original)
        base_grid = {
            cp: original["hmtx"][glyph][0] for cp, glyph in original.getBestCmap().items()
            if category(chr(cp)) != "Co" and not 0x2800 <= cp <= 0x28FF
        }
        # Capacity rejection precedes any compatibility conversion or FontForge
        # open when the base alone leaves no room for icons.
        if not skip_glyph_check and original["maxp"].numGlyphs + 2 > MAX_GLYPHS:
            raise ValueError(f"No room for Nerd glyphs in {source}")

    with TemporaryDirectory(prefix=".nerd-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        probe = preflight_info(source, directory, workspace, fontforge, fingerprint, ff_version, temporary)
        with TTFont(source) as original:
            capacity = capacity_check(original, probe, skip_glyph_check=skip_glyph_check)
        if skip_glyph_check:
            print(f"Skipping Nerd glyph capacity preflight for {source.name} "
                  f"(estimated upper bound {capacity['upper_bound']})", flush=True)
        with TTFont(source) as original:
            protected = set(probe["destinations"]) & base_grid.keys()
            protected_bounds = {cp: glyph_bounds(original, cp) for cp in protected}
        compatible = prepared_input(source, temporary)

        patch_directory = temporary / "patched"
        patch_directory.mkdir()
        log = Path(workspace) / ".han-grid" / "logs" / f"nerd-{uuid.uuid4().hex}.log"
        print(f"Nerd patching {source.name} (glyph upper bound {capacity['upper_bound']})", flush=True)
        run_fontforge(
            [fontforge, "-script", directory / "font-patcher", "--complete", "--careful",
             "--quiet", "--extension", source.suffix[1:].lower(),
             "--outputdir", patch_directory, compatible],
            workspace, log, cwd=directory,
        )
        patched = sorted(patch_directory.glob(f"*{source.suffix.lower()}"))
        if len(patched) != 1:
            raise ValueError(f"Expected one patched font, found {len(patched)}. Log: {log}")
        patched = patched[0]
        with TTFont(patched) as font:
            if font["maxp"].numGlyphs > MAX_GLYPHS:
                raise ValueError(f"Nerd output exceeds the glyph limit. Reduce regions. Log: {log}")
            validate_grid(font, trusted=True)
            cmap = font.getBestCmap()
            if not base_coverage <= cmap.keys():
                raise ValueError(f"Nerd patcher lost base characters. Log: {log}")
            for cp, expected in base_grid.items():
                actual = font["hmtx"][cmap[cp]][0]
                if actual != expected:
                    raise ValueError(f"Nerd patcher changed advance of U+{cp:04X}: {expected} -> {actual}")
            for cp, expected in protected_bounds.items():
                actual = glyph_bounds(font, cp)
                if actual != expected and (actual is None or expected is None
                                           or any(abs(a - b) > 2 for a, b in zip(actual, expected))):
                    raise ValueError(f"Nerd patcher changed existing outline bounds for U+{cp:04X}")
            if not base_variations <= variation_sequences(font).keys():
                raise ValueError(f"Nerd patcher lost variation sequences. Log: {log}")
            actual_variation_advances = variation_advances(font)
            if any(
                actual_variation_advances[sequence] != width
                for sequence, width in base_variation_advances.items()
            ):
                raise ValueError(f"Nerd patcher changed variation glyph advances. Log: {log}")
            if 0xF013 not in cmap:
                raise ValueError("Complete Nerd output has no settings icon")

        # Repair only post.isFixedPitch and the required head checksum.
        repaired = temporary / f"repaired{source.suffix.lower()}"
        repair(patched, repaired, trusted=True)
        final = repaired
        if source.suffix.lower() == ".otf":
            final = temporary / "output.otf"
            cff2_from_fontforge(repaired, final, vertical_reference=compatible)

        from .braille import apply
        apply(final, braille)
        if braille == "preserve":
            with temporary_output(final) as temporary_font:
                with TTFont(final, lazy=True, recalcTimestamp=False, recalcBBoxes=False) as font:
                    font["OS/2"].recalcUnicodeRanges(font)
                    font.save(temporary_font)
        geometry = size_nerd_icons(final, original_unicode, strict_quotes=metadata.get("glyph_geometry", {}).get("strict_quotes", False))
        name_nerd_font(source, final)
        metadata = copy.deepcopy(metadata)
        metadata["nerd"] = True
        metadata["braille"] = braille
        metadata["source_font_sha256"] = sha256(source)
        metadata["patcher"] = {key: value for key, value in probe.items() if key != "destinations"}
        metadata["glyph_capacity"] = capacity
        metadata["glyph_geometry"] = geometry
        with TTFont(final) as font:
            validate_grid(font, trusted=True)
            metadata["family_name"] = font["name"].getDebugName(16) or font["name"].getDebugName(1)
            metadata["coverage_digest"] = coverage_digest(font)
        stamp_font(final, metadata)
        final.replace(destination)
    print(f"Built Nerd font {destination}", flush=True)
    return metadata, probe


def patch_fonts(paths, output, workspace, override=None, fontforge="fontforge", braille="circle",
                skip_glyph_check=False):
    paths = list(dict.fromkeys(Path(path).resolve() for path in paths))
    if not paths:
        raise ValueError("No subset fonts selected for Nerd processing")
    # Reject a Pan-CJK font before loading patcher resources or starting FontForge.
    identities = {
        path: validate_identity(path, require_subset=True, allow_nerd=True) for path in paths
    }
    skipped = [path for path in paths if identities[path].get("nerd")]
    paths = [path for path in paths if path not in skipped]
    if not paths:
        raise ValueError("All selected fonts already contain Nerd patches; choose original subset fonts")
    if shutil.which(fontforge) is None and not Path(fontforge).is_file():
        raise ValueError("Install FontForge with Python support to use Nerd processing")
    fontforge = str(Path(shutil.which(fontforge) or fontforge).resolve())
    directory = find_patcher(workspace, override)
    fingerprint = patcher_fingerprint(directory)
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(Path(workspace) / ".han-grid" / "fontforge-config")
    runtime = subprocess.run([
        fontforge, "-lang=py", "-c",
        "import fontforge, psMat; print('HAN_GRID_FF_VERSION=' + fontforge.version())",
    ], capture_output=True, text=True, errors="replace", env=environment, timeout=60)
    found = re.search(r"HAN_GRID_FF_VERSION=([A-Za-z0-9._-]+)", runtime.stdout)
    if runtime.returncode or found is None:
        raise ValueError("FontForge must provide Python support with fontforge and psMat modules")
    ff_version = found.group(1)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "nerd-manifest.json"
    report = new_manifest("nerd")
    report["skipped_already_patched"] = [str(path) for path in skipped]
    report["patcher"] = {"directory": str(directory), "fingerprint": fingerprint,
                         "glyph_preflight_skipped": skip_glyph_check}
    destinations = {}
    for path in paths:
        identity = identities[path]
        extension = path.suffix.lower()
        from .naming import filename
        destination = output / identity["group"] / "nerd" / extension[1:] / filename(identity, extension, nerd=True)
        from .io import check_destination, same_output
        check_destination(destination, output)
        if any(same_output(destination, other) for other in destinations.values()):
            raise ValueError("Duplicate Nerd destination; select one font per style and format")
        if any(same_output(destination, original) for original in paths):
            raise ValueError("Nerd processing requires a separate output path")
        destinations[path] = destination
    write_json(manifest_path, report)
    for source, destination in destinations.items():
        try:
            identity, probe = patch_one(
                source, destination, workspace, directory, fontforge, fingerprint, ff_version,
                braille, skip_glyph_check=skip_glyph_check,
            )
            report["fonts"].append(font_record(destination, output, identity))
            report["patcher"].update({
                "nerd_version": probe["nerd_version"], "script_version": probe["script_version"],
                "fontforge_version": probe["fontforge_version"],
            })
        except (ValueError, OSError, subprocess.CalledProcessError, TTLibError, struct.error, OverflowError) as error:
            report["failures"].append({"input": str(source), "error": str(error)})
            print(f"Failed Nerd processing for {source.name}: {error}", flush=True)
        write_json(manifest_path, report)
    if report["fonts"]:
        # Retain the licenses distributed with the locally supplied glyph sets.
        licenses = output / "NerdFont-Licenses"
        for path in directory.rglob("*"):
            if path.is_file() and (
                "license" in path.name.lower() or path.name.lower().startswith(("readme", "copying", "ofl"))
            ):
                relative = path.relative_to(directory)
                destination = licenses / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        for name in ("OFL-SourceHanSans.txt", "OFL-SourceHanMono.txt"):
            if (output / name).exists():
                continue
            found = None
            for source in paths:
                found = next((parent / name for parent in source.parents if (parent / name).is_file()), None)
                if found:
                    break
            if found:
                shutil.copyfile(found, output / name)
            else:
                # Standalone inputs can have been copied away from their
                # original output directory. Keep the bundled upstream OFL.
                bundled = Path(__file__).parent / "licenses" / name
                shutil.copyfile(bundled, output / name)
                report.setdefault("bundled_license_fallback", []).append(name)
        report["licenses"] = "NerdFont-Licenses/"
        write_json(manifest_path, report)
    return report, manifest_path
