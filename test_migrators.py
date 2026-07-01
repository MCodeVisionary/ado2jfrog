"""
Unit tests for all ado2jfrog migration scripts.

Run with:
    python3 -m pytest test_migrators.py -v
"""

import base64
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.dirname(__file__))


# ===========================================================================
# _common.py
# ===========================================================================

class TestAzureHeaders(unittest.TestCase):
    def test_basic_encoding(self):
        from _common import azure_headers
        hdrs = azure_headers("mytoken")
        expected = base64.b64encode(b":mytoken").decode("ascii")
        self.assertEqual(hdrs["Authorization"], f"Basic {expected}")
        self.assertEqual(hdrs["Accept"], "application/json")

    def test_empty_pat_encodes(self):
        from _common import azure_headers
        hdrs = azure_headers("")
        self.assertIn("Basic", hdrs["Authorization"])


class TestJFrogHeaders(unittest.TestCase):
    def test_bearer_token(self):
        from _common import jfrog_headers
        hdrs = jfrog_headers("tok123")
        self.assertEqual(hdrs["Authorization"], "Bearer tok123")


class TestResolveSecret(unittest.TestCase):
    def test_returns_value_when_provided(self):
        from _common import resolve_secret
        result = resolve_secret("explicit", "SOME_ENV", "Prompt")
        self.assertEqual(result, "explicit")

    def test_falls_back_to_env_var(self):
        from _common import resolve_secret
        with patch.dict(os.environ, {"MY_SECRET": "env_value"}):
            result = resolve_secret(None, "MY_SECRET", "Prompt")
        self.assertEqual(result, "env_value")

    def test_prompts_when_no_value_or_env(self):
        from _common import resolve_secret
        env = {k: v for k, v in os.environ.items() if k != "MISSING_VAR"}
        with patch.dict(os.environ, env, clear=True):
            with patch("getpass.getpass", return_value="prompted_val") as mock_gp:
                result = resolve_secret(None, "MISSING_VAR", "Enter secret")
        mock_gp.assert_called_once_with("Enter secret: ")
        self.assertEqual(result, "prompted_val")


class TestRequestWithRetry(unittest.TestCase):
    def test_returns_immediately_on_200(self):
        from _common import request_with_retry
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.request", return_value=mock_resp) as mock_req:
            result = request_with_retry("GET", "https://example.com", retries=3)
        self.assertEqual(result, mock_resp)
        mock_req.assert_called_once()

    def test_retries_on_500_then_succeeds(self):
        from _common import request_with_retry
        fail = MagicMock(); fail.status_code = 500; fail.headers = {}
        ok   = MagicMock(); ok.status_code   = 200; ok.headers = {}
        with patch("requests.request", side_effect=[fail, ok]):
            with patch("time.sleep"):
                result = request_with_retry("GET", "https://example.com", retries=3)
        self.assertEqual(result.status_code, 200)

    def test_returns_last_response_after_all_retries_exhausted(self):
        from _common import request_with_retry
        fail = MagicMock(); fail.status_code = 503; fail.headers = {}
        with patch("requests.request", return_value=fail):
            with patch("time.sleep"):
                result = request_with_retry("GET", "https://example.com", retries=3)
        self.assertEqual(result.status_code, 503)

    def test_retries_on_connection_error(self):
        from _common import request_with_retry
        ok = MagicMock(); ok.status_code = 200; ok.headers = {}
        import requests as req_lib
        with patch("requests.request", side_effect=[req_lib.ConnectionError, ok]):
            with patch("time.sleep"):
                result = request_with_retry("GET", "https://example.com", retries=3)
        self.assertEqual(result.status_code, 200)

    def test_raises_connection_error_after_all_retries(self):
        from _common import request_with_retry
        import requests as req_lib
        with patch("requests.request", side_effect=req_lib.ConnectionError):
            with patch("time.sleep"):
                with self.assertRaises(req_lib.ConnectionError):
                    request_with_retry("GET", "https://example.com", retries=3)

    def test_honours_retry_after_header(self):
        from _common import request_with_retry
        fail = MagicMock(); fail.status_code = 429; fail.headers = {"Retry-After": "5"}
        ok   = MagicMock(); ok.status_code   = 200; ok.headers = {}
        with patch("requests.request", side_effect=[fail, ok]):
            with patch("time.sleep") as mock_sleep:
                request_with_retry("GET", "https://example.com", retries=3)
        mock_sleep.assert_called_with(5)


