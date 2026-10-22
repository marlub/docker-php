import copy
import csv
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from select_images import (
    FINGERPRINT_LABEL, HTTP, INPUTS_LABEL, PLATFORMS, Registry, ResolutionError,
    Resolver, SOURCE_FILES, canonical, select, selection_reason, sha256, tags,
    main as select_main,
)
from publish_images import publish, publish_variant, validated_digests
from verify_image import verify


IMAGE = "ghcr.io/marlub/php"
INSTALLER_API = "https://api.github.com/repos/mlocati/docker-php-extension-installer/releases/latest"
INSTALLER_URL = "https://github.com/mlocati/docker-php-extension-installer/releases/download/2.0/install-php-extensions"
PECL = "https://pecl.php.net/rest/r/redis/"
ENTRY = {
    "major": 8, "minor": 4, "patch": 17, "variant": "fpm", "os": "alpine",
    "extensions": "bcmath redis", "pecl": "", "packages": "", "node": "",
}


def digest(value):
    return "sha256:" + sha256(value.encode())


def manifest(name, platforms=PLATFORMS):
    return {"digest": digest(name), "manifests": [
        {"digest": digest(name + platform), "platform": dict(zip(("os", "architecture"), platform.split("/")))}
        for platform in platforms
    ]}


class FakeRegistry(Registry):
    def __init__(self):
        self.values = {}

    def inspect(self, reference, field="Manifest", missing_ok=False):
        key = (reference, field)
        if key not in self.values:
            if missing_ok:
                return None
            raise ResolutionError("Unexpected registry request: " + str(key))
        return copy.deepcopy(self.values[key])

    def published(self, entry, name="published", platforms=PLATFORMS):
        index = manifest(name, platforms)
        for tag in ("tagMinor", "tagPatch"):
            self.values[(f'{IMAGE}:{entry[tag]}', "Manifest")] = index
        for platform in platforms:
            self.values[(f'{IMAGE}@{digest(name + platform)}', "Image")] = {
                **dict(zip(("os", "architecture"), platform.split("/"))),
                "config": {"Labels": {
                    FINGERPRINT_LABEL: entry["fingerprint"], INPUTS_LABEL: entry["build_inputs"],
                }},
            }


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in SOURCE_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.registry = FakeRegistry()
        for base in ("php:8.4.17-fpm-alpine", "php:8.4.18-fpm-alpine", "node:24-alpine"):
            self.registry.values[(base, "Manifest")] = manifest(base)
        self.http = HTTP()
        self.http.cache = {
            INSTALLER_API: json.dumps({"assets": [{"name": "install-php-extensions", "browser_download_url": INSTALLER_URL}]}).encode(),
            INSTALLER_URL: b"installer contents",
            "https://getcomposer.org/versions": b'{"stable":[{"version":"2.9.0","min-php":70205},{"version":"2.2.0","min-php":50300}]}',
            PECL + "allreleases.xml": b'<a xmlns="urn:pecl"><r><v>6.2.0</v><s>beta</s><m>7.4</m></r><r><v>6.1.0</v><s>stable</s><m>7.4</m></r><r><v>6.0.0</v><s>stable</s><m>7.4</m></r></a>',
            PECL + "package.6.1.0.xml": b'<package xmlns="urn:package"><dependencies><required><php><min>7.4</min></php></required></dependencies></package>',
            PECL + "package.6.0.0.xml": b'<package><dependencies><required><php><min>7.0</min></php></required></dependencies></package>',
        }

    def resolver(self):
        return Resolver(self.http, self.registry, self.root)

    def resolved(self, **changes):
        return self.resolver().resolve({**ENTRY, **changes})

    def artifact_directory(self, entry, platforms=PLATFORMS):
        directory = self.root / "digests" / entry["tagMinor"]
        directory.mkdir(parents=True)
        for index, platform in enumerate(platforms):
            artifact = {key: entry[key] for key in ("tagMinor", "tagPatch", "fingerprint", "build_inputs")}
            artifact.update(platform=platform, digest=digest("published" + platform))
            (directory / f"{index}.json").write_text(canonical(artifact))
        return directory

    def test_tags_and_pins_preserve_runtime_names(self):
        entry = self.resolved()
        self.assertEqual(tags(ENTRY), ("8.4-fpm-alpine", "8.4.17-fpm-alpine"))
        self.assertEqual(entry["extensions"], "bcmath redis")
        self.assertEqual(entry["resolved"]["extensions_install"], "bcmath redis-6.1.0")
        self.assertEqual(entry["resolved"]["composer_version"], "2.9.0")
        self.assertEqual(entry["resolved"]["installer_sha256"], sha256(b"installer contents"))
        self.assertTrue(entry["resolved"]["php_images"]["linux/amd64"].startswith("php@sha256:"))
        self.assertEqual(entry["resolved"]["node_images"], {})

    def test_action_labels_preserve_json_quotes_and_commas(self):
        entry = self.resolved()
        labels = [row[0] for row in csv.reader(io.StringIO(entry["build_labels"]))]
        self.assertEqual(labels, [FINGERPRINT_LABEL + "=" + entry["fingerprint"],
                                  INPUTS_LABEL + "=" + entry["build_inputs"]])

    def test_unchanged_run_selects_nothing(self):
        self.registry.published(self.resolved())
        selected, report = select([ENTRY], self.resolver(), self.registry, IMAGE)
        self.assertEqual(selected, {"phpimages": []})
        self.assertEqual(report[0]["reason"], "unchanged")

    def test_noop_cli_emits_empty_matrix_and_false_output(self):
        self.registry.published(self.resolved())
        matrix = self.root / "matrix.json"
        matrix.write_text(canonical({"phpimages": [ENTRY]}))
        output = self.root / "output"
        selected = self.root / "selected.json"
        args = ["select_images.py", "--matrix", str(matrix), "--out", str(selected),
                "--github-output", str(output)]
        with patch("sys.argv", args), patch("select_images.Resolver", return_value=self.resolver()), \
                patch("select_images.Registry", return_value=self.registry), patch("builtins.print"):
            self.assertEqual(select_main(), 0)
        self.assertEqual(json.loads(selected.read_text()), {"phpimages": []})
        self.assertIn("has_changes=false\n", output.read_text())
        self.assertEqual(len(output.read_text().splitlines()), 2)

    def test_forced_selection_ignores_published_state(self):
        self.registry.published(self.resolved())
        selected, report = select([ENTRY], self.resolver(), self.registry, IMAGE, select_all=True)
        self.assertEqual(len(selected["phpimages"]), 1)
        self.assertTrue(report[0]["selected"])

    def test_missing_tag_selects_first_build(self):
        self.assertEqual(selection_reason(self.resolved(), self.registry, IMAGE), "published tag missing")

    def test_mismatched_tags_are_retried(self):
        entry = self.resolved()
        self.registry.published(entry)
        self.registry.values[(f'{IMAGE}:{entry["tagPatch"]}', "Manifest")] = manifest("older")
        self.assertEqual(selection_reason(entry, self.registry, IMAGE), "minor and patch tags differ")

    def test_missing_architecture_is_retried(self):
        entry = self.resolved()
        self.registry.published(entry, platforms=("linux/amd64",))
        self.assertIn("platform missing", selection_reason(entry, self.registry, IMAGE))

    def test_missing_and_corrupt_labels_are_retried(self):
        entry = self.resolved()
        for labels in ({}, {FINGERPRINT_LABEL: entry["fingerprint"], INPUTS_LABEL: "invalid"}):
            with self.subTest(labels=labels):
                self.registry.published(entry)
                self.registry.values[(f'{IMAGE}@{digest("publishedlinux/arm64")}', "Image")]["config"]["Labels"] = labels
                self.assertIsNotNone(selection_reason(entry, self.registry, IMAGE))

    def test_same_tag_base_digest_change_selects_variant(self):
        self.registry.published(self.resolved())
        self.registry.values[("php:8.4.17-fpm-alpine", "Manifest")] = manifest("updated-base")
        self.assertIsNotNone(selection_reason(self.resolved(), self.registry, IMAGE))

    def test_unrelated_platform_and_index_metadata_do_not_change_fingerprint(self):
        before = self.resolved()
        index = self.registry.values[("php:8.4.17-fpm-alpine", "Manifest")]
        index["digest"] = digest("other-index")
        index["manifests"].extend(manifest("extra", ("linux/s390x",))["manifests"])
        self.assertEqual(before["fingerprint"], self.resolved()["fingerprint"])

    def test_node_change_affects_only_node_variants(self):
        plain, node = self.resolved(), self.resolved(node="24")
        self.registry.values[("node:24-alpine", "Manifest")] = manifest("new-node")
        self.assertEqual(plain["fingerprint"], self.resolved()["fingerprint"])
        self.assertNotEqual(node["fingerprint"], self.resolved(node="24")["fingerprint"])

    def test_composer_installer_and_extension_updates_change_fingerprint(self):
        before = self.resolved()["fingerprint"]
        for url, body in (
            (INSTALLER_URL, b"updated installer"),
            ("https://getcomposer.org/versions", b'{"stable":[{"version":"2.9.1","min-php":70205}]}'),
            (PECL + "allreleases.xml", b'<a><r><v>6.0.0</v><s>stable</s></r></a>'),
        ):
            with self.subTest(url=url):
                original = self.http.cache[url]
                self.http.cache[url] = body
                self.assertNotEqual(before, self.resolved()["fingerprint"])
                self.http.cache[url] = original

    def test_core_only_image_does_not_query_pecl(self):
        entry = self.resolved(extensions="bcmath gd")
        self.assertEqual(json.loads(entry["build_inputs"])["extension_versions"], {})
        self.assertEqual(entry["resolved"]["extensions_install"], "bcmath gd")

    def test_pecl_install_list_is_versioned_separately(self):
        entry = self.resolved(extensions="bcmath", pecl="redis")
        self.assertEqual(entry["pecl"], "redis")
        self.assertEqual(entry["resolved"]["pecl_install"], "redis-6.1.0")

    def test_no_extensions_does_not_resolve_composer(self):
        del self.http.cache["https://getcomposer.org/versions"]
        self.assertEqual(self.resolved(extensions="")["resolved"]["composer_version"], "")

    def test_php_constraints_choose_compatible_stable_release(self):
        incompatible = (
            b"<min>8.5</min>", b"<min>7.4</min><max>8.3.99</max>",
            b"<min>7.4</min><exclude>8.4.17</exclude>",
        )
        for constraint in incompatible:
            with self.subTest(constraint=constraint):
                self.http.cache[PECL + "package.6.1.0.xml"] = b"<package><dependencies><required><php>" + constraint + b"</php></required></dependencies></package>"
                self.assertEqual(self.resolver().extension("redis", "8.4.17"), "6.0.0")

    def test_no_compatible_release_fails(self):
        self.http.cache[PECL + "allreleases.xml"] = b'<a><r><v>6.1.0</v><s>stable</s><m>9.0</m></r></a>'
        with self.assertRaisesRegex(ResolutionError, "No stable redis"):
            self.resolved()

    def test_prerelease_php_requirement_and_mislabeled_extension_release(self):
        self.http.cache[PECL + "allreleases.xml"] = b'<a><r><v>7.0.0RC1</v><s>stable</s></r><r><v>6.1.0</v><s>stable</s></r></a>'
        self.http.cache[PECL + "package.6.1.0.xml"] = b'<package><dependencies><required><php><min>8.4.0RC1</min></php></required></dependencies></package>'
        self.assertEqual(self.resolver().extension("redis", "8.4.17"), "6.1.0")

    def test_source_and_configuration_changes_affect_fingerprint(self):
        before = self.resolved()["fingerprint"]
        self.assertNotEqual(before, self.resolved(packages="curl")["fingerprint"])
        self.assertNotEqual(before, self.resolved(patch=18)["fingerprint"])
        (self.root / "Dockerfile").write_text("modified build")
        self.assertNotEqual(before, self.resolved()["fingerprint"])

    def test_docs_and_unrelated_matrix_changes_do_not_affect_fingerprint(self):
        before = self.resolved()["fingerprint"]
        (self.root / "README.md").write_text("new documentation")
        (self.root / "matrix.json").write_text('{"unrelated":"variant"}')
        self.assertEqual(before, self.resolved()["fingerprint"])

    def test_missing_base_architecture_fails_resolution(self):
        self.registry.values[("php:8.4.17-fpm-alpine", "Manifest")] = manifest("base", ("linux/amd64",))
        with self.assertRaisesRegex(ResolutionError, "missing linux/arm64"):
            self.resolved()

    def test_publish_requires_both_architectures(self):
        entry = self.resolved()
        directory = self.artifact_directory(entry, ("linux/amd64",))
        run = Mock()
        with self.assertRaises(ResolutionError):
            publish_variant(entry, directory, IMAGE, self.registry, run)
        run.assert_not_called()

    def test_duplicate_architecture_and_mismatched_fingerprint_are_rejected(self):
        entry = self.resolved()
        directory = self.artifact_directory(entry)
        path = directory / "1.json"
        original = json.loads(path.read_text())
        for field, value in (("platform", "linux/amd64"), ("fingerprint", "wrong"), ("digest", "wrong")):
            with self.subTest(field=field):
                path.write_text(canonical({**original, field: value}))
                with self.assertRaises(ResolutionError):
                    validated_digests(entry, directory)

    def test_successful_publish_tags_same_manifest_and_next_run_skips(self):
        entry = self.resolved()
        directory = self.artifact_directory(entry)
        self.registry.published(entry)
        run = Mock()
        publish_variant(entry, directory, IMAGE, self.registry, run)
        command = run.call_args.args[0]
        self.assertEqual(command.count("--tag"), 2)
        self.assertIn(f'{IMAGE}:{entry["tagMinor"]}', command)
        self.assertIn(f'{IMAGE}:{entry["tagPatch"]}', command)
        self.assertIsNone(selection_reason(entry, self.registry, IMAGE))

    def test_invalid_pushed_labels_prevent_tagging(self):
        entry = self.resolved()
        directory = self.artifact_directory(entry)
        self.registry.published(entry)
        self.registry.values[(f'{IMAGE}@{digest("publishedlinux/arm64")}', "Image")]["config"]["Labels"] = {}
        run = Mock()
        with self.assertRaisesRegex(ResolutionError, "dependency labels"):
            publish_variant(entry, directory, IMAGE, self.registry, run)
        run.assert_not_called()

    def test_partial_failure_does_not_block_complete_variant(self):
        incomplete = self.resolved()
        complete = self.resolved(node="24")
        self.artifact_directory(incomplete, ("linux/amd64",))
        self.artifact_directory(complete)
        self.registry.published(complete)
        run = Mock()
        failed, report = publish([incomplete, complete], self.root / "digests", IMAGE, self.registry, run)
        self.assertTrue(failed)
        self.assertIn("Failed", report)
        self.assertIn("Published", report)
        run.assert_called_once()

    def test_interrupted_publication_is_retried(self):
        entry = self.resolved()
        directory = self.artifact_directory(entry)
        self.registry.published(entry)
        self.registry.values[(f'{IMAGE}:{entry["tagMinor"]}', "Manifest")] = manifest("old")
        with self.assertRaisesRegex(ResolutionError, "Tag verification failed"):
            publish_variant(entry, directory, IMAGE, self.registry, Mock())
        self.assertIsNotNone(selection_reason(entry, self.registry, IMAGE))

    def test_version_verification_rejects_unexpected_installed_extension(self):
        entry = self.resolved()
        labels = {FINGERPRINT_LABEL: entry["fingerprint"], INPUTS_LABEL: entry["build_inputs"]}
        run = Mock(side_effect=[canonical(labels), "8.4.17", '{"redis":"6.0.0"}'])
        with self.assertRaisesRegex(ValueError, "Installed extension versions differ"):
            verify("test", entry["build_inputs"], entry["fingerprint"], run)

    def test_installed_versions_and_labels_match(self):
        entry = self.resolved()
        labels = {FINGERPRINT_LABEL: entry["fingerprint"], INPUTS_LABEL: entry["build_inputs"]}
        run = Mock(side_effect=[canonical(labels), "8.4.17", '{"redis":"6.1.0"}', "Composer version 2.9.0 2025-01-01"])
        verify("test", entry["build_inputs"], entry["fingerprint"], run)


