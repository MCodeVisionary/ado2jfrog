"""
Shared utilities for ado2jfrog migrators.

Provides: secret resolution, auth headers, HTTP retry, thread-safe printing,
JFrog connection validation, and MigrationManifest for delta tracking.
"""

import base64
from datetime import date
import getpass
import os
import sys
import time
import threading
import requests


# HTTP status codes that warrant a retry (transient server/gateway errors)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Secret resolution  (CLI flag → env var → interactive prompt)
# ---------------------------------------------------------------------------

def resolve_secret(value, env_var: str, prompt_label: str) -> str:
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
# HTTP with retry
# ---------------------------------------------------------------------------

def request_with_retry(method: str, url: str, *, retries: int = 3, **kwargs):
    """HTTP request with exponential backoff on transient 5xx / 429 responses.

    Connection errors also trigger a retry so brief network blips don't abort
    a long-running migration job.
    """
    for attempt in range(retries):
        try:
            r = requests.request(method, url, **kwargs)
        except requests.ConnectionError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue

        if r.status_code not in _RETRY_STATUSES or attempt == retries - 1:
            return r

        # Honour Retry-After header when present (e.g. Azure rate-limit responses)
        retry_after = int(r.headers.get("Retry-After", 2 ** attempt))
        time.sleep(retry_after)

    return r  # type: ignore[return-value]  — unreachable; satisfies type checkers


# ---------------------------------------------------------------------------
# JFrog connection pre-flight
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


# ---------------------------------------------------------------------------
# Thread-safe console output
# ---------------------------------------------------------------------------

_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    """Print from a parallel worker without interleaving with other workers."""
    with _print_lock:
        print(*args, **kwargs)


# ---------------------------------------------------------------------------
# Migration manifest — persistent delta state
# ---------------------------------------------------------------------------

class MigrationManifest:
    """Tracks which artifact paths have been successfully uploaded to JFrog.

    Stored as a plain-text file (one artifact path per line) inside the
    output directory.  The file is loaded into an in-memory set at startup for
    O(1) delta-check lookups.  Every successful upload appends to the file
    immediately, so a mid-run failure leaves a valid partial state that the
    next run continues from automatically.

    Pass force=True (via --force) to ignore the manifest and re-upload
    everything — useful when the target repo was wiped or you suspect the
    manifest is stale.
    """

    FILENAME = ".migrated.txt"

    def __init__(self, output_dir: str, *, force: bool = False):
        self._path = os.path.join(output_dir, self.FILENAME)
        self._force = force
        self._done: set[str] = set()
        self._lock = threading.Lock()
        if not force:
            self._load()

    def _load(self):
        if os.path.exists(self._path):
            with open(self._path) as fh:
                self._done = {ln.strip() for ln in fh if ln.strip()}
            if self._done:
                print(f"  Manifest: {len(self._done)} artifact(s) already migrated "
                      f"(use --force to re-upload).")

    def already_done(self, artifact_path: str) -> bool:
        if self._force:
            return False
        return artifact_path in self._done

    def record(self, artifact_path: str):
        """Mark an artifact as successfully uploaded (thread-safe, append-only)."""
        with self._lock:
            if artifact_path not in self._done:
                self._done.add(artifact_path)
                with open(self._path, "a") as fh:
                    fh.write(artifact_path + "\n")

    @property
    def count(self) -> int:
        return len(self._done)


# ---------------------------------------------------------------------------
# Version date filtering
# ---------------------------------------------------------------------------

def filter_versions_by_date(versions: list, since: str = None, until: str = None) -> list:
    """Return versions whose publishDate falls within [since, until] (both inclusive).

    since / until must be YYYY-MM-DD strings or None (meaning unbounded).
    Versions that carry no publishDate are always kept.
    """
    if not since and not until:
        return versions

    since_date = date.fromisoformat(since) if since else None
    until_date = date.fromisoformat(until) if until else None

    filtered = []
    for ver in versions:
        pub = ver.get("publishDate", "")
        if not pub:
            filtered.append(ver)
            continue
        try:
            ver_date = date.fromisoformat(pub[:10])
        except ValueError:
            filtered.append(ver)
            continue
        if since_date and ver_date < since_date:
            continue
        if until_date and ver_date > until_date:
            continue
        filtered.append(ver)

    return filtered
