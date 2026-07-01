# ado2jfrog — Azure Artifacts → JFrog Artifactory Migration

Scripts to migrate packages from Azure Artifacts to JFrog Artifactory.
All support **org-scoped and project-scoped** Azure feeds and handle all
package versions (or latest-only with a flag).

| Script | Package type | Azure protocol | JFrog repo type |
|--------|-------------|----------------|-----------------|
| `migrate_npm_to_jfrog.py` | npm (`.tgz`) | npm | npm local |
| `migrate_nuget_to_jfrog.py` | NuGet (`.nupkg`) | NuGet v3 flat | NuGet local |
| `migrate_maven_to_jfrog.py` | Maven (`.jar` + `.pom`) | maven | Maven local |
| `migrate_gradle_to_jfrog.py` | Gradle (`.jar` + `.pom` + `.module`) | maven¹ | Gradle/Maven local |
| `migrate_pip_to_jfrog.py` | pip (`.whl`, `.tar.gz`) | pypi | PyPI local |

> ¹ Azure Artifacts has no dedicated Gradle protocol. Gradle projects publish
> to Maven feeds — the Gradle migrator reads from a Maven feed and additionally
> captures Gradle Module Metadata (`.module`) files.

## Prerequisites

- Python 3.9+
- An Azure DevOps **PAT** with `Packaging (read)` scope
- A JFrog **Access Token** with deploy permissions on the target repo

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install requests
```

## Authentication

Tokens are resolved in this order for each run:

1. **Environment variable** — `AZURE_PAT` / `JFROG_TOKEN` *(recommended)*
2. **CLI flag** — `--az-pat` / `--jfrog-token`
3. **Interactive prompt** — if neither of the above is set

```bash
export AZURE_PAT="<your-azure-pat>"
export JFROG_TOKEN="<your-jfrog-access-token>"
```

Store these in a local `.env` file (already in `.gitignore`) and source it before running.

## Usage

### npm

```bash
python3 migrate_npm_to_jfrog.py \
  --az-org     <azure-org> \
  --az-project <azure-project> \
  --az-feed    <feed-name> \
  --jfrog-url  https://<instance>.jfrog.io \
  --jfrog-repo <npm-local-repo>
```

### NuGet

```bash
python3 migrate_nuget_to_jfrog.py \
  --az-org     <azure-org> \
  --az-project <azure-project> \
  --az-feed    <feed-name> \
  --jfrog-url  https://<instance>.jfrog.io \
  --jfrog-repo <nuget-local-repo>
```

### Maven

```bash
python3 migrate_maven_to_jfrog.py \
  --az-org     <azure-org> \
  --az-project <azure-project> \
  --az-feed    <feed-name> \
  --jfrog-url  https://<instance>.jfrog.io \
  --jfrog-repo libs-release-local
```

Downloads `.jar` and `.pom` for every version, placing them at
`groupId/artifactId/version/` in the JFrog repo (standard Maven layout).

### Gradle

```bash
python3 migrate_gradle_to_jfrog.py \
  --az-org     <azure-org> \
  --az-project <azure-project> \
  --az-feed    <feed-name> \
  --jfrog-url  https://<instance>.jfrog.io \
  --jfrog-repo gradle-local
```

Same as Maven but also downloads `.module` (Gradle Module Metadata) when
present. Target repo can be a JFrog Gradle or Maven local repository.

### pip

```bash
python3 migrate_pip_to_jfrog.py \
  --az-org     <azure-org> \
  --az-project <azure-project> \
  --az-feed    <feed-name> \
  --jfrog-url  https://<instance>.jfrog.io \
  --jfrog-repo pypi-local
```

Parses the PyPI simple index served by Azure Artifacts to discover actual
distribution filenames (handles arbitrary wheel tags). Downloads all `.whl`,
`.tar.gz`, and `.zip` files for each package version. JFrog auto-generates
the simple index from the uploaded files.

> `--az-project` is required for **project-scoped** Azure feeds (visible as
> `https://dev.azure.com/<org>/<project>/_artifacts/feed/<feed>`).
> Omit it for org-scoped feeds.

