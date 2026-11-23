"""Explicit upstream updates and pinned, read-only build inputs."""

from pathlib import Path
import os
import re
import subprocess

from build_mono import CMAP_LANGUAGES, REGIONS, WEIGHTS, git
from .io import read_json, temporary_output, write_json

UPSTREAM = {
    "sans": ("https://github.com/adobe-fonts/source-han-sans.git",
             "OTF/Japanese/SourceHanSans-Regular.otf"),
    "mono": ("https://github.com/adobe-fonts/source-han-mono.git",
             "Regular/OTC/SourceHanMono-Regular.otf"),
}


def load_sources(workspace):
    workspace = Path(workspace)
    path = workspace / "han-grid.lock.json"
    if not path.is_file():
        raise ValueError("Missing han-grid.lock.json. Run 'han-grid update' or use the project lockfile")
    lock = read_json(path)
    if lock.get("schema") != 1:
        raise ValueError(f"Unsupported source lockfile: {path}")
    for key, (_, sample) in UPSTREAM.items():
        entry = lock[key]
        repo = (workspace / entry["repository"]).resolve()
        commit = entry["commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"{key} must be pinned to an exact Git commit")
        try:
            actual = git(repo, "rev-parse", f"{commit}^{{commit}}").decode().strip()
            git(repo, "cat-file", "-e", f"{commit}:{sample}")
            git(repo, "cat-file", "-e", f"{commit}:{'LICENSE.txt' if key == 'sans' else 'LICENSE.md'}")
        except subprocess.CalledProcessError as error:
            raise ValueError(f"Missing {key} source objects. Initialize submodules or run 'han-grid update'") from error
        if commit != actual:
            raise ValueError(f"Source commit mismatch for {key}")
    return lock


def latest_tag(url, name):
    output = subprocess.check_output(["git", "ls-remote", "--tags", "--refs", url], text=True, timeout=60)
    versions = []
    for line in output.splitlines():
        tag = line.split("\t", 1)[1].removeprefix("refs/tags/")
        match = re.fullmatch(r"v?(\d+)\.(\d+)(R?)", tag)
        if match:
            major, minor, release = match.groups()
            versions.append(((int(major), int(minor), bool(release)), tag))
    if not versions:
        raise ValueError(f"No version tags found for {name}; specify --{name}-version")
    if name == "sans":
        releases = [entry for entry in versions if entry[1].endswith("R")]
        if releases:
            versions = releases
    return max(versions)[1]


def update_sources(workspace, sans_version=None, mono_version=None):
    workspace = Path(workspace)
    lock = {"schema": 1}
    for key, requested in (("sans", sans_version), ("mono", mono_version)):
        url, sample = UPSTREAM[key]
        version = requested or latest_tag(url, key)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version):
            raise ValueError(f"Invalid {key} version tag")
        repo = workspace / ".han-grid" / "upstream" / key
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        # Fetch metadata first. A release also contains large variable fonts
        # and collections that this tool does not use.
        for option, value in (
            ("remote.origin.url", url), ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ("remote.origin.promisor", "true"), ("remote.origin.partialclonefilter", "blob:none"),
        ):
            subprocess.run(["git", "-C", str(repo), "config", option, value], check=True)
        subprocess.run([
            "git", "-C", str(repo), "fetch", "--quiet", "--depth=1", "--filter=blob:none", "--no-tags",
            "origin", f"refs/tags/{version}",
        ], check=True, timeout=180)
        commit = git(repo, "rev-parse", "FETCH_HEAD^{commit}").decode().strip()
        tree = git(repo, "ls-tree", "-r", commit).decode().splitlines()
        objects = set()
        paths = set()
        for line in tree:
            info, path = line.split("\t", 1)
            object_id = info.split()[2]
            if key == "sans":
                wanted = (
                    re.fullmatch(
                        r"OTF/(Japanese|Korean|SimplifiedChinese|TraditionalChinese|TraditionalChineseHK)/[^/]+\.otf",
                        path,
                    )
                    or re.fullmatch(r"SubsetOTF/(CN|TW|HK|JP|KR)/[^/]+\.otf", path)
                    or path == "LICENSE.txt"
                )
            else:
                wanted = (
                    re.fullmatch(r"[^/]+/OTC/SourceHanMono[^/]+\.otf", path)
                    or path == "LICENSE.md"
                )
            if wanted:
                objects.add(object_id)
                paths.add(path)
        if key == "sans":
            expected = {"LICENSE.txt"}
            expected.update(f"OTF/{directory}/SourceHanSans{suffix}-{weight}.otf"
                            for directory, suffix, _, _ in REGIONS.values() for weight in WEIGHTS)
            expected.update(f"SubsetOTF/{locale}/SourceHanSans{locale}-{weight}.otf"
                            for locale in CMAP_LANGUAGES.values() for weight in WEIGHTS)
        else:
            expected = {"LICENSE.md"}
            expected.update(f"{weight}/OTC/SourceHanMono{suffix}-{weight}{style}.otf"
                            for _, suffix, _, _ in REGIONS.values()
                            for weight in WEIGHTS for style in ("", "It"))
        missing = expected - paths
        if missing:
            raise ValueError(f"{key} tag {version} is missing required files: {', '.join(sorted(missing)[:5])}")
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", "origin", "--stdin"],
            input="\n".join(sorted(objects)) + "\n", text=True,
            stdout=subprocess.DEVNULL, check=True, timeout=300,
        )
        environment = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
        inventory = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check=%(objecttype)"],
            input="\n".join(f"{commit}:{path}" for path in sorted(expected)) + "\n",
            env=environment, capture_output=True, text=True, check=True, timeout=60,
        )
        if inventory.stdout.splitlines() != ["blob"] * len(expected):
            raise ValueError(f"{key} tag {version} is missing required local font objects")
        git(repo, "update-ref", f"refs/han-grid/releases/{version}", commit)
        lock[key] = {
            "repository": repo.relative_to(workspace).as_posix(),
            "url": url, "version": version, "commit": commit,
        }
    # Write only after both upstream snapshots have passed validation.
    write_json(workspace / "han-grid.lock.json", lock)
    return lock


def copy_licenses(workspace, lock, destination):
    for key, source, target in (
        ("sans", "LICENSE.txt", "OFL-SourceHanSans.txt"),
        ("mono", "LICENSE.md", "OFL-SourceHanMono.txt"),
    ):
        entry = lock[key]
        data = git(Path(workspace) / entry["repository"], "show", f"{entry['commit']}:{source}")
        with temporary_output(Path(destination) / target) as temporary:
            temporary.write_bytes(data)
