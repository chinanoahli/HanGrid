"""Atomic files, font identity, and workspace operation locking."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fontTools import __version__ as fonttools_version
from fontTools.ttLib import TTFont, newTable

from . import __version__ as tool_version

SCHEMA = 1
META_TAG = "hgrd"


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def temporary_output(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.stem}-",
                            suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        yield temporary
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    with temporary_output(path) as temporary:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def font_metadata(font):
    if "meta" not in font or META_TAG not in font["meta"].data:
        return None
    raw = font["meta"].data[META_TAG]
    metadata = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if metadata.get("schema") != SCHEMA or metadata.get("producer") != "han-grid":
        raise ValueError("Unsupported Han Grid font metadata")
    return metadata


def stamp_font(path, metadata):
    """Add tool identity without recompiling the font's other tables."""
    with temporary_output(path) as temporary:
        with TTFont(path, lazy=True, recalcBBoxes=False, recalcTimestamp=False) as font:
            if "meta" not in font:
                font["meta"] = newTable("meta")
            font["meta"].data[META_TAG] = json.dumps(metadata, sort_keys=True).encode("utf-8")
            font.save(temporary)
        with TTFont(temporary, checkChecksums=2) as result:
            if font_metadata(result) != metadata:
                raise ValueError(f"Failed to save font metadata in {path}")


def font_record(path, root, metadata):
    with TTFont(path) as font:
        return {
            "path": Path(os.path.relpath(path, root)).as_posix(),
            "sha256": sha256(path),
            "postscript_name": font["name"].getDebugName(6),
            "glyphs": font["maxp"].numGlyphs,
            "codepoints": len(font.getBestCmap()),
            "format": Path(path).suffix[1:],
            "identity": metadata,
        }


def read_inputs(inputs):
    """Resolve font files and integrity-checked manifests."""
    paths = []
    for argument in inputs:
        path = Path(argument).resolve()
        if path.suffix.lower() == ".json":
            report = read_json(path)
            if report.get("schema") != SCHEMA or report.get("producer") != "han-grid":
                raise ValueError(f"Unsupported font manifest: {path}")
            for record in report.get("fonts", []):
                file = (path.parent / record["path"]).resolve()
                if sha256(file) != record["sha256"]:
                    raise ValueError(f"Font has changed since the manifest was written: {file}")
                paths.append(file)
        else:
            paths.append(path)
    paths = list(dict.fromkeys(paths))
    if not paths or any(not path.is_file() or path.suffix.lower() not in {".otf", ".ttf"} for path in paths):
        raise ValueError("Supply existing standalone OTF/TTF fonts or a Han Grid manifest")
    return paths


def new_manifest(operation, sources=None):
    return {
        "schema": SCHEMA, "producer": "han-grid", "tool_version": tool_version,
        "fonttools_version": fonttools_version, "operation": operation,
        "sources": sources or {}, "fonts": [], "collections": [], "failures": [],
    }


@contextmanager
def workspace_lock(workspace):
    """OS locks release automatically on process exit, including crashes."""
    state = Path(workspace) / ".han-grid"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "operation.lock").open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("Another Han Grid operation is using this workspace; retry after it finishes") from error
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)


def check_destination(destination, output):
    if not Path(destination).resolve().is_relative_to(Path(output).resolve()):
        raise ValueError(f"Output path escapes the output directory: {destination}")


def same_output(first, second):
    first, second = Path(first).resolve(), Path(second).resolve()
    return first == second or (first.exists() and second.exists() and first.samefile(second))
