#!/usr/bin/env python3
"""Single-repository ZIP-to-GitHub-Releases publisher, using stdlib and gh CLI.

Runs as the final step of a GitHub Actions build job. Does not store Actions artifacts.
Scopes itself to GITHUB_REPOSITORY and publisher-owned releases.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

MARKER = "<!-- portable-release-publisher:managed:v2 -->"
MAX_ASSET_BYTES = 2 * 1024**3  # each release asset must be strictly smaller than 2 GiB
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SAFE_EVENTS = {"workflow_dispatch", "push", "schedule", "repository_dispatch"}


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def bool_setting(name: str) -> bool:
    val = setting(name, "false").lower()
    if val not in ("true", "false"):
        raise ValueError(f"{name} must be 'true' or 'false'")
    return val == "true"


def validate_tag(tag: str, prefix: str) -> None:
    if not prefix or not TAG_RE.fullmatch(prefix + "test"):
        raise ValueError("Invalid tag-prefix")
    if not TAG_RE.fullmatch(tag):
        raise ValueError("tag is required; use letters, digits, dots, underscores or dashes (max 151 characters)")
    if not tag.startswith(prefix):
        raise ValueError(f"tag must begin with the reserved prefix '{prefix}'")


def collect_zips(patterns: str, workspace: Path) -> list[Path]:
    root = workspace.resolve(strict=True)
    selected: dict[str, Path] = {}
    for raw in re.split(r"[;\n]+", patterns):
        pat = raw.strip()
        if not pat:
            continue
        normalized = pat.replace("\\", "/")
        if (Path(normalized).is_absolute() or re.match(r"^[A-Za-z]:", normalized)
                or ".." in normalized.split("/")):
            raise ValueError(f"Unsafe ZIP pattern (absolute/parent path): {pat}")
        for matched in glob.glob(str(root / normalized), recursive=True):
            candidate = Path(matched)
            if candidate.is_symlink() or not candidate.is_file() or candidate.suffix.lower() != ".zip":
                continue
            resolved = candidate.resolve(strict=True)
            if root not in resolved.parents:
                raise ValueError(f"ZIP escapes the caller workspace: {candidate}")
            byte_size = resolved.stat().st_size
            if not 0 < byte_size < MAX_ASSET_BYTES:
                raise ValueError(f"ZIP is empty or 2 GiB or larger: {candidate}")
            if not zipfile.is_zipfile(resolved):
                raise ValueError(f"Not a valid ZIP archive: {candidate}")
            if resolved.name in selected and selected[resolved.name] != resolved:
                raise ValueError(f"Two different ZIPs have the same filename: {resolved.name}")
            selected[resolved.name] = resolved
    if not selected:
        raise ValueError("No finished ZIP found. Check 'files' against your actual build output folder.")
    if len(selected) > 999:
        raise ValueError("GitHub permits at most 1000 assets per release (including SHA256SUMS.txt).")
    return [selected[name] for name in sorted(selected)]


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
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Unknown GitHub CLI error"
        raise RuntimeError(f"GitHub CLI error during 'gh {args[0]} {args[1] if len(args)>1 else ''}': {detail}")
    return result


def gh_json(*args: str) -> object:
    return json.loads(run_gh(*args).stdout)


def view_release(tag: str, repo: str) -> dict | None:
    result = run_gh("release", "view", tag, "--repo", repo,
                    "--json", "body,assets,isDraft,isImmutable,tagName", check=False)
    if result.returncode == 0:
        return json.loads(result.stdout)
    reason = result.stderr.lower()
    if "not found" in reason or "http 404" in reason or "release not found" in reason:
        return None
    raise RuntimeError("Could not read release; refusing to assume it is missing: " + result.stderr.strip())


def remote_assets_match(record: dict, zips: list[Path], sums: str, tag: str, repo: str) -> bool:
    expected = {p.name: p.stat().st_size for p in zips}
    expected["SHA256SUMS.txt"] = len(sums.encode("utf-8"))
    actual = {a.get("name"): a.get("size") for a in record.get("assets", [])}
    if actual != expected:
        return False
    with tempfile.TemporaryDirectory(prefix="publisher-check-") as temp:
        run_gh("release", "download", tag, "--repo", repo, "--pattern", "SHA256SUMS.txt", "--dir", temp)
        sums_path = Path(temp) / "SHA256SUMS.txt"
        return sums_path.exists() and sums_path.read_text(encoding="utf-8") == sums


def verify_new_draft(tag: str, repo: str, zips: list[Path], sums: str) -> None:
    remote = view_release(tag, repo)
    if remote is None or not remote.get("isDraft"):
        raise RuntimeError("Draft release was not found after upload; refusing to publish")
    if MARKER not in remote.get("body", ""):
        raise RuntimeError("Draft release is not marked as publisher-owned")
    if not remote_assets_match(remote, zips, sums, tag, repo):
        raise RuntimeError("Draft release verification failed; leaving draft for manual inspection")


def choose_old_releases(releases: list[dict], prefix: str, protected_tag: str,
                        keep: int, is_publisher_owned) -> list[str]:
    """Only old, mutable, explicitly managed releases qualify for deletion."""
    if keep <= 0:
        return []
    relevant = []
    for entry in releases:
        tag = entry.get("tagName", "")
        if (tag.startswith(prefix) and not entry.get("isDraft")
                and is_publisher_owned(tag)):
            relevant.append(entry)
    relevant.sort(key=lambda r: (r.get("publishedAt") or r.get("createdAt") or ""), reverse=True)
    protected = {protected_tag}
    for entry in relevant:
        if len(protected) >= keep:
            break
        protected.add(entry["tagName"])
    return [r["tagName"] for r in relevant
            if r["tagName"] not in protected and not r.get("isImmutable")]


def prune(repo: str, prefix: str, tag: str, keep: int) -> None:
    # 1000 is enough for typical personal builder repositories. Refuse partial retention views.
    listing = gh_json("release", "list", "--repo", repo, "--limit", "1000", "--json",
                      "tagName,isDraft,isImmutable,createdAt,publishedAt")
    if len(listing) >= 1000:
        print("WARNING: >=1000 releases returned; pruning disabled to avoid incomplete pagination")
        return
    cache: dict[str, bool] = {}
    def owned(name: str) -> bool:
        if name not in cache:
            record = view_release(name, repo)
            cache[name] = bool(record and MARKER in record.get("body", ""))
        return cache[name]
    for old in choose_old_releases(listing, prefix, tag, keep, owned):
        print(f"Pruning previous publisher-managed release: {old}", flush=True)
        run_gh("release", "delete", old, "--repo", repo, "--cleanup-tag", "--yes")


def main() -> int:
    files = setting("PUBLISH_FILES")
    tag = setting("PUBLISH_TAG")
    prefix = setting("PUBLISH_PREFIX", "portable-")
    validate_tag(tag, prefix)
    try:
        keep = int(setting("PUBLISH_KEEP", "3"))
    except ValueError as exc:
        raise ValueError("keep-releases must be an integer between 0 and 100") from exc
    if not 0 <= keep <= 100:
        raise ValueError("keep-releases must be between 0 and 100")
    if not files:
        raise ValueError("files must contain the finished ZIP path or glob")
    root = Path(setting("WORKSPACE_ROOT", str(Path.cwd())))
    assets = collect_zips(files, root)
    sums = checksums(assets)
    title = setting("PUBLISH_TITLE") or tag
    repo = setting("TARGET_REPO")
    print(f"Release: {tag} in {repo or '(local dry-run)'}", flush=True)
    for a in assets:
        print(f"  {a.name} ({a.stat().st_size:,} bytes)", flush=True)
    if bool_setting("PUBLISH_DRY_RUN"):
        print("DRY RUN: validated input ZIPs and checksum; no GitHub changes made.")
        return 0
    event = setting("ACTION_EVENT")
    if event not in SAFE_EVENTS:
        raise ValueError(f"Publishing disallowed for event {event!r}; use manual, push, schedule or repository_dispatch")
    if not REPO_RE.fullmatch(repo):
        raise ValueError("Invalid or missing calling repository")
    if not setting("GH_TOKEN"):
        raise ValueError("GitHub job token missing; grant contents: write in the calling build job")
    if not setting("SOURCE_SHA"):
        raise ValueError("GitHub commit SHA is missing")
    if shutil.which("gh") is None:
        raise ValueError("GitHub CLI is required on the runner")
    previous = view_release(tag, repo)
    if previous:
        if previous.get("isDraft"):
            raise RuntimeError(f"Draft {tag} already exists; inspect it before retrying. Never overwrite a draft automatically.")
        if MARKER not in previous.get("body", ""):
            raise RuntimeError(f"Release {tag} already exists but was not created by this publisher. Use a different tag.")
        if not remote_assets_match(previous, assets, sums, tag, repo):
            raise RuntimeError(f"Release {tag} already exists with different ZIP(s). Change the builder revision/tag; never overwrite silently.")
        print("Release already exists with matching checksums; skipping duplicate upload.")
    else:
        with tempfile.TemporaryDirectory(prefix="portable-release-") as temp:
            checksum_path = Path(temp) / "SHA256SUMS.txt"
            checksum_path.write_text(sums, encoding="utf-8")
            notes_path = Path(temp) / "notes.md"
            notes_path.write_text(
                f"{MARKER}\n\nUnofficial portable build for personal or permitted redistribution.\n\n"
                "This release is generated by the repository's independent portable builder, "
                "not by the upstream application developer.\n\n"
                f"Builder run: https://github.com/{repo}/actions/runs/{setting('ACTION_RUN_ID')}\n\n"
                "Verify ZIP files against `SHA256SUMS.txt`.\n", encoding="utf-8")
            print("Creating an unpublished draft and uploading ZIP(s)...", flush=True)
            run_gh("release", "create", tag, *(str(a) for a in assets), str(checksum_path),
                   "--repo", repo, "--target", setting("SOURCE_SHA"),
                   "--title", title, "--notes-file", str(notes_path), "--draft")
            verify_new_draft(tag, repo, assets, sums)
            run_gh("release", "edit", tag, "--repo", repo, "--draft=false")
            print(f"Published https://github.com/{repo}/releases/tag/{tag}", flush=True)
    if bool_setting("PUBLISH_PRUNE") and keep:
        prune(repo, prefix, tag, keep)
    else:
        print("Old-release pruning disabled (safe default).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
