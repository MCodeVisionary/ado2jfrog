"""
Azure Artifacts → JFrog Artifactory npm Migrator
Supports both org-scoped and project-scoped Azure feeds.
Handles scoped packages (@scope/name) transparently.

Requirements:
    pip install requests

Usage:
    python migrate_npm_to_jfrog.py \
        --az-org        myorg \
        --az-project    myproject \
        --az-feed       sharednpm \
        --az-pat        AZURE_PAT \
        --jfrog-url     https://mycompany.jfrog.io \
        --jfrog-repo    npm-local \
        --jfrog-token   JFROG_ACCESS_TOKEN \
        --output        ./downloads

Optional flags:
    --az-project        Azure DevOps project name (required for project-scoped feeds)
    --latest-only       Only migrate the latest version of each package
    --skip-download     Skip download phase (use already-downloaded files in --output)
    --skip-upload       Skip upload phase (dry-run download only)
    --clean             Delete local .tgz files after successful upload
"""

import argparse
import base64
import getpass
import os
import sys
import requests
from pathlib import Path


# ---------------------------------------------------------------------------
# Secret resolution  (CLI flag → env var → interactive prompt)
# ---------------------------------------------------------------------------

def resolve_secret(value: str | None, env_var: str, prompt_label: str) -> str:
    if value:
        return value
    from_env = os.environ.get(env_var)
    if from_env:
        print(f"  (using {env_var} from environment)")
        return from_env
    return getpass.getpass(f"{prompt_label}: ")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def azure_headers(pat: str) -> dict:
    token = base64.b64encode(f":{pat}".encode("ascii")).decode("ascii")
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def jfrog_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Phase 1 — Download from Azure Artifacts
# ---------------------------------------------------------------------------

def get_all_packages(org: str, feed: str, headers: dict, project: str = None) -> list:
    packages = []
    skip, top = 0, 100

    print(f"\n{'='*60}")
    print(f"PHASE 1: Downloading from Azure Artifacts")
    print(f"  Org  : {org}")
    if project:
        print(f"  Proj : {project}")
    print(f"  Feed : {feed}")
    print(f"{'='*60}")
    print("Fetching package list...")

    base = f"https://feeds.dev.azure.com/{org}/{project}/_apis" if project else f"https://feeds.dev.azure.com/{org}/_apis"

    while True:
        url = (
            f"{base}/packaging/feeds/{feed}/packages"
            f"?protocolType=npm&api-version=7.1&$top={top}&$skip={skip}&includeAllVersions=true"
        )
        r = requests.get(url, headers=headers)

        if r.status_code == 401:
            print("ERROR: Azure auth failed. Check your PAT has 'Packaging (read)' scope.")
            sys.exit(1)
        if r.status_code == 404:
            print(f"ERROR: Feed '{feed}' not found in org '{org}'" + (f", project '{project}'" if project else "") + ".")
            sys.exit(1)
        r.raise_for_status()

        batch = r.json().get("value", [])
        packages.extend(batch)
        print(f"  Retrieved {len(packages)} packages so far...")

        if len(batch) < top:
            break
        skip += top

    return packages


def _parse_npm_name(name: str):
    """Split a package name into (scope_or_None, basename).

    '@scope/pkg' → ('scope', 'pkg')
    'pkg'        → (None,    'pkg')
    """
    if name.startswith("@") and "/" in name:
        scope, basename = name[1:].split("/", 1)
        return scope, basename
    return None, name