class TestMigrationManifest(unittest.TestCase):
    def _tmpdir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def test_new_manifest_is_empty(self):
        from _common import MigrationManifest
        m = MigrationManifest(self._tmpdir())
        self.assertFalse(m.already_done("foo/bar-1.0.tgz"))
        self.assertEqual(m.count, 0)

    def test_record_and_lookup(self):
        from _common import MigrationManifest
        d = self._tmpdir()
        m = MigrationManifest(d)
        m.record("pkg/v1/pkg-1.0.tgz")
        self.assertTrue(m.already_done("pkg/v1/pkg-1.0.tgz"))
        self.assertFalse(m.already_done("pkg/v2/pkg-2.0.tgz"))

    def test_persists_across_instances(self):
        from _common import MigrationManifest
        d = self._tmpdir()
        m1 = MigrationManifest(d)
        m1.record("a/1.0/a-1.0.jar")
        m2 = MigrationManifest(d)
        self.assertTrue(m2.already_done("a/1.0/a-1.0.jar"))

    def test_force_ignores_manifest(self):
        from _common import MigrationManifest
        d = self._tmpdir()
        m1 = MigrationManifest(d)
        m1.record("a/1.0/a-1.0.jar")
        m2 = MigrationManifest(d, force=True)
        self.assertFalse(m2.already_done("a/1.0/a-1.0.jar"))

    def test_thread_safe_concurrent_records(self):
        from _common import MigrationManifest
        d = self._tmpdir()
        m = MigrationManifest(d)
        paths = [f"pkg/v{i}/pkg-{i}.tgz" for i in range(50)]

        def record_all():
            for p in paths:
                m.record(p)

        threads = [threading.Thread(target=record_all) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(m.count, 50)

    def test_count_reflects_unique_records(self):
        from _common import MigrationManifest
        d = self._tmpdir()
        m = MigrationManifest(d)
        m.record("x")
        m.record("x")   # duplicate
        m.record("y")
        self.assertEqual(m.count, 2)


class TestFilterVersionsByDate(unittest.TestCase):
    def _ver(self, version: str, publish_date: str = None):
        v = {"version": version}
        if publish_date:
            v["publishDate"] = publish_date
        return v

    def test_no_filter_returns_all(self):
        from _common import filter_versions_by_date
        versions = [self._ver("1.0", "2024-03-01T00:00:00Z"), self._ver("2.0", "2025-01-01T00:00:00Z")]
        self.assertEqual(filter_versions_by_date(versions), versions)

    def test_since_filters_older_versions(self):
        from _common import filter_versions_by_date
        versions = [
            self._ver("1.0", "2023-12-31T23:59:59Z"),
            self._ver("2.0", "2024-01-01T00:00:00Z"),
            self._ver("3.0", "2024-06-15T12:00:00Z"),
        ]
        result = filter_versions_by_date(versions, since="2024-01-01")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["version"], "2.0")
        self.assertEqual(result[1]["version"], "3.0")

    def test_until_filters_newer_versions(self):
        from _common import filter_versions_by_date
        versions = [
            self._ver("1.0", "2023-06-01T00:00:00Z"),
            self._ver("2.0", "2024-01-01T00:00:00Z"),
            self._ver("3.0", "2025-01-01T00:00:00Z"),
        ]
        result = filter_versions_by_date(versions, until="2024-12-31")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["version"], "1.0")
        self.assertEqual(result[1]["version"], "2.0")

    def test_since_and_until_windowed(self):
        from _common import filter_versions_by_date
        versions = [
            self._ver("1.0", "2023-01-01T00:00:00Z"),
            self._ver("2.0", "2024-03-15T00:00:00Z"),
            self._ver("3.0", "2024-09-01T00:00:00Z"),
            self._ver("4.0", "2025-01-01T00:00:00Z"),
        ]
        result = filter_versions_by_date(versions, since="2024-01-01", until="2024-12-31")
        self.assertEqual(len(result), 2)
        self.assertEqual({r["version"] for r in result}, {"2.0", "3.0"})

    def test_since_and_until_inclusive(self):
        from _common import filter_versions_by_date
        versions = [
            self._ver("1.0", "2024-01-01T00:00:00Z"),
            self._ver("2.0", "2024-12-31T23:59:59Z"),
        ]
        result = filter_versions_by_date(versions, since="2024-01-01", until="2024-12-31")
        self.assertEqual(len(result), 2)

    def test_missing_publish_date_always_included(self):
        from _common import filter_versions_by_date
        versions = [
            self._ver("1.0"),                                   # no publishDate
            self._ver("2.0", "2023-01-01T00:00:00Z"),          # before since
        ]
        result = filter_versions_by_date(versions, since="2024-01-01")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["version"], "1.0")

    def test_empty_versions_returns_empty(self):
        from _common import filter_versions_by_date
        self.assertEqual(filter_versions_by_date([], since="2024-01-01"), [])

    def test_invalid_publish_date_format_included(self):
        from _common import filter_versions_by_date
        versions = [self._ver("1.0", "not-a-date")]
        result = filter_versions_by_date(versions, since="2024-01-01")
        self.assertEqual(len(result), 1)


