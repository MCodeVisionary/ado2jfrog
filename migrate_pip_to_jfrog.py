"""
Azure Artifacts → JFrog Artifactory pip/PyPI Migrator
Supports both org-scoped and project-scoped Azure feeds.
Downloads all distribution files (.whl, .tar.gz) for each package version
by parsing the PyPI simple index served by Azure Artifacts.

Features:
  - Parallel simple-index fetches and file downloads (--workers)
  - Parallel uploads (--workers)
  - Delta migration: skips artifacts already in JFrog via a local manifest
  - Automatic retry with exponential backoff on transient errors (--retries)
  - --force to re-upload everything regardless of manifest state

Requirements:
    pip install requests

Usage:
    python migrate_pip_to_jfrog.py \
        --az-org        myorg \
        --az-project    myproject \
        --az-feed       myfeed \
        --az-pat        AZURE_PAT \
        --jfrog-url     https://mycompany.jfrog.io \
        --jfrog-repo    pypi-local \
        --jfrog-token   JFROG_ACCESS_TOKEN \
        --output        ./downloads

Optional flags:
    --az-project        Azure DevOps project name (required for project-scoped feeds)
    --latest-only       Only migrate the latest version of each package
    --since YYYY-MM-DD  Only migrate versions published on or after this date (UTC)
    --until YYYY-MM-DD  Only migrate versions published on or before this date (UTC)
    --skip-download     Skip download phase (use already-downloaded files in --output)
    --skip-upload       Skip upload phase (dry-run download only)
    --clean             Delete local files after successful upload
    --workers N         Parallel download/upload workers (default: 4)
    --retries N         HTTP retry attempts on transient errors (default: 3)
    --force             Re-upload even if artifact is recorded in the migration manifest
"""

import argparse
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

from _common import (
    azure_headers, check_jfrog_connection, filter_versions_by_date,
    jfrog_headers, MigrationManifest, request_with_retry, resolve_secret, safe_print,
)

_DIST_EXTENSIONS = (".whl", ".tar.gz", ".zip", ".egg")


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
               f"?protocolType=pypi&api-version=7.1&$top={top}&$skip={skip}&includeAllVersions=true")
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


def _file_matches_version(filename: str, version: str) -> bool:
    """Return True if filename belongs to the given version.

    Wheels:  name-VERSION-pythontag-abi-platform.whl  → version flanked by dashes
    Sdists:  name-VERSION.tar.gz / .zip / .egg        → version immediately before ext
    """
    if f"-{version}-" in filename:
        return True
    for ext in (".tar.gz", ".zip", ".egg"):
        if filename.endswith(f"-{version}{ext}"):
            return True
    return False


def get_package_files(org: str, feed: str, name: str, headers: dict,
                      project: str = None, retries: int = 3) -> list[tuple[str, str]]:
    """Query the PyPI simple index and return (filename, download_url) for all files."""
    pkg_prefix = f"{org}/{project}" if project else org
    base_url = (f"https://pkgs.dev.azure.com/{pkg_prefix}/_packaging/{feed}"
                f"/pypi/simple/{name.lower()}/")

    html_headers = {**headers, "Accept": "text/html"}
    r = request_with_retry("GET", base_url, retries=retries, headers=html_headers)

    if r.status_code == 404:
        return []
    if not r.ok:
        safe_print(f"  [WARN]   Could not fetch simple index for '{name}' — HTTP {r.status_code}.")
        return []

    files = []
    for href in re.findall(r'href="([^"]+)"', r.text):
        clean_href = href.split("#")[0]
        abs_url = urljoin(base_url, clean_href)
        filename = abs_url.rstrip("/").split("/")[-1]
        if any(filename.endswith(ext) for ext in _DIST_EXTENSIONS):
            files.append((filename, abs_url))

    return files


def download_file(name: str, version: str, filename: str, url: str,
                  output_dir: str, headers: dict, retries: int = 3) -> str | None:
    """Download a single distribution file and return its local path, or None on failure."""
    pkg_dir = os.path.join(output_dir, name.lower(), version)
    os.makedirs(pkg_dir, exist_ok=True)
    filepath = os.path.join(pkg_dir, filename)

    if os.path.exists(filepath):
        safe_print(f"  [CACHED] {filename}")
        return filepath

    dl_headers = {**headers, "Accept": "application/octet-stream"}
    r = request_with_retry("GET", url, retries=retries, headers=dl_headers, stream=True)

    if r.status_code == 404:
        safe_print(f"  [WARN]   {filename} — not found on Azure, skipping.")
        return None
    if not r.ok:
        safe_print(f"  [WARN]   {filename} — HTTP {r.status_code}, skipping.")
        return None

    with open(filepath, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)

    size_kb = os.path.getsize(filepath) / 1024
    safe_print(f"  [OK]     {filename} ({size_kb:.1f} KB)")
    return filepath


