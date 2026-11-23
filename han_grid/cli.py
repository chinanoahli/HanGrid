"""Unified command line interface."""

import argparse
from contextlib import ExitStack
from pathlib import Path
import re
import shutil
import struct
import subprocess

from fontTools.ttLib import TTLibError

from build_mono import WEIGHTS
from . import __version__
from .build import FONT_VERSION, build_fonts
from .collections import collect_fonts
from .io import new_manifest, read_inputs, read_json, workspace_lock, write_json
from .sources import update_sources


def region(value):
    value = value.lower()
    if value not in {"sc", "tw", "hk", "jp", "kr"}:
        raise argparse.ArgumentTypeError("Choose sc, tw, hk, jp, or kr")
    return value


def weight(value):
    names = {name.lower(): name for name in WEIGHTS}
    if value.lower() not in names:
        raise argparse.ArgumentTypeError(f"Choose one of {', '.join(WEIGHTS)}")
    return names[value.lower()]


def version(value):
    if not re.fullmatch(r"[0-9]+\.[0-9]{3}", value):
        raise argparse.ArgumentTypeError("Use a version such as 1.102")
    return value


def family_name(value):
    value = " ".join(value.split())
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise argparse.ArgumentTypeError("Use a nonempty font family name of at most 200 characters")
    return value


def make_parser():
    parser = argparse.ArgumentParser(prog="han-grid", description="Build and package Han Grid Mono fonts")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(),
                        help="Project directory containing han-grid.lock.json and Tools/")
    subcommands = parser.add_subparsers(dest="command", required=True)
    update = subcommands.add_parser("update", help="Explicitly fetch and lock upstream release versions")
    update.add_argument("--sans-version")
    update.add_argument("--mono-version")

    build = subcommands.add_parser("build", help="Build a regional subset or Pan-CJK fonts")
    build.add_argument("--languages", nargs="+", type=region, help="One subset union; default: sc")
    build.add_argument("--default-region", type=region)
    build.add_argument("--full", action="store_true")
    build.add_argument("--regions", nargs="+", type=region, help="Full-font default variants; default: all five")
    build.add_argument("--format", choices=("otf", "ttf", "both"), default="otf")
    build.add_argument("--weights", nargs="+", type=weight, default=list(WEIGHTS))
    build.add_argument("--styles", choices=("both", "upright", "italic"), default="both")
    build.add_argument("--family-name", type=family_name, default="Han Grid Mono",
                       help="Font family name; region and subset labels appear only in filenames")
    build.add_argument("--version", type=version, default=FONT_VERSION)
    build.add_argument("--jobs", type=int, default=2)
    build.add_argument("--output", type=Path)
    build.add_argument("--trim-windows", action="store_true", help="Prune only CJK using extracted Windows repertoires")
    build.add_argument("--windows-catalog", type=Path)
    build.add_argument("--braille", choices=("circle", "rectangle", "gapless", "preserve"), default="circle")
    build.add_argument("--nerd", action="store_true")
    build.add_argument("--collection", action="store_true")
    build.add_argument("--patcher", type=Path, help="Override local FontPatcher directory or ZIP")
    build.add_argument("--fontforge", default="fontforge")

    nerd = subcommands.add_parser("nerd", help="Patch verified subsets or trimmed full fonts; restore fixed pitch")
    nerd.add_argument("inputs", nargs="+", type=Path)
    nerd.add_argument("--output", type=Path)
    nerd.add_argument("--patcher", type=Path)
    nerd.add_argument("--fontforge", default="fontforge")
    nerd.add_argument("--collection", action="store_true")
    nerd.add_argument("--braille", choices=("circle", "rectangle", "gapless", "preserve"), default="circle")

    extract = subcommands.add_parser("extract-windows", help="Extract all local Windows font CJK repertoires")
    extract.add_argument("--source", type=Path)
    extract.add_argument("--output", type=Path)

    trim = subcommands.add_parser("trim", help="Prune CJK in existing base OTF/TTF fonts")
    trim.add_argument("inputs", nargs="+", type=Path)
    trim.add_argument("--windows-catalog", type=Path)
    trim.add_argument("--output", type=Path)
    trim.add_argument("--nerd", action="store_true")
    trim.add_argument("--collection", action="store_true")
    trim.add_argument("--patcher", type=Path)
    trim.add_argument("--fontforge", default="fontforge")
    trim.add_argument("--braille", choices=("circle", "rectangle", "gapless", "preserve"), default="circle")

    for command in (build, nerd, trim):
        command.add_argument(
            "--skip-nerd-glyph-check", action="store_true",
            help="Skip Nerd glyph capacity preflight for testing; output validation still applies",
        )

    collect = subcommands.add_parser("collect", help="Package eligible OTFs into OTC, or TTFs into TTC")
    collect.add_argument("inputs", nargs="+", type=Path)
    collect.add_argument("--output", type=Path)
    return parser