class TestCheckJFrogConnection(unittest.TestCase):
    def test_exits_on_401(self):
        from _common import check_jfrog_connection
        r = MagicMock(); r.status_code = 401; r.ok = False
        with patch("requests.get", return_value=r):
            with self.assertRaises(SystemExit):
                check_jfrog_connection("https://jfrog.example.com", "repo", {})

    def test_exits_on_404(self):
        from _common import check_jfrog_connection
        r = MagicMock(); r.status_code = 404; r.ok = False
        with patch("requests.get", return_value=r):
            with self.assertRaises(SystemExit):
                check_jfrog_connection("https://jfrog.example.com", "repo", {})

    def test_ok_on_200(self):
        from _common import check_jfrog_connection
        r = MagicMock(); r.status_code = 200; r.ok = True
        with patch("requests.get", return_value=r):
            check_jfrog_connection("https://jfrog.example.com", "repo", {})


# ===========================================================================
# migrate_npm_to_jfrog.py
# ===========================================================================

class TestParseNpmName(unittest.TestCase):
    def setUp(self):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            "migrate_npm_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_npm_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_scoped_package(self):
        scope, base = self.mod._parse_npm_name("@myorg/mypackage")
        self.assertEqual(scope, "myorg")
        self.assertEqual(base, "mypackage")

    def test_unscoped_package(self):
        scope, base = self.mod._parse_npm_name("lodash")
        self.assertIsNone(scope)
        self.assertEqual(base, "lodash")

    def test_scoped_with_nested_slash(self):
        scope, base = self.mod._parse_npm_name("@scope/pkg/extra")
        self.assertEqual(scope, "scope")
        self.assertEqual(base, "pkg/extra")