def run_download(args, az_hdrs) -> list[tuple[str, str]]:
    """Returns list of (local_filepath, package_name) tuples."""
    project = getattr(args, "az_project", None)
    packages = get_all_packages(args.az_org, args.az_feed, az_hdrs, project)

    if not packages:
        print("No packages found in the feed.")
        sys.exit(0)

    print(f"\nFound {len(packages)} package(s). "
          f"Fetching simple indexes with {args.workers} worker(s)...\n")

    # --- Step A: fetch all simple indexes in parallel (one per package) ---
    pkg_file_index: dict[str, list[tuple[str, str]]] = {}
    index_lock = threading.Lock()

    def fetch_index(name: str):
        files = get_package_files(args.az_org, args.az_feed, name, az_hdrs, project, args.retries)
        with index_lock:
            pkg_file_index[name] = files

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_index, pkg.get("name", "")) for pkg in packages]
        for f in as_completed(futures):
            f.result()

    # --- Step B: build per-version download tasks ---
    tasks: list[tuple[str, str, str, str]] = []  # (name, version, filename, url)
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

        all_files = pkg_file_index.get(name, [])
        if not all_files:
            safe_print(f"  [WARN]   {name} — no downloadable files in simple index, skipping.")
            continue

        for ver in versions:
            version_str = ver.get("version", "")
            if not version_str:
                continue
            version_files = [(fn, url) for fn, url in all_files
                             if _file_matches_version(fn, version_str)]
            if not version_files:
                safe_print(f"  [WARN]   {name}=={version_str} — no matching files in simple index.")
                continue
            for filename, url in version_files:
                tasks.append((name, version_str, filename, url))

    total = len(tasks)
    print(f"\nFound {total} distribution file(s). Downloading with {args.workers} worker(s)...\n")

    local_files: list[tuple[str, str]] = []
    collect_lock = threading.Lock()

    def download_one(name: str, version: str, filename: str, url: str):
        try:
            fp = download_file(name, version, filename, url, args.output, az_hdrs, args.retries)
            if fp:
                with collect_lock:
                    local_files.append((fp, name))
        except Exception as exc:
            safe_print(f"  [ERROR]  {filename}: {exc}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(download_one, n, v, fn, u) for n, v, fn, u in tasks]
        for f in as_completed(futures):
            f.result()

    return local_files


# ---------------------------------------------------------------------------
# Phase 2 — Upload to JFrog Artifactory
# ---------------------------------------------------------------------------

def upload_file(base_url: str, repo: str, filepath: str, pkg_name: str,
                headers: dict, retries: int = 3) -> bool:
    """PUT a distribution file to JFrog under {repo}/{pkg_name}/{filename}.

    JFrog auto-generates the PyPI simple index from uploaded files.
    """
    filename = os.path.basename(filepath)
    upload_url = f"{base_url.rstrip('/')}/artifactory/{repo}/{pkg_name.lower()}/{filename}"

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
        artifact_path = f"{pkg_name.lower()}/{filename}"

        with counter_lock:
            counter[0] += 1
            n = counter[0]

        if manifest.already_done(artifact_path):
            safe_print(f"  [{n}/{total}] [SKIP]     {artifact_path}")
            with results_lock:
                results.append("skipped")
            return

        ok = upload_file(args.jfrog_url, args.jfrog_repo, filepath, pkg_name,
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

def collect_local_packages(output_dir: str) -> list[tuple[str, str]]:
    """Return (filepath, pkg_name) tuples for all distribution files under output_dir.

    Expected layout: {output_dir}/{pkg_name}/{version}/{filename}
    """
    results = []
    for p in Path(output_dir).rglob("*"):
        if not p.is_file():
            continue
        if not any(p.name.endswith(ext) for ext in _DIST_EXTENSIONS):
            continue
        parts = p.relative_to(output_dir).parts
        pkg_name = parts[0] if len(parts) > 1 else p.stem
        results.append((str(p), pkg_name))
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Migrate pip/PyPI packages from Azure Artifacts to JFrog Artifactory."
    )

    parser.add_argument("--az-org",     required=True,  help="Azure DevOps organization name")
    parser.add_argument("--az-project", default=None,   help="Azure DevOps project name (required for project-scoped feeds)")
    parser.add_argument("--az-feed",    required=True,  help="Azure Artifacts feed name")
    parser.add_argument("--az-pat",     default=None,   help="Azure PAT — or set AZURE_PAT env var")

    parser.add_argument("--jfrog-url",   required=True, help="JFrog base URL, e.g. https://mycompany.jfrog.io")
    parser.add_argument("--jfrog-repo",  required=True, help="JFrog PyPI local repo name, e.g. pypi-local")
    parser.add_argument("--jfrog-token", default=None,  help="JFrog Access Token — or set JFROG_TOKEN env var")

    parser.add_argument("--output",        default="./downloads", help="Local folder for downloaded files (default: ./downloads)")
    parser.add_argument("--latest-only",   action="store_true",   help="Only migrate the latest version of each package")
    parser.add_argument("--since",         default=None,          metavar="YYYY-MM-DD", help="Only migrate versions published on or after this date (UTC, inclusive)")
    parser.add_argument("--until",         default=None,          metavar="YYYY-MM-DD", help="Only migrate versions published on or before this date (UTC, inclusive)")
    parser.add_argument("--skip-download", action="store_true",   help="Skip download; upload files already in --output")
    parser.add_argument("--skip-upload",   action="store_true",   help="Skip upload (download only / dry run)")
    parser.add_argument("--clean",         action="store_true",   help="Delete local files after successful upload")
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
        print("Skipping download phase. Scanning local output folder for Python distributions...")
        local_files = collect_local_packages(args.output)
        print(f"  Found {len(local_files)} local distribution file(s).")
    else:
        local_files = run_download(args, az_hdrs)

    if not local_files:
        print("\nNo distribution files to upload. Exiting.")
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
