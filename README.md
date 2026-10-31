## v2.1.1 checksum compatibility fix

- Windows-safe raw UTF-8 byte writing for `SHA256SUMS.txt`.
- Byte-for-byte checksum verification, avoiding CRLF normalization discrepancies.
- Added checksum regression tests and updated CI checkout to Node.js 24-compatible checkout v6.
- Existing ZIP and `.paf.exe` publishing, same-repository privacy, release tags,
  duplicate checks and optional retention are preserved.

# Universal Portable Release Publisher — PAF support update (v2.1)

Publish **`.zip`**, **`.paf.exe`**, or **both** as release assets in the *calling builder repository*.

This is a drop-in nonbreaking update of the v2 shared action. Its action inputs, SHA-256 checksum filename, draft-first release flow, managed-release marker, deduplication, optional release pruning, and visibility handling are preserved.

## Supported output formats

| Output | Example file | `files` glob |
|---|---|---|
| NATIVE ZIP | `MyAppPortable-1.2.zip` | `dist/*.zip` |
| PortableApps `.paf.exe` | `MyAppPortable_1.2.paf.exe` | `dist/*.paf.exe` |
| BOTH | ZIP and PAF installer | `dist/*.zip;dist/*.paf.exe` or multiline patterns |

**`.paf.exe` is a PortableApps.com installer**, not a regular ZIP file. The publisher checks its Windows PE executable header rather than trying to parse it as a ZIP. It *does not execute* the installer, and it does not certify that the installer fully complies with the PortableApps format. Regular `.exe` installers or launchers are **not** published by this action.

A single GitHub Release can contain both packages and one `SHA256SUMS.txt` file with a digest for each file.

## Integration in a builder

After the same job finishes building and packaging:

```yaml
# At the JOB level (not inside steps):
permissions:
  contents: write

# In existing build job's steps, AFTER ZIP/PAF generation:
- name: Publish portable packages
  uses: vhick/portable-release-publisher@v1.1.0
  with:
    files: |
      dist/*.zip
      dist/*.paf.exe
    tag: 'portable-v1.2.3-r1'
    release-title: 'MyApp Portable v1.2.3 (r1)'
    keep-releases: '3'
    prune: 'false'
    dry-run: 'true'
```

The `dist` folder and stable tag are **examples**. Replace them with your actual build output paths/version-detection expression. For a real publish change `dry-run` to `'false'`.

If no PAF installer is produced for the selected NATIVE mode, its unmatched glob is harmless as long as a finished ZIP exists. For PAF-only builds, use only `dist/*.paf.exe`. The action does not create `.paf.exe`; your *builder* must generate it first.

## Inputs (unchanged)

| Input | Default | Description |
|---|---|---|
| `files` | Required | Semicolon/newline-separated relative glob patterns for output packages |
| `tag` | Required | Stable tag starting with `portable-` by default |
| `release-title` | Tag | Human-readable label |
| `tag-prefix` | `portable-` | Prefix of managed release tags |
| `keep-releases` | `3` | Maximum retained recent mutable managed releases if pruning enabled |
| `prune` | `false` | Opt-in deletion of old publisher-managed mutable releases |
| `dry-run` | `false` | Preview without publishing/deleting |

## What stays the same

- Repository-private builds publish **private** releases in the same repository; public builds publish public releases.
- Uses the caller's default `GITHUB_TOKEN` and needs `contents: write` in the publishing job.
- Creates a draft, uploads files and `SHA256SUMS.txt`, checks remote filenames/sizes/checksum file, and publishes only afterward.
- If a release has exactly the same tag and assets, skips it. If package content changes, change the builder revision/tag. Do **not** overwrite immutable or existing conflicting releases.
- Only old releases with the previous v2 marker `<!-- portable-release-publisher:managed:v2 -->` can be pruned. Default pruning is off.
- No `actions/upload-artifact` is required if packages are made in the **same job** and uploaded directly from the runner.
- Never use this from untrusted pull-request workflows. Building untrusted upstream code inside a write-permission job carries supply-chain risk.

## Limits / validation

- Every file must be smaller than **2 GiB**. GitHub supports up to 1,000 assets per release; the action reserves one for checksums.
- A `.paf.exe` is checked only for its `.paf.exe` suffix, size, and minimal Windows PE format. Use a real PortableApps installer build and functional test for full PAF compatibility.
- ZIP validity is checked using the Python `zipfile` structural check; this is not a complete installed-app smoke test.
- If you use multiple packaging jobs on separate runners, the files are not automatically shared; publish within the packaging job or explicitly implement a safe transfer mechanism.
- Filename collisions across package directories are rejected.
- Public redistribution requires upstream licensing permission. Published `.paf.exe` packages may trigger antivirus warnings depending on signing and origin; this action cannot prevent that.
- Github CLI (`gh`) and Python must be available on the runner. These are present on standard GitHub-hosted Windows and Ubuntu images, but verify in your environment.
- A release might become immutable once published, in which case old-release cleanup cannot delete it.

## Test command

```sh
python -m unittest discover -s tests -v
```

No release publishing is performed by the publisher repository's tests.

References: https://portableapps.com/development/portableapps.com_format and https://cli.github.com/manual/gh_release_create .