class NetworkTests(unittest.TestCase):
    @patch("select_images.time.sleep")
    @patch("select_images.subprocess.run")
    def test_authentication_failure_is_not_a_missing_image(self, run, sleep):
        run.return_value = subprocess.CompletedProcess([], 1, "", "401 Unauthorized")
        with self.assertRaisesRegex(ResolutionError, "Unauthorized"):
            Registry().inspect("image:tag", missing_ok=True)
        self.assertEqual(run.call_count, 3)

    @patch("select_images.subprocess.run")
    def test_registry_not_found_is_a_missing_image(self, run):
        for message in ("manifest unknown", "ERROR: image:tag: not found"):
            with self.subTest(message=message):
                run.return_value = subprocess.CompletedProcess([], 1, "", message)
                self.assertIsNone(Registry().inspect("image:tag", missing_ok=True))

    @patch("select_images.subprocess.run")
    def test_invalid_registry_json_is_an_error(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "null", "")
        with self.assertRaisesRegex(ResolutionError, "Invalid registry metadata"):
            Registry().inspect("image:tag")

    @patch("select_images.time.sleep")
    @patch("select_images.urlopen")
    def test_upstream_network_failure_is_an_error(self, urlopen, sleep):
        urlopen.side_effect = URLError("offline")
        with self.assertRaisesRegex(ResolutionError, "offline"):
            HTTP().get("https://pecl.php.net/rest/r/redis/allreleases.xml")
        self.assertEqual(urlopen.call_count, 3)

    @patch("select_images.time.sleep")
    @patch("select_images.urlopen")
    def test_upstream_authentication_failure_is_not_retried(self, urlopen, sleep):
        urlopen.side_effect = HTTPError("url", 403, "Forbidden", {}, None)
        with self.assertRaisesRegex(ResolutionError, "HTTP 403"):
            HTTP().get(INSTALLER_API)
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