class TestNpmArtifactPath(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_npm_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_npm_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_scoped_artifact_path(self):
        path = self.mod._npm_artifact_path("@scope/pkg", "pkg-1.0.0.tgz")
        self.assertEqual(path, "@scope/pkg/-/pkg-1.0.0.tgz")

    def test_unscoped_artifact_path(self):
        path = self.mod._npm_artifact_path("lodash", "lodash-4.17.21.tgz")
        self.assertEqual(path, "lodash/-/lodash-4.17.21.tgz")


class TestCollectLocalTarballs(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_npm_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_npm_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def _make_tree(self, base, paths):
        for p in paths:
            full = Path(base) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"fake")

    def test_unscoped_packages(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "lodash/4.17.21/lodash-4.17.21.tgz",
                "express/5.0.0/express-5.0.0.tgz",
            ])
            results = self.mod.collect_local_tarballs(d)
            pkg_names = {r[1] for r in results}
            self.assertIn("lodash", pkg_names)
            self.assertIn("express", pkg_names)

    def test_scoped_packages(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, ["@babel/core/7.0.0/core-7.0.0.tgz"])
            results = self.mod.collect_local_tarballs(d)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1], "@babel/core")

    def test_ignores_non_tgz(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "lodash/4.17.21/lodash-4.17.21.tgz",
                "lodash/4.17.21/lodash-4.17.21.zip",
            ])
            results = self.mod.collect_local_tarballs(d)
            self.assertEqual(len(results), 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            results = self.mod.collect_local_tarballs(d)
            self.assertEqual(results, [])


# ===========================================================================
# migrate_pip_to_jfrog.py
# ===========================================================================

class TestFileMatchesVersion(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_pip_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_pip_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_wheel_file_matches(self):
        self.assertTrue(self.mod._file_matches_version("requests-2.28.0-py3-none-any.whl", "2.28.0"))

    def test_sdist_matches(self):
        self.assertTrue(self.mod._file_matches_version("requests-2.28.0.tar.gz", "2.28.0"))

    def test_different_version_no_match(self):
        self.assertFalse(self.mod._file_matches_version("requests-2.27.0-py3-none-any.whl", "2.28.0"))

    def test_partial_version_no_false_positive(self):
        # "1.0" must not match "1.0.0.tar.gz" — the version ends right before the ext
        self.assertFalse(self.mod._file_matches_version("pkg-1.0.0.tar.gz", "1.0"))

    def test_exact_sdist_version_matches(self):
        self.assertTrue(self.mod._file_matches_version("pkg-1.0.tar.gz", "1.0"))
        self.assertTrue(self.mod._file_matches_version("pkg-1.0.zip", "1.0"))
        self.assertTrue(self.mod._file_matches_version("pkg-1.0.egg", "1.0"))


class TestCollectLocalPackagesPip(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_pip_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_pip_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def _make_tree(self, base, paths):
        for p in paths:
            full = Path(base) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"fake")

    def test_collects_whl_and_tgz(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "requests/2.28.0/requests-2.28.0-py3-none-any.whl",
                "flask/3.0.0/Flask-3.0.0.tar.gz",
            ])
            results = self.mod.collect_local_packages(d)
            names = {r[1] for r in results}
            self.assertIn("requests", names)
            self.assertIn("flask", names)

    def test_ignores_non_dist_files(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "requests/2.28.0/requests-2.28.0-py3-none-any.whl",
                "requests/2.28.0/README.txt",
            ])
            results = self.mod.collect_local_packages(d)
            self.assertEqual(len(results), 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            results = self.mod.collect_local_packages(d)
            self.assertEqual(results, [])


# ===========================================================================
# migrate_gradle_to_jfrog.py  &  migrate_maven_to_jfrog.py
# ===========================================================================

class _ParseMavenMixin:
    """Shared tests for _parse_maven_name used in both gradle and maven scripts."""

    def test_group_and_artifact(self):
        group_path, artifact_id = self.mod._parse_maven_name("com.example:my-lib")
        self.assertEqual(group_path, "com/example")
        self.assertEqual(artifact_id, "my-lib")

    def test_no_group(self):
        group_path, artifact_id = self.mod._parse_maven_name("standalone-lib")
        self.assertEqual(group_path, "")
        self.assertEqual(artifact_id, "standalone-lib")

    def test_deeply_nested_group(self):
        group_path, artifact_id = self.mod._parse_maven_name("org.apache.commons:commons-lang3")
        self.assertEqual(group_path, "org/apache/commons")
        self.assertEqual(artifact_id, "commons-lang3")


class TestParseGradleName(_ParseMavenMixin, unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_gradle_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_gradle_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)


class TestParseMavenName(_ParseMavenMixin, unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_maven_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_maven_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)


class TestCollectLocalArtifactsGradle(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_gradle_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_gradle_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def _make_tree(self, base, paths):
        for p in paths:
            full = Path(base) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"fake")

    def test_collects_jar_pom_module(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "com/example/mylib/1.0/mylib-1.0.jar",
                "com/example/mylib/1.0/mylib-1.0.pom",
                "com/example/mylib/1.0/mylib-1.0.module",
            ])
            results = self.mod.collect_local_artifacts(d)
            exts = {Path(r).suffix for r in results}
            self.assertIn(".jar", exts)
            self.assertIn(".pom", exts)
            self.assertIn(".module", exts)

    def test_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "com/example/mylib/1.0/mylib-1.0.jar",
                "com/example/mylib/1.0/mylib-1.0.txt",
            ])
            results = self.mod.collect_local_artifacts(d)
            self.assertEqual(len(results), 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            results = self.mod.collect_local_artifacts(d)
            self.assertEqual(results, [])


# ===========================================================================
# migrate_nuget_to_jfrog.py
# ===========================================================================

class TestCollectLocalNupkgs(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_nuget_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_nuget_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def _make_tree(self, base, paths):
        for p in paths:
            full = Path(base) / p
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"fake")

    def test_collects_nupkg_files(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "Newtonsoft.Json.13.0.3.nupkg",
                "subdir/MyLib.1.0.0.nupkg",
            ])
            results = self.mod.collect_local_nupkgs(d)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r.endswith(".nupkg") for r in results))

    def test_ignores_non_nupkg(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(d, [
                "Newtonsoft.Json.13.0.3.nupkg",
                "Newtonsoft.Json.13.0.3.zip",
            ])
            results = self.mod.collect_local_nupkgs(d)
            self.assertEqual(len(results), 1)

    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            results = self.mod.collect_local_nupkgs(d)
            self.assertEqual(results, [])


# ===========================================================================
# HTTP-level smoke tests for download/upload helpers (mocked)
# ===========================================================================

class TestNpmDownloadTgz(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_npm_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_npm_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_download_unscoped_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_content.return_value = [b"data"]

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_tgz("myorg", "myfeed", "lodash", "4.17.21", d, {})
            self.assertIsNotNone(fp)
            self.assertTrue(fp.endswith("lodash-4.17.21.tgz"))
            self.assertTrue(os.path.exists(fp))

    def test_download_scoped_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_content.return_value = [b"data"]

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_tgz("myorg", "myfeed", "@babel/core", "7.0.0", d, {})
            self.assertIsNotNone(fp)
            self.assertIn("@babel", fp)

    def test_download_returns_none_on_404(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.ok = False

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_tgz("myorg", "myfeed", "missing-pkg", "1.0.0", d, {})
            self.assertIsNone(fp)

    def test_uses_cached_file_without_request(self):
        with tempfile.TemporaryDirectory() as d:
            pkg_dir = Path(d) / "lodash" / "4.17.21"
            pkg_dir.mkdir(parents=True)
            cached = pkg_dir / "lodash-4.17.21.tgz"
            cached.write_bytes(b"cached")

            with patch.object(self.mod, "request_with_retry") as mock_req:
                fp = self.mod.download_tgz("myorg", "myfeed", "lodash", "4.17.21", d, {})
            mock_req.assert_not_called()
            self.assertEqual(fp, str(cached))


class TestNpmUploadTgz(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_npm_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_npm_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_upload_returns_true_on_201(self):
        mock_resp = MagicMock(); mock_resp.status_code = 201

        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as f:
            f.write(b"fake tgz")
            fp = f.name
        try:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                ok = self.mod.upload_tgz("https://jfrog.io", "npm-local", fp, "lodash", {})
            self.assertTrue(ok)
        finally:
            os.unlink(fp)

    def test_upload_returns_false_on_500(self):
        mock_resp = MagicMock(); mock_resp.status_code = 500

        with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as f:
            f.write(b"fake tgz")
            fp = f.name
        try:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                ok = self.mod.upload_tgz("https://jfrog.io", "npm-local", fp, "lodash", {})
            self.assertFalse(ok)
        finally:
            os.unlink(fp)


class TestNugetDownload(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_nuget_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_nuget_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_download_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_content.return_value = [b"nupkg data"]

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_nupkg("myorg", "myfeed", "Newtonsoft.Json", "13.0.3", d, {})
            self.assertIsNotNone(fp)
            self.assertTrue(fp.endswith(".nupkg"))
            self.assertTrue(os.path.exists(fp))

    def test_download_returns_none_on_404(self):
        mock_resp = MagicMock(); mock_resp.status_code = 404; mock_resp.ok = False

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_nupkg("myorg", "myfeed", "Missing", "1.0.0", d, {})
            self.assertIsNone(fp)


class TestPipDownloadFile(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_pip_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_pip_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_download_whl_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_content.return_value = [b"wheel data"]

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_file(
                    "requests", "2.28.0",
                    "requests-2.28.0-py3-none-any.whl",
                    "https://example.com/requests-2.28.0-py3-none-any.whl",
                    d, {},
                )
            self.assertIsNotNone(fp)
            self.assertTrue(fp.endswith(".whl"))

    def test_download_returns_none_on_404(self):
        mock_resp = MagicMock(); mock_resp.status_code = 404; mock_resp.ok = False

        with tempfile.TemporaryDirectory() as d:
            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                fp = self.mod.download_file(
                    "requests", "2.28.0", "requests-2.28.0.tar.gz",
                    "https://example.com/requests-2.28.0.tar.gz", d, {},
                )
            self.assertIsNone(fp)


class TestGradleUploadArtifact(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_gradle_to_jfrog",
            os.path.join(os.path.dirname(__file__), "migrate_gradle_to_jfrog.py"),
        )
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_upload_returns_true_on_201(self):
        mock_resp = MagicMock(); mock_resp.status_code = 201

        with tempfile.TemporaryDirectory() as d:
            artifact = Path(d) / "com" / "example" / "mylib" / "1.0" / "mylib-1.0.jar"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"fake jar")

            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                ok = self.mod.upload_artifact("https://jfrog.io", "gradle-local", str(artifact), d, {})
            self.assertTrue(ok)

    def test_upload_returns_false_on_500(self):
        mock_resp = MagicMock(); mock_resp.status_code = 500

        with tempfile.TemporaryDirectory() as d:
            artifact = Path(d) / "mylib-1.0.jar"
            artifact.write_bytes(b"fake jar")

            with patch.object(self.mod, "request_with_retry", return_value=mock_resp):
                ok = self.mod.upload_artifact("https://jfrog.io", "gradle-local", str(artifact), d, {})
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
