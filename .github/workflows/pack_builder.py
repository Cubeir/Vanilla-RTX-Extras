#!/usr/bin/env python3
"""
pack_builder.py — drop-in Minecraft Bedrock resource pack builder + releaser.

Lives right next to the workflow that runs it (.github/workflows/) so the
entire tool is two files in one folder - copy that folder into any repo
that hosts Bedrock resource packs and it works with zero code changes.

What it does, in order:
  1. Decides whether a real rebuild is warranted: compares header.version
     in every manifest.json touched by this push (git diff BEFORE_SHA vs
     AFTER_SHA) against its previous committed value. A file being merely
     *touched* (e.g. a description edit) does NOT count - only an actual
     version change does. No state file anywhere; git history is the only
     source of truth. FORCE=true (manual workflow_dispatch runs) always
     builds, skipping this check.
  2. If a rebuild is warranted: every manifest.json anywhere in the repo
     is discovered on its own (no folder list to maintain). Each pack is
     zipped as <folder-name>.mcpack - the contents of the folder
     containing that manifest, flattened (no wrapper directory in the
     zip). An "__enhancements" folder (name configurable) next to a
     manifest, if present, is merged into that pack before zipping,
     unconditionally. Anything named with a leading "__" is treated as
     tooling/notes and never shipped, except __enhancements' own contents,
     which are deliberately merged in.
  3. If more than one pack was built, all of them are also bundled into
     <BUNDLE_NAME_PREFIX>-<UTC date>-<time>.mcaddon (just a zip of the
     individual .mcpacks). Exactly one pack -> no bundle.

Env vars (all optional, set by the workflow):
  BEFORE_SHA             commit SHA before the push (github.event.before)
  AFTER_SHA              commit SHA after the push (github.event.after)
  FORCE                  "true" to always build, skipping the version check
  ENHANCEMENTS_DIR_NAME  default "__enhancements"
  BUNDLE_NAME_PREFIX     default "All-Packs" - the date-time is always
                         appended regardless of what this is set to
"""
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ZERO_SHA = "0" * 40
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

ENHANCEMENTS_DIR_NAME = os.environ.get("ENHANCEMENTS_DIR_NAME", "__enhancements")
BUNDLE_NAME_PREFIX = os.environ.get("BUNDLE_NAME_PREFIX", "All-Packs")


def git(*args, cwd=None):
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    return result.stdout if result.returncode == 0 else None


def repo_root() -> Path:
    out = git("rev-parse", "--show-toplevel")
    return Path(out.strip()) if out else Path.cwd()


def set_output(name: str, value: str):
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"[output] {name}={value}")


def is_dunder(name: str) -> bool:
    return name.startswith("__")


def extract_version(text):
    if not text:
        return None
    try:
        data = json.loads(text)
        return ".".join(str(p) for p in data["header"]["version"])
    except Exception:
        return None


# --------------------------- step 1: should we build? ---------------------------

def should_build(root: Path) -> bool:
    if os.environ.get("FORCE", "false").lower() == "true":
        print("Manual run -- building unconditionally.")
        return True

    before = os.environ.get("BEFORE_SHA", "")
    after = os.environ.get("AFTER_SHA", "")

    if not before or before == ZERO_SHA:
        print("No previous commit to compare against (new branch) -- building everything.")
        return True

    diff = git("diff", "--name-only", before, after, cwd=root)
    if diff is None:
        print("Could not diff commits -- building to be safe.")
        return True

    manifest_paths = [p for p in diff.splitlines() if p.endswith("manifest.json")]
    if not manifest_paths:
        print("This push didn't touch any manifest.json -- nothing to do.")
        return False

    changed = []
    for path in manifest_paths:
        old_version = extract_version(git("show", f"{before}:{path}", cwd=root))
        full_path = root / path
        new_version = extract_version(full_path.read_text(encoding="utf-8")) if full_path.exists() else None
        if old_version != new_version:
            changed.append((path, old_version, new_version))

    if changed:
        print(f"Real version change in {len(changed)} manifest(s):")
        for path, old, new in changed:
            print(f"  {path}: {old or '(new pack)'} -> {new or '(removed)'}")
        return True

    print("manifest.json touched, but header.version didn't actually change -- skipping.")
    return False


