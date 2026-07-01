"""
Azure Artifacts → JFrog Artifactory npm Migrator
Supports both org-scoped and project-scoped Azure feeds.
Handles scoped packages (@scope/name) transparently.

Features:
  - Parallel downloads and uploads (--workers)
  - Delta migration: skips artifacts already in JFrog via a local manifest
  - Automatic retry with exponential backoff on transient errors (--retries)
  - --force to re-upload everything regardless of manifest state

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
    --since YYYY-MM-DD  Only migrate versions published on or after this date (UTC)
    --until YYYY-MM-DD  Only migrate versions published on or before this date (UTC)
    --skip-download     Skip download phase (use already-downloaded files in --output)
    --skip-upload       Skip upload phase (dry-run download only)
    --clean             Delete local .tgz files after successful upload
    --workers N         Parallel download/upload workers (default: 4)
    --retries N         HTTP retry attempts on transient errors (default: 3)
    --force             Re-upload even if artifact is recorded in the migration manifest
"""

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from _common import (
    azure_headers, check_jfrog_connection, filter_versions_by_date,
    jfrog_headers, MigrationManifest, request_with_retry, resolve_secret, safe_print,
)


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

    base = (f"https://feeds.dev.azure.com/{org}/{project}/_apis"
            if project else f"https://feeds.dev.azure.com/{org}/_apis")

    while True:
        url = (f"{base}/packaging/feeds/{feed}/packages"
               f"?protocolType=npm&api-version=7.1&$top={top}&$skip={skip}&includeAllVersions=true")
        r = request_with_retry("GET", url, headers=headers)

        if r.status_code == 401:
            print("ERROR: Azure auth failed. Check your PAT has 'Packaging (read)' scope.")
            sys.exit(1)
        if r.status_code == 404:
            print(f"ERROR: Feed '{feed}' not found in org '{org}'"
                  + (f", project '{project}'" if project else "") + ".")
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


def download_tgz(org: str, feed: str, name: str, version: str, output_dir: str,
                 headers: dict, project: str = None, retries: int = 3) -> str | None:
    """Download a .tgz tarball and return its local filepath, or None on failure."""
    scope, basename = _parse_npm_name(name)
    pkg_prefix = f"{org}/{project}" if project else org

    if scope:
        url = (f"https://pkgs.dev.azure.com/{pkg_prefix}/_packaging/{feed}/npm/registry"
               f"/@{scope}/{basename}/-/{basename}-{version}.tgz")
        pkg_dir = os.path.join(output_dir, f"@{scope}", basename, version)
    else:
        url = (f"https://pkgs.dev.azure.com/{pkg_prefix}/_packaging/{feed}/npm/registry"
               f"/{basename}/-/{basename}-{version}.tgz")
        pkg_dir = os.path.join(output_dir, basename, version)

    os.makedirs(pkg_dir, exist_ok=True)
    filename = f"{basename}-{version}.tgz"
    filepath = os.path.join(pkg_dir, filename)

    if os.path.exists(filepath):
        safe_print(f"  [CACHED] {name}@{version}")
        return filepath

    dl_headers = {**headers, "Accept": "application/octet-stream"}
    r = request_with_retry("GET", url, retries=retries, headers=dl_headers, stream=True)

    if r.status_code == 404:
        safe_print(f"  [WARN]   {name}@{version} — not found on Azure, skipping.")
        return None
    if not r.ok:
        safe_print(f"  [WARN]   {name}@{version} — HTTP {r.status_code}, skipping.")
        return None

    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)

    size_kb = os.path.getsize(filepath) / 1024
    safe_print(f"  [OK]     {name}@{version} ({size_kb:.1f} KB)")
    return filepath


