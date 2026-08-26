#!/usr/bin/env python3
"""Vendoring tool for the img2threejs skill copy under vendor/img2threejs.

The skill is vendored, not submoduled, so this repository stays self-contained: clone it and
`auto3d.py run` works with no second checkout. The cost of vendoring is drift, so this tool makes
drift visible and the refresh repeatable.

    python3 tools/sync_upstream.py check
        Hash every vendored file and compare against vendor/img2threejs/VENDORED.json.
        Reports files edited locally, added, or deleted since the last sync. Exit 1 on drift.

    python3 tools/sync_upstream.py update --from ../img2threejs
        Re-copy the vendored paths from an img2threejs checkout, record its git commit, and
        rewrite the manifest. Prints added/changed/removed counts. --dry-run shows the plan.

Local edits to vendor/ are legitimate (a patch you need before upstream takes it) — `check` exists
so an update never silently throws them away. Record why in the manifest's `localPatches` list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENDOR_ROOT = PROJECT_ROOT / "vendor" / "img2threejs"
MANIFEST = VENDOR_ROOT / "VENDORED.json"

UPSTREAM_URL = "https://github.com/img2threejs/img2threejs.git"

# upstream path -> vendored path (same name unless renamed to avoid confusion)
VENDORED_PATHS: dict[str, str] = {
    "SKILL.md": "SKILL.md",
    "LICENSE": "LICENSE",
    "README.md": "README.md",
    "CHANGELOG.md": "CHANGELOG.md",
    "forge": "forge",
    "grimoire": "grimoire",
    "docs": "docs",
    "skills": "skills",
    "scripts": "scripts",
}

# .cache/ is written by forge at runtime (spec search index); it is not part of the vendored copy.
SKIP_DIRS = {"__pycache__", ".git", ".cache", "node_modules", ".venv", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_NAMES = {".DS_Store", "VENDORED.json"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk(root: Path):
    """Every vendored file under root, as (posix-relative-path, absolute path)."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_NAMES or Path(name).suffix in SKIP_SUFFIXES:
                continue
            absolute = Path(dirpath) / name
            yield absolute.relative_to(root).as_posix(), absolute


def hash_tree(root: Path) -> dict[str, str]:
    return {rel: sha256(absolute) for rel, absolute in walk(root)}


def read_manifest() -> dict:
    if not MANIFEST.is_file():
        raise SystemExit(f"no manifest at {MANIFEST} — run `update --from <img2threejs checkout>` first")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def write_manifest(data: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def cmd_check(_args: argparse.Namespace) -> int:
    manifest = read_manifest()
    recorded: dict[str, str] = manifest.get("files", {})
    current = hash_tree(VENDOR_ROOT)

    changed = sorted(p for p in recorded.keys() & current.keys() if recorded[p] != current[p])
    added = sorted(current.keys() - recorded.keys())
    removed = sorted(recorded.keys() - current.keys())

    upstream = manifest.get("upstream", {})
    print(f"vendored from {upstream.get('url', UPSTREAM_URL)} @ {upstream.get('commit', '?')[:12]} ({upstream.get('syncedAt', '?')})")
    print(f"{len(current)} files tracked")
    for label, items in (("locally modified", changed), ("added since sync", added), ("missing since sync", removed)):
        if items:
            print(f"\n{label} ({len(items)}):")
            for item in items[:40]:
                print(f"  {item}")
            if len(items) > 40:
                print(f"  … and {len(items) - 40} more")
    patches = manifest.get("localPatches") or []
    if patches:
        print("\ndeclared local patches:")
        for patch in patches:
            print(f"  {patch}")
    if not (changed or added or removed):
        print("\nclean — vendored tree matches the manifest")
        return 0
    return 1


def copy_path(source: Path, destination: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if destination.exists():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*SKIP_DIRS, "*.pyc", "*.pyo", ".DS_Store"),
            symlinks=False,
        )
    else:
        shutil.copy2(source, destination)


def cmd_update(args: argparse.Namespace) -> int:
    upstream_root = Path(args.source).expanduser().resolve()
    if not (upstream_root / "SKILL.md").is_file() or not (upstream_root / "forge").is_dir():
        raise SystemExit(f"{upstream_root} does not look like an img2threejs checkout")

    if args.ref:
        if git(upstream_root, "rev-parse", "--verify", args.ref) == "":
            raise SystemExit(f"ref {args.ref!r} not found in {upstream_root}")
        if not args.dry_run:
            print(f"checking out {args.ref} in {upstream_root}")
            subprocess.run(["git", "checkout", args.ref], cwd=str(upstream_root), check=True)

    before = hash_tree(VENDOR_ROOT)
    manifest_before = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.is_file() else {}
    recorded = manifest_before.get("files", {})
    local_edits = sorted(p for p in recorded.keys() & before.keys() if recorded[p] != before[p])
    if local_edits and not args.force:
        print("refusing to overwrite locally modified vendored files:")
        for item in local_edits[:20]:
            print(f"  {item}")
        print("\nre-run with --force once you have carried those changes upstream or recorded them")
        print("in VENDORED.json's localPatches, or run `check` to review them first.")
        return 1

    missing = [src for src in VENDORED_PATHS if not (upstream_root / src).exists()]
    if missing:
        raise SystemExit("upstream checkout is missing: " + ", ".join(missing))

    for source_rel, dest_rel in VENDORED_PATHS.items():
        copy_path(upstream_root / source_rel, VENDOR_ROOT / dest_rel, dry_run=args.dry_run)

    after = before if args.dry_run else hash_tree(VENDOR_ROOT)
    changed = sorted(p for p in before.keys() & after.keys() if before[p] != after[p])
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())

    commit = git(upstream_root, "rev-parse", "HEAD")
    describe = git(upstream_root, "describe", "--tags", "--always")
    branch = git(upstream_root, "rev-parse", "--abbrev-ref", "HEAD")
    subject = git(upstream_root, "log", "-1", "--format=%s")

    manifest = {
        "_comment": "Generated by tools/sync_upstream.py — do not edit files under vendor/ by hand "
                    "without recording it in localPatches.",
        "upstream": {
            "url": UPSTREAM_URL,
            "commit": commit,
            "describe": describe,
            "branch": branch,
            "subject": subject,
            "syncedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "syncedFrom": str(upstream_root),
        },
        "vendoredPaths": VENDORED_PATHS,
        "localPatches": manifest_before.get("localPatches", []),
        "files": after,
    }
    if not args.dry_run:
        write_manifest(manifest)

    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}vendored {len(after)} files from {upstream_root} @ {commit[:12] or '(not a git checkout)'}")
    print(f"{prefix}changed {len(changed)} · added {len(added)} · removed {len(removed)}")
    for label, items in (("changed", changed), ("added", added), ("removed", removed)):
        for item in items[:15]:
            print(f"  {label[0].upper()} {item}")
        if len(items) > 15:
            print(f"  … and {len(items) - 15} more {label}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="report drift between vendor/ and its manifest")
    check.set_defaults(func=cmd_check)

    update = sub.add_parser("update", help="re-vendor from an img2threejs checkout")
    update.add_argument("--from", dest="source", required=True, metavar="PATH", help="path to an img2threejs checkout")
    update.add_argument("--ref", default=None, help="git ref to check out in that checkout first (e.g. v1.5.1)")
    update.add_argument("--force", action="store_true", help="overwrite locally modified vendored files")
    update.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    update.set_defaults(func=cmd_update)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