def download_tgz(org: str, feed: str, name: str, version: str, output_dir: str, headers: dict, project: str = None) -> str | None:
    """Download a .tgz tarball and return its local filepath, or None on failure."""
    scope, basename = _parse_npm_name(name)
    pkg_prefix = f"{org}/{project}" if project else org

    if scope:
        url = (
            f"https://pkgs.dev.azure.com/{pkg_prefix}/_packaging/{feed}/npm/registry"
            f"/@{scope}/{basename}/-/{basename}-{version}.tgz"
        )
        pkg_dir = os.path.join(output_dir, f"@{scope}", basename, version)
    else:
        url = (
            f"https://pkgs.dev.azure.com/{pkg_prefix}/_packaging/{feed}/npm/registry"
            f"/{basename}/-/{basename}-{version}.tgz"
        )
        pkg_dir = os.path.join(output_dir, basename, version)

    os.makedirs(pkg_dir, exist_ok=True)
    filename = f"{basename}-{version}.tgz"
    filepath = os.path.join(pkg_dir, filename)

    if os.path.exists(filepath):
        print(f"  [CACHED] {name}@{version}")
        return filepath

    dl_headers = {**headers, "Accept": "application/octet-stream"}
    r = requests.get(url, headers=dl_headers, stream=True)

    if r.status_code == 404:
        print(f"  [WARN]   {name}@{version} — not found on Azure, skipping.")
        return None
    if not r.ok:
        print(f"  [WARN]   {name}@{version} — HTTP {r.status_code}, skipping.")
        return None

    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)

    size_kb = os.path.getsize(filepath) / 1024
    print(f"  [OK]     {name}@{version} ({size_kb:.1f} KB)")
    return filepath


def run_download(args, az_hdrs) -> list[tuple[str, str]]:
    """Returns list of (local_filepath, package_name) tuples."""
    project = getattr(args, "az_project", None)
    packages = get_all_packages(args.az_org, args.az_feed, az_hdrs, project)

    if not packages:
        print("No packages found in the feed.")
        sys.exit(0)

    print(f"\nFound {len(packages)} package(s). Downloading...\n")

    local_files = []
    for pkg in packages:
        name = pkg.get("name", "")
        versions = pkg.get("versions", [])

        if not versions:
            continue

        if args.latest_only:
            versions = [v for v in versions if v.get("isLatest")] or [versions[0]]

        print(f"Package: {name} ({len(versions)} version(s))")
        for ver in versions:
            version_str = ver.get("version", "")
            if not version_str:
                continue
            filepath = download_tgz(args.az_org, args.az_feed, name, version_str, args.output, az_hdrs, project)
            if filepath:
                local_files.append((filepath, name))

    return local_files


# ---------------------------------------------------------------------------
# Phase 2 — Upload to JFrog Artifactory
# ---------------------------------------------------------------------------

def check_jfrog_connection(base_url: str, repo: str, headers: dict):
    url = f"{base_url.rstrip('/')}/artifactory/api/storage/{repo}"
    r = requests.get(url, headers=headers)
    if r.status_code == 401:
        print("ERROR: JFrog auth failed. Check your access token.")
        sys.exit(1)
    if r.status_code == 404:
        print(f"ERROR: JFrog repo '{repo}' not found at {base_url}.")
        sys.exit(1)
    if not r.ok:
        print(f"ERROR: JFrog connectivity check failed — HTTP {r.status_code}.")
        sys.exit(1)
    print("  JFrog connection OK.")


def upload_tgz(base_url: str, repo: str, filepath: str, pkg_name: str, headers: dict) -> bool:
    """Upload a .tgz to JFrog, preserving the npm registry path structure."""
    scope, basename = _parse_npm_name(pkg_name)
    filename = os.path.basename(filepath)

    if scope:
        upload_path = f"{repo}/@{scope}/{basename}/-/{filename}"
    else:
        upload_path = f"{repo}/{basename}/-/{filename}"

    upload_url = f"{base_url.rstrip('/')}/artifactory/{upload_path}"

    with open(filepath, "rb") as f:
        r = requests.put(
            upload_url,
            headers={**headers, "Content-Type": "application/octet-stream"},
            data=f,
        )

    if r.status_code in (200, 201):
        print(f"  [UPLOADED] {pkg_name} → {upload_path}")
        return True
    else:
        print(f"  [FAILED]   {pkg_name} — HTTP {r.status_code}: {r.text[:120]}")
        return False


def run_upload(local_files: list[tuple[str, str]], args, jfrog_hdrs) -> tuple[int, int]:
    print(f"\n{'='*60}")
    print(f"PHASE 2: Uploading to JFrog Artifactory")
    print(f"  URL  : {args.jfrog_url}")
    print(f"  Repo : {args.jfrog_repo}")
    print(f"{'='*60}")

    check_jfrog_connection(args.jfrog_url, args.jfrog_repo, jfrog_hdrs)
    print(f"\nUploading {len(local_files)} file(s)...\n")

    succeeded, failed = 0, 0
    for filepath, pkg_name in local_files:
        ok = upload_tgz(args.jfrog_url, args.jfrog_repo, filepath, pkg_name, jfrog_hdrs)
        if ok:
            succeeded += 1
            if args.clean:
                os.remove(filepath)
        else:
            failed += 1

    return succeeded, failed


