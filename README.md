# Universal Portable GitHub Release Publisher v2.0

Reusable **composite action** for independent portable builders. Uploads completed ZIPs **directly** to Releases in the **calling repository**.

- Public caller = public release. Private caller = private release.
- The publisher does not target other repositories, does not create Actions artifacts, and never requires a personal access token for same-repository publishing.
- Compatible with GitHub-hosted Windows and Linux runners with Python and GitHub CLI.
- Requires a unique **stable release tag** corresponding to the upstream source version/revision **and** your builder revision.
- Checks ZIP validity, size, SHA-256; includes `SHA256SUMS.txt` as a release asset.
- Creates a draft and checks uploaded file names, sizes and checksum list before publishing it.
- For the **same tag**, skips an identical already-published release, rejects files that changed, and refuses to overwrite manually created releases or drafts.
- Optional retention keeps 3 latest publisher-managed **mutable** releases by default when `prune: true` is enabled. Drafts, manually authored releases, and immutable releases are never deleted.

## Install once

Create public `vhick/portable-release-publisher` on GitHub.com. Clone it in GitHub Desktop and copy the **contents** of this `publisher` directory into the clone root (so `action.yml` is at the top level). Commit and push.

The included `.github/workflows/test.yml` runs Python tests in the shared repository without publishing any Releases or storing artifacts.

After the tests are green, create Git tag **v1** on the publisher repository (on GitHub.com: Releases > Draft a new release > choose `v1` as a new tag targeting `main`, publish the publisher-code release with **no binaries**). Your builders can then reference `vhick/portable-release-publisher@v1`.

For maximum supply-chain stability, reference the tested **40-character commit SHA** instead of `@v1`. Keep `v1` stable; introduce new major tags for incompatible future changes.

## Integration for a new standalone builder

Preserve the current checkout/source-fetch/build/packaging steps. In that SAME job after the portable ZIP exists:

```yaml
permissions:
  contents: write

jobs:
  build:
    runs-on: windows-latest  # keep your builder's existing OS and jobs
    steps:
      # ... YOUR EXISTING STEPS THAT PRODUCE dist/MyAppPortable.zip ...
      - name: Publish portable ZIP to this repository
        uses: vhick/portable-release-publisher@v1
        with:
          files: 'dist/MyAppPortable-*.zip'  # CHANGE to actual finished ZIP path
          tag: 'portable-v1.2.3-r1'          # CHANGE when upstream OR builder changes
          release-title: 'MyApp Portable v1.2.3 (r1)'
          keep-releases: '3'
          prune: 'false'                    # set true only after first releases work
```

**Important:** `permissions:` must be added at the right job/workflow level, not inserted into `steps:`. If the workflow uses multiple jobs, the publishing step must run in the one that still has the ZIP on disk. A separate publishing job requires a file-transfer strategy (often temporary Actions artifacts).

For stable automatic names based on the upstream source commit, add the following before the publish step (ONLY if the builder cloned Git source to that location):

```yaml
      - name: Identify upstream source revision
        id: upstream
        shell: bash
        run: |
          # Change upstream/MyApp to your actual upstream clone directory.
          HASH="$(git -C upstream/MyApp rev-parse --short=12 HEAD)"
          echo "tag=portable-src-${HASH}-r1" >> "$GITHUB_OUTPUT"
      - name: Publish portable ZIP
        uses: vhick/portable-release-publisher@v1
        with:
          files: 'dist/MyAppPortable-*.zip'
          tag: ${{ steps.upstream.outputs.tag }}
          release-title: MyApp Portable
          keep-releases: '3'
          prune: 'false'
```

If upstream code remains the same but your packaging script changes, increment `r1` to `r2` so the new ZIP gets a new tag. The publisher refuses to replace different ZIPs under an existing tag.

## Safety, permissions, and retention

1. Use `workflow_dispatch` and/or controlled `push` triggers. **Never** publish from untrusted PR events. The publisher blocks them.
2. Set `permissions: contents: write` on the publishing job. GitHub's built-in `GITHUB_TOKEN` writes to the same repo only. Keep `actions/checkout` set to `persist-credentials: false` when practical.
3. `prune: false` is the default. After testing, switch to `prune: true` if you want to keep the latest 3 mutable releases **created by this publisher**. Old releases will be deleted; preserve important versions separately. Immutable releases cannot be pruned and may exceed 3 total.
4. GitHub's Releases asset limit is **under 2 GiB per file** and **1000 assets per release**. GitHub does not state unlimited Actions runtime; private Komorebi still consumes included compute usage.
5. Avoid any other `actions/upload-artifact` step once direct publishing works. Temporary copies already stored there continue counting until deleted/expired.
6. In public builder repositories, publish third-party programs only if redistribution is allowed by their licenses. Private personal usage does not grant permission to redistribute publicly.
7. **Trust model:** same-job publishing avoids temporary Actions artifacts, but the job has write-capable GitHub permissions while building third-party code. Don't run untrusted PRs; review fetched source/build scripts. A separate isolated publishing job is safer for untrusted code but needs a transfer method and potentially temporary storage.
8. Do not enable GitHub release immutability and expect automatic deletion of old immutable releases. This publisher skips them.
9. GitHub can change limits, permission requirements, or installed runner tools. We cannot guarantee that no limit will ever be reached; monitor billing and use a spending hard stop.

## Inputs

| Input | Required | Default | Purpose |
|---|---|---|---|
| `files` | Yes | | Semicolon/newline-separated ZIP globs inside the builder workspace |
| `tag` | Yes | | Stable tag beginning `portable-` by default |
| `release-title` | No | Tag | Display title |
| `tag-prefix` | No | `portable-` | Reserved namespace for publisher-owned releases |
| `keep-releases` | No | `3` | Keep N recent releases when pruning is enabled; `0` disables pruning |
| `prune` | No | `false` | Opt-in automatic old-release deletion |
| `dry-run` | No | `false` | Validate and report without publishing |

## Troubleshooting

- **No ZIP found:** Correct the `files` glob; it must point to the final packaged ZIP, not the downloaded original source.
- **403 Resource not accessible:** Set `contents: write` for the build job, and ensure release policies permit publishing.
- **Existing tag has different files:** Increment builder revision in `tag` (`-r2`) rather than overwrite.
- **Draft already exists:** Open Releases, review the draft and any partial assets. Remove it manually if appropriate, then retry.
- **Files in a separate build job:** This action cannot see files on a different runner. Call it in the package job or explicitly transfer the ZIP.
- **Old releases not removed:** `prune` defaults to `false`; immutable and manually created releases are intentionally excluded.
- **A workflow-change commit fails to publish:** On some GitHub API paths, release creation against a commit that modifies workflow files can require additional workflow authorization. Try a later manual run on the established default branch; do not broaden credentials without reviewing the error.

Official references: https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases , https://cli.github.com/manual/gh_release_create , https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax .
