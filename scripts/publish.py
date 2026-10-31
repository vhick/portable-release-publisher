#!/usr/bin/env python3
"""Upload validated ZIP and/or PortableApps .paf.exe assets to same-repo Releases.

Only files ending in .zip or .paf.exe are eligible; other executables are ignored.
Checks package signatures without launching or installing executables.
Uses GitHub CLI provided by GitHub-hosted runners; Python stdlib only.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

# Preserve the v2 marker, so existing publisher-owned Releases remain recognizable.
MARKER = "<!-- portable-release-publisher:managed:v2 -->"
MAX_ASSET_BYTES = 2 * 1024**3  # Release asset must be strictly smaller than 2 GiB.
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAFE_EVENTS = {"workflow_dispatch", "push", "schedule", "repository_dispatch"}


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def bool_setting(name: str) -> bool:
    text = setting(name, "false").lower()
    if text not in ("true", "false"):
        raise ValueError(f"{name} must be 'true' or 'false'")
    return text == "true"


def validate_tag(tag: str, prefix: str) -> None:
    if not prefix or not TAG_RE.fullmatch(prefix + "test"):
        raise ValueError("Invalid release tag-prefix")
    if not TAG_RE.fullmatch(tag):
        raise ValueError("tag must use letters, digits, dots, underscores or dashes; max length 151")
    if not tag.startswith(prefix):
        raise ValueError(f"tag must begin with tag-prefix '{prefix}'")


def kind(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".paf.exe"):
        return "paf"
    return None


def valid_windows_exe(path: Path) -> bool:
    """Check that .paf.exe is a PE executable (not full PAF/NSIS certification).

    Verify MZ header and PE signature at e_lfanew without executing the file.
    """
    if path.stat().st_size < 68:
        return False
    with path.open("rb") as f:
        header = f.read(64)
        if header[:2] != b"MZ":
            return False
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset < 64 or pe_offset > path.stat().st_size - 4:
            return False
        f.seek(pe_offset)
        return f.read(4) == b"PE\x00\x00"


def validate_asset(path: Path) -> str:
    asset_kind = kind(path)
    if asset_kind is None:
        raise ValueError(f"Unsupported release file (only .zip / .paf.exe): {path.name}")
    size = path.stat().st_size
    if not 0 < size < MAX_ASSET_BYTES:
        raise ValueError(f"File is empty or >= 2 GiB: {path.name}")
    if asset_kind == "zip" and not zipfile.is_zipfile(path):
        raise ValueError(f"Invalid ZIP archive: {path.name}")
    if asset_kind == "paf" and not valid_windows_exe(path):
        raise ValueError(f"Invalid .paf.exe Windows PE executable: {path.name}")
    return asset_kind


def collect_assets(patterns: str, workspace: Path) -> list[Path]:
    root = workspace.resolve(strict=True)
    selected: dict[str, Path] = {}
    for raw in re.split(r"[;\n]+", patterns):
        pat = raw.strip()
        if not pat:
            continue
        normalized = pat.replace("\\", "/")
        if (Path(normalized).is_absolute() or re.match(r"^[A-Za-z]:", normalized)
                or ".." in normalized.split("/")):
            raise ValueError(f"Unsafe input pattern (absolute/parent path): {pat}")
        for match in glob.glob(str(root / normalized), recursive=True):
            entry = Path(match)
            if entry.is_symlink() or not entry.is_file() or kind(entry) is None:
                continue
            resolved = entry.resolve(strict=True)
            if root not in resolved.parents:
                raise ValueError(f"Release asset escapes workspace: {entry}")
            validate_asset(resolved)
            if resolved.name in selected and selected[resolved.name] != resolved:
                raise ValueError(f"Duplicate asset filename from different folders: {resolved.name}")
            selected[resolved.name] = resolved
    if not selected:
        raise ValueError("No .zip or .paf.exe file found. Check 'files' and your finished output path.")
    if len(selected) > 999:
        raise ValueError("At most 999 packages allowed (plus SHA256SUMS.txt).")
    return [selected[name] for name in sorted(selected)]


def collect_zips(patterns: str, workspace: Path) -> list[Path]:
    """Compatibility alias for any older local tests importing this helper."""
    return collect_assets(patterns, workspace)


def checksums(assets: list[Path]) -> str:
    lines = []
    for asset in assets:
        digest = hashlib.sha256()
        with asset.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {asset.name}")
    return "\n".join(lines) + "\n"


def run_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["gh", *args], text=True, encoding="utf-8",
                            capture_output=True, check=False)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown GitHub CLI error"
        raise RuntimeError(f"GitHub CLI error during gh {args[0]}: {detail}")
    return result


def gh_json(*args: str) -> object:
    return json.loads(run_gh(*args).stdout)


def view_release(tag: str, repo: str) -> dict | None:
    result = run_gh("release", "view", tag, "--repo", repo,
                    "--json", "body,assets,isDraft,isImmutable,tagName", check=False)
    if result.returncode == 0:
        return json.loads(result.stdout)
    error = result.stderr.lower()
    if "not found" in error or "http 404" in error or "release not found" in error:
        return None
    raise RuntimeError("Release lookup failed (not assuming it is missing): " + result.stderr.strip())


def remote_assets_match(record: dict, assets: list[Path], sums: str, tag: str, repo: str) -> bool:
    expected = {p.name: p.stat().st_size for p in assets}
    expected["SHA256SUMS.txt"] = len(sums.encode("utf-8"))
    remote = {item.get("name"): item.get("size") for item in record.get("assets", [])}
    if remote != expected:
        return False
    with tempfile.TemporaryDirectory(prefix="publisher-check-") as tmp:
        run_gh("release", "download", tag, "--repo", repo,
               "--pattern", "SHA256SUMS.txt", "--dir", tmp)
        found = Path(tmp) / "SHA256SUMS.txt"
        # Binary equality guarantees exact LF bytes on Windows, too.
        # Text-mode reads hide CRLF differences and can cause false verification.
        return found.is_file() and found.read_bytes() == sums.encode("utf-8")


def verify_new_draft(tag: str, repo: str, assets: list[Path], sums: str) -> None:
    record = view_release(tag, repo)
    if not record or not record.get("isDraft"):
        raise RuntimeError("Draft release missing after upload; refusing to publish")
    if MARKER not in record.get("body", ""):
        raise RuntimeError("Draft is not marked as managed by this publisher")
    if not remote_assets_match(record, assets, sums, tag, repo):
        raise RuntimeError("Draft asset verification failed; draft left for review")


def choose_old_releases(releases: list[dict], prefix: str, active_tag: str,
                        keep: int, owned) -> list[str]:
    if keep <= 0:
        return []
    allowed = [r for r in releases
               if r.get("tagName", "").startswith(prefix)
               and not r.get("isDraft") and owned(r["tagName"])]
    allowed.sort(key=lambda r: r.get("publishedAt") or r.get("createdAt") or "", reverse=True)
    retained = {active_tag}
    for entry in allowed:
        if len(retained) >= keep:
            break
        retained.add(entry["tagName"])
    return [r["tagName"] for r in allowed
            if r["tagName"] not in retained and not r.get("isImmutable")]


def prune(repo: str, prefix: str, tag: str, keep: int) -> None:
    all_releases = gh_json("release", "list", "--repo", repo, "--limit", "1000",
                           "--json", "tagName,isDraft,isImmutable,createdAt,publishedAt")
    if len(all_releases) >= 1000:
        print("WARNING: 1000 releases returned; skipping prune (pagination might be incomplete).")
        return
    ownership: dict[str, bool] = {}
    def owned(name: str) -> bool:
        if name not in ownership:
            record = view_release(name, repo)
            ownership[name] = bool(record and MARKER in record.get("body", ""))
        return ownership[name]
    for old in choose_old_releases(all_releases, prefix, tag, keep, owned):
        print(f"Removing old mutable publisher-owned release: {old}", flush=True)
        run_gh("release", "delete", old, "--repo", repo, "--cleanup-tag", "--yes")


def main() -> int:
    tag = setting("PUBLISH_TAG")
    prefix = setting("PUBLISH_PREFIX", "portable-")
    validate_tag(tag, prefix)
    try:
        keep = int(setting("PUBLISH_KEEP", "3"))
    except ValueError as exc:
        raise ValueError("keep-releases must be an integer from 0 to 100") from exc
    if not 0 <= keep <= 100:
        raise ValueError("keep-releases must be an integer from 0 to 100")
    patterns = setting("PUBLISH_FILES")
    if not patterns:
        raise ValueError("files input is required")
    root = Path(setting("WORKSPACE_ROOT", str(Path.cwd())))
    assets = collect_assets(patterns, root)
    sums = checksums(assets)
    repo = setting("TARGET_REPO")
    title = setting("PUBLISH_TITLE") or tag
    print(f"Release: {tag}; repository: {repo or '(local dry run)'}")
    for asset in assets:
        print(f"  {asset.name} ({kind(asset)}, {asset.stat().st_size:,} bytes)")
    if bool_setting("PUBLISH_DRY_RUN"):
        print("DRY RUN: packages and checksums validated; no API calls or uploads made")
        return 0
    event = setting("ACTION_EVENT")
    if event not in SAFE_EVENTS:
        raise ValueError(f"Unsafe publishing event: {event!r}")
    if not REPO_RE.fullmatch(repo):
        raise ValueError("TARGET_REPO must be the calling owner/repo")
    if not setting("GH_TOKEN") or not setting("SOURCE_SHA"):
        raise ValueError("GH_TOKEN and SOURCE_SHA are required; set contents: write in caller")
    if not shutil.which("gh"):
        raise ValueError("gh executable not available on runner")
    old = view_release(tag, repo)
    if old:
        if old.get("isDraft"):
            raise RuntimeError("Existing draft with same tag; inspect before retrying")
        if MARKER not in old.get("body", ""):
            raise RuntimeError("An existing non-publisher release uses this tag; choose another tag")
        if not remote_assets_match(old, assets, sums, tag, repo):
            raise RuntimeError("Existing tag has different assets; increase builder revision/tag")
        print("Matching release exists; duplicate upload skipped")
    else:
        with tempfile.TemporaryDirectory(prefix="portable-release-") as tmp:
            sums_path = Path(tmp) / "SHA256SUMS.txt"
            # Write exact UTF-8 bytes; text mode can convert LF to CRLF on Windows.
            checksum_path = sums_path
            checksum_path.write_bytes(sums.encode("utf-8"))
            notes_path = Path(tmp) / "notes.md"
            notes_path.write_text(
                f"{MARKER}\n\nUnofficial portable build from this repository. "
                "Not endorsed by the upstream developer.\n\n"
                f"Builder run: https://github.com/{repo}/actions/runs/{setting('ACTION_RUN_ID')}\n\n"
                "Verify all downloaded ZIP and PAF installers against `SHA256SUMS.txt`.\n",
                encoding="utf-8")
            print("Creating draft release and uploading validated package(s)...")
            run_gh("release", "create", tag, *(str(p) for p in assets), str(sums_path),
                   "--repo", repo, "--target", setting("SOURCE_SHA"),
                   "--title", title, "--notes-file", str(notes_path), "--draft")
            verify_new_draft(tag, repo, assets, sums)
            run_gh("release", "edit", tag, "--repo", repo, "--draft=false")
            print(f"Published: https://github.com/{repo}/releases/tag/{tag}")
    if bool_setting("PUBLISH_PRUNE") and keep:
        prune(repo, prefix, tag, keep)
    else:
        print("Old-release pruning disabled")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
