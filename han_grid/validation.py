"""Static font, terminal-grid, and standalone font identity checks."""

import hashlib
import json
from pathlib import Path
import re

from fontTools import unicodedata
from fontTools.ttLib import TTFont

from build_mono import WEIGHTS, mappings_by_gid, variation_sequences, verify_terminal_grid
from .io import font_metadata, sha256
from .regions import PUBLIC_REGIONS

MAX_GLYPHS = 65535
REGION_ORDER = ("sc", "tw", "hk", "jp", "kr")


def normalize_regions(values):
    result = list(dict.fromkeys(value.lower() for value in values))
    if not result or any(value not in PUBLIC_REGIONS for value in result):
        raise ValueError("Choose one or more of sc, tw, hk, jp, kr")
    return result


def default_region(regions, requested=None):
    if requested:
        if requested not in regions:
            raise ValueError("--default-region must belong to --languages")
        return requested
    return "sc" if "sc" in regions else regions[0]


def region_label(regions, default):
    return "".join(region.upper() for region in [default, *(r for r in regions if r != default)])


def group_name(regions, default, coverage, family_base):
    profile = ("full-" if coverage == "pan-cjk" else "") + "-".join(
        region.upper() for region in regions
    ) + f"-default-{default.upper()}"
    if family_base != "Han Grid Mono":
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", family_base).strip("-")[:40] or "custom"
        digest = hashlib.sha256(family_base.encode("utf-8")).hexdigest()[:8]
        profile = f"{slug}-{digest}-{profile}"
    return profile


def grid_metrics(font):
    cmap = font.getBestCmap()
    return {
        cp: font["hmtx"][glyph][0]
        for cp, glyph in cmap.items()
        if 0x20 <= cp <= 0x7E or 0xFF01 <= cp <= 0xFF5E or 0x3000 <= cp <= 0x303F
        or unicodedata.script(chr(cp)) in {"Hani", "Hira", "Kana", "Hang", "Bopo"}
    }



def variation_advances(font):
    cmap = font.getBestCmap()
    metrics = font["hmtx"].metrics
    return {
        sequence: metrics[glyph or cmap[sequence[0]]][0]
        for sequence, glyph in variation_sequences(font).items()
    }


def coverage_digest(font):
    data = sorted(variation_sequences(font).keys())
    return hashlib.sha256(json.dumps({
        "codepoints": sorted(font.getBestCmap()), "variations": data,
    }).encode("ascii")).hexdigest()


def check_base_font(font, path, allow_filtered=False):
    if "fvar" in font or not ({"CFF ", "CFF2", "glyf"} & set(font.keys())):
        raise ValueError(f"Expected a static outline font: {path}")
    if font["maxp"].numGlyphs > MAX_GLYPHS:
        raise ValueError(f"Font exceeds the {MAX_GLYPHS}-glyph limit: {path}")
    if font["post"].isFixedPitch != 1 or font["OS/2"].panose.bProportion != 9:
        raise ValueError(f"Missing fixed-pitch metadata: {path}")
    verify_terminal_grid(font, path, allow_filtered)