def run_download(args, az_hdrs) -> list[tuple[str, str]]:
    """Returns list of (local_filepath, package_name) tuples."""
    project = getattr(args, "az_project", None)
    packages = get_all_packages(args.az_org, args.az_feed, az_hdrs, project)

    if not packages:
        print("No packages found in the feed.")
        sys.exit(0)

    # Flatten packages → (name, version) tasks
    tasks: list[tuple[str, str]] = []
    for pkg in packages:
        name = pkg.get("name", "")
        versions = pkg.get("versions", [])
        if not versions:
            continue
        versions = filter_versions_by_date(versions, args.since, args.until)
        if not versions:
            continue
        if args.latest_only:
            versions = [v for v in versions if v.get("isLatest")] or [versions[0]]
        for ver in versions:
            version_str = ver.get("version", "")
            if version_str:
                tasks.append((name, version_str))

    total = len(tasks)
    print(f"\nFound {len(packages)} package(s), {total} version(s). "
          f"Downloading with {args.workers} worker(s)...\n")

    local_files: list[tuple[str, str]] = []
    collect_lock = threading.Lock()

    def download_one(name: str, version: str):
        try:
            fp = download_tgz(args.az_org, args.az_feed, name, version,
                              args.output, az_hdrs, project, args.retries)
            if fp:
                with collect_lock:
                    local_files.append((fp, name))
        except Exception as exc:
            safe_print(f"  [ERROR]  {name}@{version}: {exc}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download_one, name, ver) for name, ver in tasks]
        for f in as_completed(futures):
            f.result()

    return local_files


# ---------------------------------------------------------------------------
# Phase 2 — Upload to JFrog Artifactory
# ---------------------------------------------------------------------------

def _npm_artifact_path(pkg_name: str, filename: str) -> str:
    scope, basename = _parse_npm_name(pkg_name)
    if scope:
        return f"@{scope}/{basename}/-/{filename}"
    return f"{basename}/-/{filename}"


def upload_tgz(base_url: str, repo: str, filepath: str, pkg_name: str,
               headers: dict, retries: int = 3) -> bool:
    """PUT a .tgz to JFrog. Returns True on success."""
    filename = os.path.basename(filepath)
    artifact_path = _npm_artifact_path(pkg_name, filename)
    upload_url = f"{base_url.rstrip('/')}/artifactory/{repo}/{artifact_path}"

    with open(filepath, "rb") as f:
        r = request_with_retry(
            "PUT", upload_url, retries=retries,
            headers={**headers, "Content-Type": "application/octet-stream"},
            data=f,
        )

    return r.status_code in (200, 201)


def run_upload(local_files: list[tuple[str, str]], args, jfrog_hdrs,
               manifest: MigrationManifest) -> tuple[int, int, int]:
    """Returns (succeeded, skipped, failed)."""
    print(f"\n{'='*60}")
    print(f"PHASE 2: Uploading to JFrog Artifactory")
    print(f"  URL     : {args.jfrog_url}")
    print(f"  Repo    : {args.jfrog_repo}")
    print(f"  Workers : {args.workers}")
    print(f"{'='*60}")

    check_jfrog_connection(args.jfrog_url, args.jfrog_repo, jfrog_hdrs)

    total = len(local_files)
    print(f"\nUploading {total} file(s)...\n")

    counter = [0]
    counter_lock = threading.Lock()
    results: list[str] = []
    results_lock = threading.Lock()

    def upload_one(filepath: str, pkg_name: str):
        filename = os.path.basename(filepath)
        artifact_path = _npm_artifact_path(pkg_name, filename)

        with counter_lock:
            counter[0] += 1
            n = counter[0]

        if manifest.already_done(artifact_path):
            safe_print(f"  [{n}/{total}] [SKIP]     {artifact_path}")
            with results_lock:
                results.append("skipped")
            return

        ok = upload_tgz(args.jfrog_url, args.jfrog_repo, filepath, pkg_name,
                        jfrog_hdrs, args.retries)
        if ok:
            manifest.record(artifact_path)
            if args.clean:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            safe_print(f"  [{n}/{total}] [UPLOADED] {artifact_path}")
        else:
            safe_print(f"  [{n}/{total}] [FAILED]   {artifact_path}")

        with results_lock:
            results.append("ok" if ok else "failed")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(upload_one, fp, pkg) for fp, pkg in local_files]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as exc:
                safe_print(f"  [ERROR] {exc}")
                with results_lock:
                    results.append("failed")

    return results.count("ok"), results.count("skipped"), results.count("failed")