# ---------------------------------------------------------------------------
# Collect already-downloaded files (for --skip-download mode)
# ---------------------------------------------------------------------------

def collect_local_tarballs(output_dir: str) -> list[tuple[str, str]]:
    """Return (filepath, inferred_pkg_name) tuples for all .tgz under output_dir."""
    results = []
    for p in Path(output_dir).rglob("*.tgz"):
        parts = p.relative_to(output_dir).parts
        # Layout: @scope/name/version/name-version.tgz  or  name/version/name-version.tgz
        if len(parts) >= 2 and parts[0].startswith("@"):
            pkg_name = f"{parts[0]}/{parts[1]}"
        else:
            pkg_name = parts[0]
        results.append((str(p), pkg_name))
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Migrate npm packages from Azure Artifacts to JFrog Artifactory."
    )

    # Azure args
    parser.add_argument("--az-org",     required=True,  help="Azure DevOps organization name")
    parser.add_argument("--az-project", default=None,   help="Azure DevOps project name (required for project-scoped feeds)")
    parser.add_argument("--az-feed",    required=True,  help="Azure Artifacts feed name")
    parser.add_argument("--az-pat",     default=None,   help="Azure PAT — or set AZURE_PAT env var")

    # JFrog args
    parser.add_argument("--jfrog-url",   required=True, help="JFrog base URL, e.g. https://mycompany.jfrog.io")
    parser.add_argument("--jfrog-repo",  required=True, help="JFrog npm local repo name, e.g. npm-local")
    parser.add_argument("--jfrog-token", default=None,  help="JFrog Access Token — or set JFROG_TOKEN env var")

    # Options
    parser.add_argument("--output",        default="./downloads", help="Local folder for .tgz files (default: ./downloads)")
    parser.add_argument("--latest-only",   action="store_true",   help="Only migrate the latest version of each package")
    parser.add_argument("--skip-download", action="store_true",   help="Skip download; upload files already in --output")
    parser.add_argument("--skip-upload",   action="store_true",   help="Skip upload (download only / dry run)")
    parser.add_argument("--clean",         action="store_true",   help="Delete local .tgz files after successful upload")

    args = parser.parse_args()
    os.makedirs(args.output, exist_ok=True)

    az_pat      = resolve_secret(args.az_pat,      "AZURE_PAT",   "Azure PAT")
    jfrog_token = resolve_secret(args.jfrog_token, "JFROG_TOKEN", "JFrog Access Token")

    az_hdrs    = azure_headers(az_pat)
    jfrog_hdrs = jfrog_headers(jfrog_token)

    # --- Download phase ---
    if args.skip_download:
        print("Skipping download phase. Scanning local output folder for .tgz files...")
        local_files = collect_local_tarballs(args.output)
        print(f"  Found {len(local_files)} local .tgz file(s).")
    else:
        local_files = run_download(args, az_hdrs)

    if not local_files:
        print("\nNo .tgz files to upload. Exiting.")
        sys.exit(0)

    # --- Upload phase ---
    if args.skip_upload:
        print("\nSkipping upload phase (--skip-upload flag set).")
        print(f"Downloaded {len(local_files)} file(s) to {os.path.abspath(args.output)}")
        sys.exit(0)

    upload_succeeded, upload_failed = run_upload(local_files, args, jfrog_hdrs)

    # --- Summary ---
    print(f"\n{'='*60}")
    print("MIGRATION COMPLETE")
    print(f"  Packages processed : {len(local_files)}")
    print(f"  Uploaded           : {upload_succeeded}")
    print(f"  Failed             : {upload_failed}")
    print(f"  Local folder       : {os.path.abspath(args.output)}")
    if args.clean and upload_succeeded:
        print(f"  Local files cleaned up after upload.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