## Flags

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--az-org` | Yes | — | Azure DevOps organization name |
| `--az-project` | No | — | Azure DevOps project (project-scoped feeds only) |
| `--az-feed` | Yes | — | Azure Artifacts feed name |
| `--az-pat` | No | env/prompt | Azure PAT — or set `AZURE_PAT` env var |
| `--jfrog-url` | Yes | — | JFrog base URL, e.g. `https://mycompany.jfrog.io` |
| `--jfrog-repo` | Yes | — | Target local repo name in Artifactory |
| `--jfrog-token` | No | env/prompt | JFrog Access Token — or set `JFROG_TOKEN` env var |
| `--output` | No | `./downloads` | Local folder for downloaded files |
| `--latest-only` | No | false | Migrate only the latest version of each package |
| `--since` | No | — | Only migrate versions published **on or after** this date (`YYYY-MM-DD`, UTC, inclusive) |
| `--until` | No | — | Only migrate versions published **on or before** this date (`YYYY-MM-DD`, UTC, inclusive) |
| `--skip-download` | No | false | Skip download; upload files already in `--output` |
| `--skip-upload` | No | false | Download only (dry run) |
| `--clean` | No | false | Delete local files after successful upload |
| `--workers` | No | `4` | Parallel download/upload workers |
| `--retries` | No | `3` | HTTP retry attempts on transient errors |
| `--force` | No | false | Re-upload even if already recorded in the migration manifest |

## Incremental / delta migration

All scripts support safe re-runs out of the box.

### Delta manifest (skip already-uploaded artifacts)

Every successful upload is recorded in `.migrated.txt` inside `--output`. On
the next run the script reads that file and skips anything already listed —
only new or previously-failed artifacts are transferred.

```bash
# First run — migrates everything
python3 migrate_npm_to_jfrog.py ...

# Second run — only transfers artifacts not yet in JFrog
python3 migrate_npm_to_jfrog.py ...

# Force a full re-upload (ignores the manifest)
python3 migrate_npm_to_jfrog.py ... --force
```

### Time-range filtering (`--since` / `--until`)

Use these flags to restrict the migration to package versions published within
a specific date window. Dates must be in **`YYYY-MM-DD`** format (UTC). Both
flags are optional and can be used independently or together.

```bash
# Only versions published in 2024
python3 migrate_npm_to_jfrog.py ... --since 2024-01-01 --until 2024-12-31

# Only versions published on or after a cutoff date (open-ended)
python3 migrate_npm_to_jfrog.py ... --since 2025-06-01

# Combine with --latest-only to get the most recent version within a window
python3 migrate_npm_to_jfrog.py ... --since 2024-01-01 --latest-only
```

The date filter is applied before `--latest-only`, so
`--since 2024-01-01 --latest-only` returns the latest version that was
published on or after 1 Jan 2024 (not necessarily the absolute latest).

Versions that carry no `publishDate` in the Azure API response are always
included regardless of `--since` / `--until`.

## How it works

**Phase 1 — Download** fetches the full package list from Azure Artifacts via
the Packaging REST API and downloads each artifact to a local folder, preserving
the appropriate directory structure. Already-downloaded files are skipped (safe
to re-run). pip additionally queries the PyPI simple index per package to
discover the exact distribution filenames (wheel tags, etc.).

**Phase 2 — Upload** pushes each file to Artifactory via a `PUT` request,
preserving the standard registry path structure for each ecosystem:
- **npm**: `@scope/name/-/name-version.tgz`
- **NuGet**: flat `name.version.nupkg`
- **Maven / Gradle**: `groupId/artifactId/version/artifactId-version.jar`
- **pip**: `package-name/filename` (JFrog auto-generates the PyPI simple index)
