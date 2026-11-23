"""Executed by FontForge's Python interpreter to inspect the local patcher's patch sets."""

import json
import logging
from pathlib import Path
import sys

import fontforge

patcher_path, input_path, output_path = map(Path, sys.argv[1:4])
sys.argv = [str(patcher_path), "--complete", "--careful", "--dry", str(input_path)]
namespace = {
    "__file__": str(patcher_path), "__name__": "_han_grid_patcher_probe",
    "__dir__": str(patcher_path.parent),
}
exec(compile(patcher_path.read_bytes(), str(patcher_path), "exec"), namespace)
namespace["logger"] = logging.getLogger("han-grid-probe")
namespace["allversions"] = f"{namespace['version']} ({namespace['script_version']})"
args, config = namespace["setup_arguments"]()
patcher = namespace["font_patcher"](args, config)
font = fontforge.open(str(input_path))
patcher.patch(font)
destinations = set()
copied = 0
resources = set()
for entry in patcher.patch_set:
    if not entry["Enabled"]:
        continue
    if "Filename" in entry:
        resource = Path(args.glyphdir) / entry["Filename"]
        resources.add(str(resource.resolve()))
        symbols = fontforge.open(str(resource))
    else:
        symbols = entry["Font"]()
    symbols.selection.select(("ranges", "unicode"), entry["SymStart"], entry["SymEnd"])
    selected = [glyph for glyph in symbols.selection.byGlyphs if glyph.unicode >= 0]
    codes = []
    last = -1
    for glyph in selected:
        possible = [glyph.unicode] if glyph.unicode > last else []
        if glyph.altuni:
            possible.extend(cp for cp, _, _ in glyph.altuni if cp > last)
        if not possible:
            continue
        last = min(possible)
        codes.append(last)
    if entry["Exact"]:
        destinations.update(codes)
    else:
        start = entry["SrcStart"] or entry["SymStart"]
        destinations.update(range(start, start + len(codes)))
    copied += len(codes)
    symbols.close()
# FontForge can add .null and nonmarkingreturn control glyphs.
output = {
    "nerd_version": namespace["version"], "script_version": namespace["script_version"],
    "fontforge_version": fontforge.version(),
    "destinations": sorted(destinations), "copy_operations": copied,
    "resources": sorted(resources), "control_glyph_reserve": 2,
}
Path(output_path).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
font.close()