def validate_identity(path, require_subset=True, allow_nerd=False):
    path = Path(path)
    if path.suffix.lower() not in {".otf", ".ttf"}:
        raise ValueError(f"Expected a standalone OTF or TTF: {path}")
    with TTFont(path, checkChecksums=2) as font:
        actual_format = ".ttf" if "glyf" in font else ".otf"
        if path.suffix.lower() != actual_format:
            raise ValueError(f"File extension does not match actual outline format: {path}")
        names = font["name"]
        if any(not names.getDebugName(name_id) for name_id in (1, 2, 4, 6)):
            raise ValueError(f"Font is missing required family/style names: {path}")
        metadata = font_metadata(font)
        if metadata:
            if metadata.get("coverage") not in {"subset", "pan-cjk"}:
                raise ValueError(f"Invalid coverage metadata: {path}")
            if require_subset and metadata["coverage"] != "subset" and not metadata.get("windows_trim"):
                raise ValueError(f"Untrimmed Pan-CJK inputs are prohibited; run trim or build --trim-windows first: {path}")
            if metadata.get("nerd") and not allow_nerd:
                raise ValueError(f"Font already contains Nerd patches: {path}")
            family = font["name"].getDebugName(16) or font["name"].getDebugName(1) or ""
            if metadata["family_name"] != family:
                raise ValueError(f"Font family no longer matches its build metadata: {path}")
            expected = metadata.get("coverage_digest")
            if expected and expected != coverage_digest(font):
                raise ValueError(f"Font coverage no longer matches its build metadata: {path}")
        else:
            # Support the project's earlier standalone subsets by inspecting
            # both the internal family and filename, not the filename alone.
            family = font["name"].getDebugName(16) or font["name"].getDebugName(1) or ""
            match = re.fullmatch(r"Han Grid Mono (SC|TW|HK|JP|KR) Subset", family)
            if not match or "Subset" not in path.stem:
                raise ValueError(f"Unknown font provenance; rebuild with han-grid: {path}")
            region = match.group(1).lower()
            style = names.getDebugName(17) or names.getDebugName(2)
            italic = bool(font["head"].macStyle & 2) or style.endswith("Italic")
            weight = style.removesuffix("Italic").strip() or "Regular"
            if weight not in WEIGHTS:
                raise ValueError(f"Unsupported legacy style {style}: {path}")
            metadata = {
                "schema": 1, "producer": "han-grid", "coverage": "subset",
                "languages": [region], "default_region": region,
                "group": f"{region.upper()}-default-{region.upper()}",
                "family_base": "Han Grid Mono", "family_name": family,
                "nerd": False, "legacy_input_sha256": sha256(path),
                "weight": weight, "italic": italic,
                "version": font["name"].getDebugName(5),
            }
            if "Nerd Font" in family:
                raise ValueError(f"Use the original subset font: {path}")
        if not isinstance(metadata.get("group"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+", metadata["group"]):
            raise ValueError(f"Invalid output group: {path}")
        if not isinstance(metadata.get("languages"), list) or not metadata["languages"] or any(
            not isinstance(region, str) or region not in PUBLIC_REGIONS for region in metadata["languages"]
        ):
            raise ValueError(f"Invalid font regions: {path}")
        if metadata.get("default_region") not in metadata["languages"]:
            raise ValueError(f"Invalid default region: {path}")
        if not isinstance(metadata.get("family_base"), str) or not metadata["family_base"].strip():
            raise ValueError(f"Invalid base family name: {path}")
        if metadata.get("weight") not in WEIGHTS or not isinstance(metadata.get("italic"), bool):
            raise ValueError(f"Invalid font style: {path}")
        if not metadata.get("nerd"):
            check_base_font(font, path, bool(metadata.get("windows_trim")))
        else:
            from repair_nerd_mono import validate_grid
            validate_grid(font, trusted=True)
            if font["post"].isFixedPitch != 1:
                raise ValueError(f"Nerd font is missing its fixed-pitch repair: {path}")
        if font["maxp"].numGlyphs > MAX_GLYPHS:
            raise ValueError(f"Font exceeds the {MAX_GLYPHS}-glyph limit: {path}")
        return metadata


def semantic_signature(font):
    return {
        "name": font["name"].getDebugName(6),
        "glyphs": font["maxp"].numGlyphs,
        "coverage": mappings_by_gid(font, font.getBestCmap(), variation_sequences(font)),
        "hmtx": [font["hmtx"][glyph] for glyph in font.getGlyphOrder()],
        "vmtx": [font["vmtx"][glyph] for glyph in font.getGlyphOrder()] if "vmtx" in font else None,
        "fixed_pitch": font["post"].isFixedPitch,
        "panose": font["OS/2"].panose.bProportion,
    }