# ---------------------------------------------------------------------------
# Collect already-downloaded files (for --skip-download mode)
# ---------------------------------------------------------------------------

def collect_local_tarballs(output_dir: str) -> list[tuple[str, str]]:
    """Return (filepath, inferred_pkg_name) tuples for all .tgz under output_dir."""
    results = []
    for p in Path(output_dir).rglob("*.tgz"):
        parts = p.relative_to(output_dir).parts
        pkg_name = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 and parts[0].startswith("@") else parts[0]
        results.append((str(p), pkg_name))
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Migrate npm packages from Azure Artifacts to JFrog Artifactory."
    )

    parser.add_argument("--az-org",     required=True,  help="Azure DevOps organization name")
    parser.add_argument("--az-project", default=None,   help="Azure DevOps project name (required for project-scoped feeds)")
    parser.add_argument("--az-feed",    required=True,  help="Azure Artifacts feed name")
    parser.add_argument("--az-pat",     default=None,   help="Azure PAT — or set AZURE_PAT env var")

    parser.add_argument("--jfrog-url",   required=True, help="JFrog base URL, e.g. https://mycompany.jfrog.io")
    parser.add_argument("--jfrog-repo",  required=True, help="JFrog npm local repo name, e.g. npm-local")
    parser.add_argument("--jfrog-token", default=None,  help="JFrog Access Token — or set JFROG_TOKEN env var")

    parser.add_argument("--output",        default="./downloads", help="Local folder for .tgz files (default: ./downloads)")
    parser.add_argument("--latest-only",   action="store_true",   help="Only migrate the latest version of each package")
    parser.add_argument("--since",         default=None,          metavar="YYYY-MM-DD", help="Only migrate versions published on or after this date (UTC, inclusive)")
    parser.add_argument("--until",         default=None,          metavar="YYYY-MM-DD", help="Only migrate versions published on or before this date (UTC, inclusive)")
    parser.add_argument("--skip-download", action="store_true",   help="Skip download; upload files already in --output")
    parser.add_argument("--skip-upload",   action="store_true",   help="Skip upload (download only / dry run)")
    parser.add_argument("--clean",         action="store_true",   help="Delete local .tgz files after successful upload")
    parser.add_argument("--workers",       type=int, default=4,   help="Parallel download/upload workers (default: 4)")
    parser.add_argument("--retries",       type=int, default=3,   help="HTTP retry attempts on transient errors (default: 3)")
    parser.add_argument("--force",         action="store_true",   help="Re-upload even if already recorded in migration manifest")

    args = parser.parse_args()
    os.makedirs(args.output, exist_ok=True)

    az_pat      = resolve_secret(args.az_pat,      "AZURE_PAT",   "Azure PAT")
    jfrog_token = resolve_secret(args.jfrog_token, "JFROG_TOKEN", "JFrog Access Token")

    az_hdrs    = azure_headers(az_pat)
    jfrog_hdrs = jfrog_headers(jfrog_token)
    manifest   = MigrationManifest(args.output, force=args.force)

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

    upload_succeeded, upload_skipped, upload_failed = run_upload(local_files, args, jfrog_hdrs, manifest)

    # --- Summary ---
    print(f"\n{'='*60}")
    print("MIGRATION COMPLETE")
    print(f"  Packages processed : {len(local_files)}")
    print(f"  Uploaded           : {upload_succeeded}")
    print(f"  Skipped (manifest) : {upload_skipped}")
    print(f"  Failed             : {upload_failed}")
    print(f"  Local folder       : {os.path.abspath(args.output)}")
    if args.clean and upload_succeeded:
        print(f"  Local files cleaned up after upload.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
