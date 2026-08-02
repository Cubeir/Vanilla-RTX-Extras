#!/usr/bin/env python3
"""
build_packs.py

Finds every manifest.json in the repo, builds a .mcpack for each pack, and
bundles them into .mcaddon file(s) as configured in BUNDLES below.

- The folder that directly contains a manifest.json is treated as that
  pack's root. The .mcpack is a zip of that folder's *contents* (no wrapper
  folder inside the archive).
- If a pack root has a "__enhancements" subfolder, two individual .mcpack
  variants are produced:
    <pack_id>.mcpack             -> base pack with __enhancements merged in
    <pack_id>-Unenhanced.mcpack  -> base pack only
  If there's no __enhancements folder, only <pack_id>.mcpack is produced.
- Anything named with a leading "__" (folders or files, e.g. __enhancements
  itself, or a stray __notes.txt inside it) is treated as tooling/notes and
  never ends up inside a shipped pack -- except __enhancements' *contents*,
  which are deliberately merged in for the "merged" variant.
- Only runs a build when at least one manifest's header.version differs from
  the last recorded run (tracked in .github/pack-versions.json). That file
  is rewritten on every successful build; the workflow commits it back.
"""
import json
import os
import shutil
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = REPO_ROOT / ".github" / "pack-versions.json"
DIST_DIR = REPO_ROOT / "dist"
WORK_DIR = REPO_ROOT / ".build-tmp"
CHANGELOG_FILE = REPO_ROOT / "CHANGELOG_RUN.md"
ENHANCEMENTS_DIR_NAME = "__enhancements"
JUNK_FILES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# --- repo-specific config ---------------------------------------------
# bundle filename (no extension) -> which per-pack variant to include:
#   "merged"     = enhancements merged in where available, base otherwise
#   "unenhanced" = always the base pack, even where enhancements exist
BUNDLES = {
    "All-Vanilla-RTX-Extras": "merged",
}
# ------------------------------------------------------------------------


def is_dunder(name: str) -> bool:
    return name.startswith("__")


def find_packs():
    """Every manifest.json's parent folder is a pack. Returns [(pack_id, pack_root), ...]."""
    packs = []
    for manifest_path in sorted(REPO_ROOT.rglob("manifest.json")):
        parts = manifest_path.relative_to(REPO_ROOT).parts
        if ".git" in parts or ENHANCEMENTS_DIR_NAME in parts:
            continue
        pack_root = manifest_path.parent
        pack_id = pack_root.name
        packs.append((pack_id, pack_root))
    return packs


def read_version(manifest_path: Path) -> str:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return ".".join(str(p) for p in data["header"]["version"])


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def copy_filtered(src: Path, dst: Path, skip_dirs=()):
    """Recursively copy src -> dst, skipping dunder-prefixed entries, named
    skip_dirs, and common OS junk files. Overwrites files already in dst."""
    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if not is_dunder(d) and d not in skip_dirs]
        rel = root_path.relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            if is_dunder(f) or f in JUNK_FILES:
                continue
            shutil.copy2(root_path / f, target_dir / f)


def zip_dir_contents(src: Path, zip_path: Path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src):
            for f in files:
                fp = Path(root) / f
                zf.write(fp, fp.relative_to(src))


def build_pack(pack_id: str, pack_root: Path):
    """Builds the .mcpack variant(s) for one pack. Returns {'merged': Path, 'unenhanced': Path|None}."""
    enh_dir = pack_root / ENHANCEMENTS_DIR_NAME
    has_enh = enh_dir.is_dir()

    base_dir = WORK_DIR / pack_id / "base"
    copy_filtered(pack_root, base_dir, skip_dirs=(ENHANCEMENTS_DIR_NAME,))

    if not has_enh:
        # Nothing to contrast against -- ship a single plain .mcpack.
        plain_zip = DIST_DIR / f"{pack_id}.mcpack"
        zip_dir_contents(base_dir, plain_zip)
        return {"merged": plain_zip, "unenhanced": None}

    unenh_zip = DIST_DIR / f"{pack_id}-Unenhanced.mcpack"
    zip_dir_contents(base_dir, unenh_zip)

    merged_dir = WORK_DIR / pack_id / "merged"
    shutil.copytree(base_dir, merged_dir)
    copy_filtered(enh_dir, merged_dir)  # overlay, overwrites on conflict

    merged_zip = DIST_DIR / f"{pack_id}.mcpack"
    zip_dir_contents(merged_dir, merged_zip)

    return {"merged": merged_zip, "unenhanced": unenh_zip}


def build_bundle(bundle_name: str, variant: str, pack_files: dict):
    bundle_path = DIST_DIR / f"{bundle_name}.mcaddon"
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for pack_id in sorted(pack_files):
            variants = pack_files[pack_id]
            src = variants.get(variant) or variants["merged"]
            zf.write(src, src.name)
    return bundle_path


def set_output(name: str, value: str):
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    else:
        print(f"[output] {name}={value}")


def main():
    packs = find_packs()
    if not packs:
        print("No manifest.json files found -- nothing to do.")
        set_output("should_build", "false")
        return

    old_versions = load_state()
    new_versions = {}
    changes = []
    for pack_id, pack_root in packs:
        version = read_version(pack_root / "manifest.json")
        new_versions[pack_id] = version
        old = old_versions.get(pack_id)
        if old != version:
            changes.append((pack_id, old, version))

    if not changes:
        print("No version changes since the last recorded build. Nothing to do.")
        set_output("should_build", "false")
        return

    print(f"Version changes detected in {len(changes)} pack(s):")
    for pack_id, old, new in changes:
        print(f"  {pack_id}: {old or '(new)'} -> {new}")

    for d in (DIST_DIR, WORK_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    pack_files = {}
    for pack_id, pack_root in packs:
        pack_files[pack_id] = build_pack(pack_id, pack_root)

    for bundle_name, variant in BUNDLES.items():
        build_bundle(bundle_name, variant, pack_files)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(new_versions, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    lines = ["## Changed packs", ""]
    for pack_id, old, new in changes:
        lines.append(f"- **{pack_id}**: {'new pack, ' if old is None else ''}`{old or '—'}` → `{new}`")
    CHANGELOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    set_output("should_build", "true")
    print(f"\nBuilt {len(pack_files)} pack(s) and {len(BUNDLES)} bundle(s) into {DIST_DIR}")


if __name__ == "__main__":
    main()