def validate_build_arguments(args):
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    if args.full and args.nerd and not args.trim_windows:
        raise ValueError("Nerd patching is prohibited for untrimmed Pan-CJK fonts; first use --trim-windows or trim")
    if args.full and args.collection and not args.trim_windows:
        raise ValueError("Collections are supported for regional subsets and Nerd outputs; choose --languages")


def package(report, manifest_path, paths, output):
    try:
        report["collections"] = collect_fonts(paths, output / "collections", manifest_root=output)
    except Exception as error:
        report["failures"].append({"stage": "collection", "error": str(error)})
        write_json(manifest_path, report)
        raise


def run(args):
    workspace = args.workspace.resolve()
    if args.command in {"build", "update"} and shutil.which("git") is None:
        raise ValueError("Git is required. Install Git and initialize the source submodules")
    if args.command == "update":
        lock = update_sources(workspace, args.sans_version, args.mono_version)
        print(f"Locked Sans {lock['sans']['version']} and Mono {lock['mono']['version']}")
        return

    from .windows import default_catalog
    if args.command == "extract-windows":
        from .windows import extract
        extract(args.source or workspace / "Tools" / "WindowsLocalizedFonts",
                args.output or default_catalog(workspace, for_extraction=True))
        return
    if args.command in {"build", "trim"}:
        args.windows_catalog = (args.windows_catalog or default_catalog(workspace)).resolve()
    from .nerd import patch_fonts
    args.output = (args.output or workspace / "dist" / "han-grid").resolve()
    if args.command == "build":
        validate_build_arguments(args)
        report, manifest_path = build_fonts(args, workspace)
        base_paths = [
            (manifest_path.parent / record["path"]).resolve() for record in report["fonts"]
        ]
        if args.nerd:
            try:
                nerd_report, _ = patch_fonts(
                    base_paths, args.output, workspace, args.patcher, args.fontforge, args.braille,
                    skip_glyph_check=args.skip_nerd_glyph_check
                )
            except Exception as error:
                report["failures"].append({"stage": "nerd", "error": str(error)})
                write_json(manifest_path, report)
                raise
            report["fonts"].extend(nerd_report["fonts"])
            report["failures"].extend(nerd_report["failures"])
            report["nerd"] = nerd_report["patcher"]
            write_json(manifest_path, report)
        if args.collection:
            paths = [(manifest_path.parent / entry["path"]).resolve() for entry in report["fonts"]]
            package(report, manifest_path, paths, args.output)
            write_json(manifest_path, report)
        if report["failures"]:
            raise ValueError(f"Some requested outputs failed. See {manifest_path}")
        return

    paths = read_inputs(args.inputs)
    if args.command == "trim":
        from .trimming import trim_fonts
        report, manifest_path = trim_fonts(paths, args.output, args.windows_catalog)
        if args.nerd and report["fonts"]:
            bases = [(manifest_path.parent / item["path"]).resolve() for item in report["fonts"]]
            try:
                patched, _ = patch_fonts(
                    bases, args.output, workspace, args.patcher, args.fontforge, args.braille,
                    skip_glyph_check=args.skip_nerd_glyph_check,
                )
            except Exception as error:
                report["failures"].append({"stage": "nerd", "error": str(error)})
                write_json(manifest_path, report)
                raise
            report["fonts"].extend(patched["fonts"])
            report["failures"].extend(patched["failures"])
        if args.collection and report["fonts"]:
            members = [(manifest_path.parent / item["path"]).resolve() for item in report["fonts"]]
            package(report, manifest_path, members, args.output)
        write_json(manifest_path, report)
        if report["failures"]:
            raise ValueError(f"Some trim outputs failed. See {manifest_path}")
    elif args.command == "nerd":
        report, manifest_path = patch_fonts(
            paths, args.output, workspace, args.patcher, args.fontforge, args.braille,
            skip_glyph_check=args.skip_nerd_glyph_check,
        )
        if args.collection and report["fonts"]:
            patched = [(manifest_path.parent / entry["path"]).resolve() for entry in report["fonts"]]
            package(report, manifest_path, patched, args.output)
            write_json(manifest_path, report)
        if report["failures"]:
            raise ValueError(f"Some Nerd outputs failed. See {manifest_path}")
    else:
        report = new_manifest("collect")
        manifest_path = args.output / "collection-manifest.json"
        package(report, manifest_path, paths, args.output)
        write_json(args.output / "collection-manifest.json", report)


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        with ExitStack() as stack:
            workspace = args.workspace.resolve()
            stack.enter_context(workspace_lock(workspace))
            if args.command != "update":
                from .windows import default_catalog
                default_output = default_catalog(workspace, for_extraction=True) if args.command == "extract-windows" else workspace / "dist" / "han-grid"
                args.output = (args.output or default_output).resolve()
                if args.output != workspace:
                    stack.enter_context(workspace_lock(args.output))
            run(args)
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, TTLibError, struct.error, OverflowError) as error:
        parser.exit(1, f"error: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted. Completed fonts remain available; temporary face directories are cleaned.\n")
