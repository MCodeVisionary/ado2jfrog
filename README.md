# ado2jfrog — Azure Artifacts → JFrog Artifactory Migration

Two scripts to migrate packages from Azure Artifacts to JFrog Artifactory.
Both support **org-scoped and project-scoped** Azure feeds and handle all
package versions (or latest-only with a flag).

| Script | Package type | Azure format | JFrog format |
|--------|-------------|--------------|--------------|
| `migrate_npm_to_jfrog.py` | npm (`.tgz`) | npm registry | npm local repo |
| `migrate_nuget_to_jfrog.py` | NuGet (`.nupkg`) | NuGet v3 flat | NuGet local repo |

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
| `--skip-download` | No | false | Skip download; upload files already in `--output` |
| `--skip-upload` | No | false | Download only (dry run) |
| `--clean` | No | false | Delete local files after successful upload |

## How it works

**Phase 1 — Download** fetches the full package list from Azure Artifacts via
the Packaging REST API and downloads each `.tgz` / `.nupkg` to a local folder,
preserving `name/version/` directory structure. Already-downloaded files are
skipped (safe to re-run).

**Phase 2 — Upload** pushes each file to Artifactory via a `PUT` request,
preserving the standard registry path structure (`@scope/name/-/name-version.tgz`
for scoped npm packages).
