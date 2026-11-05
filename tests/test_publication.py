"""Registry metadata and publication tests without network access or Docker."""

import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from test_images import ROOT
from image_config import REVISION_LABEL, VARIANT_LABEL
from plan_ci import load_variants
from publication import (ARCHITECTURES, INDEX_TYPE, Manifest, Registry, content_digest,
                         main, normalized_config, publish, reusable_digest)

IMAGE = "example.test/php"


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def image_config(entry, arch="amd64", revision=None):
    return {"architecture": arch, "os": "linux", "created": "2026-01-01T00:00:00Z",
            "rootfs": {"type": "layers", "diff_ids": [content_digest(b"filesystem")]},
            "config": {"Env": ["PATH=/bin", "MODE=production"], "Entrypoint": ["/entrypoint"],
                       "Labels": {REVISION_LABEL: revision or entry["source_ref"], VARIANT_LABEL: entry["tagMinor"]}},
            "history": [{"created_by": "RUN install"}]}


class MemoryRegistry(Registry):
    """Exercise the real registry parser and digest checks with an in-memory transport."""

    def __init__(self):
        self.manifests = {}
        self.blobs = {}
        self.calls = []
        self.errors = {}
        self.ignore_writes = False

    def command(self, arguments, *, data=None):
        self.calls.append(arguments)
        key = tuple(arguments)
        if key in self.errors:
            raise self.errors.pop(key)
        if arguments[:2] == ["manifest", "get"]:
            if arguments[2] not in self.manifests:
                raise subprocess.CalledProcessError(1, arguments, stderr=b"manifest unknown: HTTP 404")
            return self.manifests[arguments[2]]
        if arguments[:2] == ["blob", "get"]:
            return self.blobs[arguments[3]]
        if arguments[:2] == ["manifest", "put"]:
            if not self.ignore_writes:
                self.manifests[arguments[2]] = data
                self.manifests[IMAGE + "@" + content_digest(data)] = data
            return b""
        raise AssertionError(arguments)

    @property
    def writes(self):
        return [call[2] for call in self.calls if call[:2] == ["manifest", "put"]]

    def add_image(self, config):
        raw = encoded(config)
        config_digest = content_digest(raw)
        self.blobs[config_digest] = raw
        document = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                               "digest": config_digest, "size": len(raw)},
                    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar",
                                "digest": digest, "size": 123} for digest in config["rootfs"]["diff_ids"]]}
        raw = encoded(document)
        self.manifests[IMAGE + "@" + content_digest(raw)] = raw
        return content_digest(raw)

    def add_index(self, children, tags):
        descriptors = []
        for arch, digest in children.items():
            raw = self.manifests[IMAGE + "@" + digest]
            descriptors.append({"mediaType": json.loads(raw)["mediaType"], "digest": digest, "size": len(raw),
                                "platform": {"os": "linux", "architecture": arch}})
        raw = encoded({"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": descriptors})
        for tag in tags:
            self.manifests[IMAGE + ":" + tag] = raw
        self.manifests[IMAGE + "@" + content_digest(raw)] = raw
        return Manifest.parse(raw)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.entry = load_variants(ROOT)[0]
        self.registry = MemoryRegistry()
        self.tested = {arch: image_config(self.entry, arch) for arch in ARCHITECTURES}
        self.published = copy.deepcopy(self.tested)
        for config in self.published.values():
            config["created"] = "2025-01-01T00:00:00Z"
            config["history"] = [{"created_by": "old build"}]
            config["config"]["Labels"][REVISION_LABEL] = "b" * 40
        self.children = {arch: self.registry.add_image(config) for arch, config in self.published.items()}
        self.baseline = self.registry.add_index(self.children, self.entry["publishTags"])
        self.records = {arch: self.record(arch) for arch in ARCHITECTURES}

    def record(self, arch, *, uploaded=False):
        return {"group_id": self.entry["base_target"], "source_ref": self.entry["source_ref"], "arch": arch,
                "run_attempt": "2", "status": "success", "publication": "uploaded" if uploaded else "reused",
                "tested_config": copy.deepcopy(self.tested[arch]),
                "digest": self.registry.add_image(self.tested[arch]) if uploaded else self.children[arch]}

    def publish(self, *, superseded=False):
        return publish(self.registry, IMAGE, self.entry, self.records, "2", lambda: superseded)

    def test_metadata_only_change_reuses_each_architecture_and_leaves_all_tags_untouched(self):
        for arch in ARCHITECTURES:
            self.assertEqual(reusable_digest(self.registry, IMAGE, self.entry, arch, self.tested[arch]),
                             (self.children[arch], None))
        result = self.publish()
        self.assertEqual(result["result"], "Unchanged")
        self.assertEqual(result["revisions"], dict.fromkeys(ARCHITECTURES, "b" * 40))
        self.assertFalse(self.registry.writes)
        self.assertTrue(all(call[:2] in (["manifest", "get"], ["blob", "get"]) for call in self.registry.calls))
        self.assertTrue(all(call[3] in self.registry.blobs for call in self.registry.calls if call[:2] == ["blob", "get"]))

    def test_layers_runtime_other_labels_unknown_fields_and_array_order_prevent_reuse(self):
        changes = [
            lambda config: config["rootfs"]["diff_ids"].__setitem__(0, content_digest(b"changed")),
            lambda config: config["config"]["Env"].reverse(),
            lambda config: config["config"].update(User="1234"),
            lambda config: config["config"].update(Cmd=["php-fpm", "-F"]),
            lambda config: config["config"]["Labels"].update(example="changed"),
            lambda config: config.update(future_runtime_field={"important": True}),
        ]
        for change in changes:
            with self.subTest(change=change):
                tested = copy.deepcopy(self.tested["amd64"])
                change(tested)
                self.assertEqual(reusable_digest(self.registry, IMAGE, self.entry, "amd64", tested), (None, None))

    def test_missing_null_and_empty_label_maps_normalize_equally(self):
        values = []
        for labels in (None, {}, {REVISION_LABEL: "a" * 40}):
            values.append(normalized_config({"config": {"Labels": labels}}))
        values.append(normalized_config({"config": {}}))
        self.assertTrue(all(value == values[0] for value in values))

    def test_missing_baseline_uploads_normally_without_warning(self):
        del self.registry.manifests[IMAGE + ":" + self.entry["tagMinor"]]
        self.assertEqual(reusable_digest(self.registry, IMAGE, self.entry, "amd64", self.tested["amd64"]), (None, None))
        self.records = {arch: self.record(arch, uploaded=True) for arch in ARCHITECTURES}
        self.assertEqual(self.publish()["result"], "Published")
        self.assertEqual(self.registry.writes, [IMAGE + ":" + tag for tag in self.entry["publishTags"]])

    def test_only_changed_architecture_is_uploaded_and_index_combines_old_and_new(self):
        self.tested["arm64"]["rootfs"]["diff_ids"] = [content_digest(b"new arm64 filesystem")]
        self.assertEqual(reusable_digest(self.registry, IMAGE, self.entry, "amd64", self.tested["amd64"])[0], self.children["amd64"])
        self.assertIsNone(reusable_digest(self.registry, IMAGE, self.entry, "arm64", self.tested["arm64"])[0])
        self.records["arm64"] = self.record("arm64", uploaded=True)
        self.assertEqual(self.publish()["result"], "Published")
        for tag in self.entry["publishTags"]:
            index = self.registry.manifest(IMAGE + ":" + tag)
            self.assertEqual(index.children(), {arch: record["digest"] for arch, record in self.records.items()})

    def test_missing_and_incorrect_aliases_are_repaired_using_exact_existing_index(self):
        tags = self.entry["publishTags"]
        del self.registry.manifests[IMAGE + ":" + tags[1]]
        other = self.registry.add_index(self.children, [tags[2]])
        altered = copy.deepcopy(other.document)
        altered["annotations"] = {"different": "index"}
        self.registry.manifests[IMAGE + ":" + tags[2]] = encoded(altered)
        result = self.publish()
        self.assertEqual(result["result"], "Retagged")
        self.assertEqual(self.registry.writes, [IMAGE + ":" + tag for tag in tags[1:3]])
        self.assertTrue(all(self.registry.manifest(IMAGE + ":" + tag).digest == self.baseline.digest for tag in tags))

    def test_final_baseline_recheck_retains_a_new_equivalent_index(self):
        newer = copy.deepcopy(self.published)
        for config in newer.values():
            config["config"]["Labels"][REVISION_LABEL] = "c" * 40
        current_children = {arch: self.registry.add_image(config) for arch, config in newer.items()}
        current = self.registry.add_index(current_children, self.entry["publishTags"])
        result = self.publish()
        self.assertEqual(result["result"], "Unchanged")
        self.assertEqual(result["revisions"], dict.fromkeys(ARCHITECTURES, "c" * 40))
        self.assertFalse(self.registry.writes)
        self.assertNotEqual(current.digest, self.baseline.digest)

    def test_optional_read_and_malformed_metadata_errors_warn_then_publish_verified_images(self):
        reference = IMAGE + ":" + self.entry["tagMinor"]
        for error in (subprocess.CalledProcessError(1, ["regctl"], stderr=b"unauthorized"), ValueError("bad metadata")):
            with self.subTest(error=error), redirect_stderr(io.StringIO()) as warning:
                self.registry.errors[("manifest", "get", reference, "--format", "raw-body")] = error
                digest, message = reusable_digest(self.registry, IMAGE, self.entry, "amd64", self.tested["amd64"])
                self.assertIsNone(digest)
                self.assertIn("comparison failed", message)
                self.registry.errors[("manifest", "get", reference, "--format", "raw-body")] = error
                self.assertEqual(self.publish()["result"], "Published")
                self.assertIn("::warning::", warning.getvalue())

    def test_wrong_published_platform_variant_revision_and_rootfs_cannot_be_reused(self):
        for mutate in (
            lambda config: config.update(architecture="arm64"),
            lambda config: config["config"]["Labels"].update({VARIANT_LABEL: "other"}),
            lambda config: config["config"]["Labels"].update({REVISION_LABEL: "not-a-sha"}),
            lambda config: config["rootfs"].update(diff_ids=["invalid"]),
        ):
            with self.subTest(mutate=mutate), redirect_stderr(io.StringIO()):
                config = copy.deepcopy(self.published["amd64"])
                mutate(config)
                children = {**self.children, "amd64": self.registry.add_image(config)}
                self.registry.add_index(children, [self.entry["tagMinor"]])
                result, warning = reusable_digest(self.registry, IMAGE, self.entry, "amd64", self.tested["amd64"])
                self.assertIsNone(result)
                self.assertIsNotNone(warning)

    def test_invalid_records_and_selected_image_fail_before_writes(self):
        original = copy.deepcopy(self.records)
        mutations = [
            lambda record: record.pop("tested_config"),
            lambda record: record.pop("publication"),
            lambda record: record.update(publication="unknown"),
            lambda record: record.update(status="failure"),
            lambda record: record.update(run_attempt="1"),
            lambda record: record.update(source_ref="a" * 40),
            lambda record: record.update(arch="arm64"),
            lambda record: record["tested_config"]["config"]["Labels"].update({REVISION_LABEL: "a" * 40}),
            lambda record: record["tested_config"]["config"].update(User="wrong"),
            lambda record: record.update(publication="uploaded"),  # The reused image has an older source.
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.records = copy.deepcopy(original)
                mutation(self.records["amd64"])
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertFalse(self.registry.writes)
        self.records = original
        del self.registry.manifests[IMAGE + "@" + self.children["arm64"]]
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.assertFalse(self.registry.writes)

    def test_superseded_sources_cannot_write_tags_even_during_comparison_fallback(self):
        self.tested["arm64"]["config"]["User"] = "new"
        self.records["arm64"] = self.record("arm64", uploaded=True)
        self.registry.errors[("manifest", "get", IMAGE + ":" + self.entry["tagMinor"], "--format", "raw-body")] = ValueError("bad index")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.publish(superseded=True)["result"], "Superseded")
        self.assertFalse(self.registry.writes)

    def test_tag_validation_precedes_registry_access(self):
        for tags in (None, [], ["other", "other"], [self.entry["tagMinor"], "-invalid"],
                     self.entry["publishTags"] + [None]):
            with self.subTest(tags=tags):
                self.entry["publishTags"] = tags
                with self.assertRaises(ValueError):
                    self.publish()
                self.assertFalse(self.registry.calls)

    def test_actual_write_and_final_verification_failures_remain_fatal(self):
        tag = self.entry["publishTags"][1]
        del self.registry.manifests[IMAGE + ":" + tag]
        key = ("manifest", "put", IMAGE + ":" + tag, "--content-type", INDEX_TYPE)
        self.registry.errors[key] = subprocess.CalledProcessError(1, ["regctl"], stderr=b"write denied")
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()
        self.registry.ignore_writes = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.publish()

    def test_registry_digest_layer_count_and_index_shape_are_checked(self):
        reference = IMAGE + "@" + self.children["amd64"]
        original = self.registry.manifests[reference]
        self.registry.manifests[reference] = original + b" "
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.publish()
        self.registry.manifests[reference] = original
        config_digest = json.loads(original)["config"]["digest"]
        raw = self.registry.blobs[config_digest]
        self.registry.blobs[config_digest] = raw + b" "
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.publish()
        self.registry.blobs[config_digest] = raw
        for descriptors in ([], [self.baseline.document["manifests"][0]] * 2,
                            self.baseline.document["manifests"] * 2):
            bad = {**self.baseline.document, "manifests": descriptors}
            with self.assertRaises(ValueError):
                Manifest.parse(encoded(bad)).children()

    def test_cli_requires_matching_sidecars_and_reports_guard_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for arch, record in self.records.items():
                (directory / (arch + ".json")).write_text(json.dumps(record))
                (directory / (arch + ".digest")).write_text(record["digest"] + "\n")
            output = directory / "github-output"
            arguments = ["--image", IMAGE, "--entry", json.dumps(self.entry), "--candidates", str(directory),
                         "--attempt", "2", "--github-output", str(output)]
            with patch("publication.Registry", return_value=self.registry), \
                    patch("publication.is_candidate_superseded", return_value=False) as current, \
                    patch("builtins.print"):
                self.assertEqual(main(arguments), 0)
                current.assert_called_once()
            self.assertEqual(output.read_text(), "result=Unchanged\n")
            (directory / "arm64.digest").write_text(content_digest(b"wrong"))
            with patch("publication.Registry", return_value=self.registry), redirect_stderr(io.StringIO()):
                self.assertEqual(main(arguments), 1)
            self.assertFalse(self.registry.writes)


if __name__ == "__main__":
    unittest.main()