# --------------------------- step 2: build ---------------------------

def find_packs(root: Path):
    roots = []
    for manifest_path in sorted(root.rglob("manifest.json")):
        parts = manifest_path.relative_to(root).parts
        if ".git" in parts or ENHANCEMENTS_DIR_NAME in parts:
            continue
        roots.append(manifest_path.parent)

    name_counts = {}
    for r in roots:
        name_counts[r.name] = name_counts.get(r.name, 0) + 1

    packs = []
    for r in roots:
        if name_counts[r.name] > 1:
            pack_id = "-".join(r.relative_to(root).parts)  # disambiguate collisions
        else:
            pack_id = r.name
        packs.append((pack_id, r))
    return packs


def copy_filtered(src: Path, dst: Path, skip_dirs=()):
    """Recursively copy src -> dst, skipping dunder-prefixed entries, named
    skip_dirs, and common OS junk files. Overwrites files already in dst."""
    for r, dirs, files in os.walk(src):
        r_path = Path(r)
        dirs[:] = [d for d in dirs if not is_dunder(d) and d not in skip_dirs]
        target_dir = dst / r_path.relative_to(src)
        target_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if is_dunder(f) or f in JUNK_FILES:
                continue
            shutil.copy2(r_path / f, target_dir / f)


def zip_dir_contents(src: Path, zip_path: Path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for r, _, files in os.walk(src):
            for f in files:
                fp = Path(r) / f
                zf.write(fp, fp.relative_to(src))


def build_pack(pack_id: str, pack_root: Path, dist_dir: Path, work_dir: Path) -> Path:
    build_dir = work_dir / pack_id
    copy_filtered(pack_root, build_dir, skip_dirs=(ENHANCEMENTS_DIR_NAME,))

    enh_dir = pack_root / ENHANCEMENTS_DIR_NAME
    if enh_dir.is_dir():
        copy_filtered(enh_dir, build_dir)  # merge, overwrites on conflict

    zip_path = dist_dir / f"{pack_id}.mcpack"
    zip_dir_contents(build_dir, zip_path)
    return zip_path


def build_bundle(mcpack_paths, dist_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    bundle_path = dist_dir / f"{BUNDLE_NAME_PREFIX}-{stamp}.mcaddon"
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(mcpack_paths):
            zf.write(p, p.name)
    return bundle_path


def build_everything(root: Path):
    dist_dir = root / "dist"
    work_dir = root / ".build-tmp"
    for d in (dist_dir, work_dir):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    packs = find_packs(root)
    if not packs:
        print("No manifest.json files found in this repo -- nothing to build.")
        set_output("pack_count", "0")
        return

    print(f"Found {len(packs)} pack(s):")
    built = {}
    for pack_id, pack_root in packs:
        version = extract_version((pack_root / "manifest.json").read_text(encoding="utf-8")) or "unknown"
        has_enh = (pack_root / ENHANCEMENTS_DIR_NAME).is_dir()
        print(f"  {pack_id} (v{version}){' [enhancements merged]' if has_enh else ''}")
        built[pack_id] = (build_pack(pack_id, pack_root, dist_dir, work_dir), version)

    if len(built) > 1:
        bundle_path = build_bundle([p for p, _ in built.values()], dist_dir)
        print(f"\nBundled all {len(built)} packs into {bundle_path.name}")

    lines = [f"## Packs in this build ({len(built)})", ""]
    for pack_id in sorted(built):
        _, version = built[pack_id]
        lines.append(f"- **{pack_id}** — v{version}")
    (root / "RELEASE_NOTES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    shutil.rmtree(work_dir, ignore_errors=True)
    set_output("pack_count", str(len(built)))
    print(f"\nBuilt {len(built)} pack(s) into {dist_dir}")


def main():
    root = repo_root()
    if not should_build(root):
        set_output("should_build", "false")
        set_output("pack_count", "0")
        return

    set_output("should_build", "true")
    build_everything(root)


if __name__ == "__main__":
    main()
