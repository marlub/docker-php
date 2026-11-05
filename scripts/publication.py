#!/usr/bin/env python3
"""Compare tested images and publish only changed contents or missing tags."""

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

from image_config import REVISION_LABEL, VARIANT_LABEL
from plan_ci import is_candidate_superseded

ROOT = Path(__file__).resolve().parent.parent
ARCHITECTURES = ("amd64", "arm64")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
INDEX_TYPES = {INDEX_TYPE, "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json"}
COMPARISON_ERRORS = (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def content_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def descriptor_digest(descriptor: dict) -> str:
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("digest"), str):
        raise ValueError("Malformed image descriptor")
    digest = descriptor["digest"]
    if DIGEST.fullmatch(digest) is None:
        raise ValueError("Invalid image descriptor digest")
    return digest


def normalized_config(config: dict) -> dict:
    """Preserve unknown fields and array ordering; discard only build metadata."""
    result = copy.deepcopy(config)
    result.pop("created", None)
    result.pop("history", None)
    runtime = result.setdefault("config", {})
    labels = runtime.get("Labels") or {}
    labels.pop(REVISION_LABEL, None)
    runtime["Labels"] = labels
    return result


def verify_config(config: dict, entry: dict, arch: str, *, current: bool = True) -> None:
    if not isinstance(config, dict) or arch not in ARCHITECTURES:
        raise ValueError("Malformed image configuration")
    if config.get("os") != "linux" or config.get("architecture") != arch:
        raise ValueError("Candidate architecture mismatch")
    runtime = config.get("config")
    rootfs = config.get("rootfs")
    if not isinstance(runtime, dict) or not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
        raise ValueError("Missing runtime configuration or filesystem layer hashes")
    layers = rootfs.get("diff_ids")
    if not isinstance(layers, list) or any(not isinstance(value, str) or not DIGEST.fullmatch(value) for value in layers):
        raise ValueError("Malformed filesystem layer hashes")
    labels = runtime.get("Labels")
    if labels is None:
        labels = {}
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise ValueError("Malformed image labels")
    revision = labels.get(REVISION_LABEL, "")
    if (not re.fullmatch(r"[a-f0-9]{40}", revision) or labels.get(VARIANT_LABEL) != entry["tagMinor"]
            or (current and revision != entry["source_ref"])):
        raise ValueError("Candidate source/variant label mismatch")
    for name in ("Env", "Entrypoint", "Cmd", "OnBuild", "Shell"):
        value = runtime.get(name)
        if value is not None and (not isinstance(value, list) or any(not isinstance(item, str) for item in value)):
            raise ValueError(f"Malformed runtime setting: {name}")
    for name in ("User", "WorkingDir", "StopSignal"):
        if runtime.get(name) is not None and not isinstance(runtime[name], str):
            raise ValueError(f"Malformed runtime setting: {name}")
    for name in ("Volumes", "ExposedPorts"):
        if runtime.get(name) is not None and not isinstance(runtime[name], dict):
            raise ValueError(f"Malformed runtime setting: {name}")


@dataclass(frozen=True)
class Manifest:
    raw: bytes
    document: dict

    @classmethod
    def parse(cls, raw: bytes, expected: str | None = None):
        if expected is not None and content_digest(raw) != expected:
            raise ValueError("Registry manifest digest mismatch")
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("schemaVersion") != 2:
            raise ValueError("Malformed image manifest")
        return cls(raw, document)

    @property
    def digest(self) -> str:
        return content_digest(self.raw)

    def children(self) -> dict[str, str]:
        if self.document.get("mediaType") not in INDEX_TYPES or not isinstance(self.document.get("manifests"), list):
            raise ValueError("Expected a multiarchitecture image index")
        children = {}
        for descriptor in self.document["manifests"]:
            digest = descriptor_digest(descriptor)
            platform = descriptor.get("platform", {})
            if not isinstance(platform, dict):
                raise ValueError("Malformed image platform")
            arch = platform.get("architecture")
            if platform.get("os") != "linux" or arch not in ARCHITECTURES or arch in children:
                raise ValueError("Unexpected or duplicate image platform")
            if descriptor.get("mediaType") not in MANIFEST_TYPES:
                raise ValueError("Unexpected architecture manifest media type")
            children[arch] = digest
        if set(children) != set(ARCHITECTURES):
            raise ValueError("Missing image architecture")
        return children


class Registry:
    """Use regctl for authentication and metadata reads; never pull layer blobs."""

    def command(self, arguments: list[str], *, data: bytes | None = None) -> bytes:
        return subprocess.run(["regctl", *arguments], input=data, capture_output=True,
                              check=True, timeout=60).stdout

    def manifest(self, reference: str, *, missing_ok: bool = False) -> Manifest | None:
        try:
            raw = self.command(["manifest", "get", reference, "--format", "raw-body"])
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode(errors="replace").lower()
            if missing_ok and re.search(r"\b404\b|manifest unknown|manifest_unknown|not found", detail):
                return None
            raise
        expected = reference.rsplit("@", 1)[1] if "@" in reference else None
        return Manifest.parse(raw, expected)

    def image(self, repository: str, digest: str) -> tuple[Manifest, dict]:
        manifest = self.manifest(f"{repository}@{digest}")
        if manifest.document.get("mediaType") not in MANIFEST_TYPES:
            raise ValueError("Expected an architecture image manifest")
        descriptor = manifest.document.get("config")
        config_digest = descriptor_digest(descriptor)
        if descriptor.get("mediaType") not in CONFIG_TYPES:
            raise ValueError("Unexpected image configuration media type")
        raw = self.command(["blob", "get", repository, config_digest])
        if content_digest(raw) != config_digest:
            raise ValueError("Registry image configuration digest mismatch")
        config = json.loads(raw)
        layers = manifest.document.get("layers")
        if not isinstance(layers, list):
            raise ValueError("Missing manifest layers")
        for layer in layers:
            descriptor_digest(layer)
        if not isinstance(config, dict) or not isinstance(config.get("rootfs"), dict):
            raise ValueError("Malformed registry image configuration")
        if not isinstance(config["rootfs"].get("diff_ids"), list) or len(layers) != len(config["rootfs"]["diff_ids"]):
            raise ValueError("Manifest and configuration layer counts differ")
        return manifest, config

    def put(self, repository: str, tag: str, manifest: Manifest) -> None:
        self.command(["manifest", "put", f"{repository}:{tag}", "--content-type", manifest.document["mediaType"]],
                     data=manifest.raw)


def comparison_warning(error: Exception) -> str:
    message = f"Published-image comparison failed; publishing tested contents instead: {error}"
    print("::warning::" + message, file=sys.stderr, flush=True)
    return message


def reusable_digest(registry: Registry, repository: str, entry: dict, arch: str, tested_config: dict) -> tuple[str | None, str | None]:
    """Optional comparison; every failure falls back to the tested local export."""
    verify_config(tested_config, entry, arch)
    try:
        baseline = registry.manifest(f"{repository}:{entry['tagMinor']}", missing_ok=True)
        if baseline is None:
            return None, None
        digest = baseline.children()[arch]
        _, published = registry.image(repository, digest)
        verify_config(published, entry, arch, current=False)
        if normalized_config(tested_config) == normalized_config(published):
            return digest, None
    except COMPARISON_ERRORS as error:
        return None, comparison_warning(error)
    return None, None


def validate_record(record: dict, entry: dict, arch: str, attempt: str) -> None:
    if not isinstance(record, dict):
        raise ValueError("Malformed publication record")
    expected = {"group_id": entry["base_target"], "source_ref": entry["source_ref"],
                "arch": arch, "run_attempt": attempt, "status": "success"}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("Publication record identity or status mismatch")
    descriptor_digest(record)
    if record.get("publication") not in ("uploaded", "reused"):
        raise ValueError("Missing candidate publication disposition")
    verify_config(record.get("tested_config"), entry, arch)


def publication_tags(entry: dict) -> list[str]:
    tags = entry.get("publishTags")
    if (not isinstance(tags, list) or len(tags) < 2
            or any(not isinstance(tag, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", tag) for tag in tags)
            or len(set(tags)) != len(tags) or entry["tagMinor"] not in tags or entry["tagPatch"] not in tags):
        raise ValueError("Invalid publication tags")
    return tags


def publish(registry: Registry, repository: str, entry: dict, records: dict[str, dict], attempt: str,
            superseded: Callable[[], bool]) -> dict:
    tags = publication_tags(entry)  # Validate every tag before any registry access or write.
    selected = {}
    revisions = {}
    for arch in ARCHITECTURES:
        record = records[arch]
        validate_record(record, entry, arch, attempt)
        manifest, actual = registry.image(repository, record["digest"])
        verify_config(actual, entry, arch, current=record["publication"] == "uploaded")
        if record["publication"] == "uploaded":
            matches = actual == record["tested_config"]
        else:
            matches = normalized_config(actual) == normalized_config(record["tested_config"])
        if not matches:
            raise ValueError("Selected registry image does not match tested configuration")
        selected[arch] = manifest
        revisions[arch] = actual["config"]["Labels"][REVISION_LABEL]

    # Everything above is mandatory candidate verification, including reused images.
    warnings = []
    retained = None
    retained_revisions = {}
    writes = tags
    try:
        baseline = registry.manifest(f"{repository}:{entry['tagMinor']}", missing_ok=True)
        if baseline is not None:
            children = baseline.children()
            equivalent = True
            for arch in ARCHITECTURES:
                _, actual = registry.image(repository, children[arch])
                verify_config(actual, entry, arch, current=False)
                equivalent &= normalized_config(actual) == normalized_config(records[arch]["tested_config"])
                retained_revisions[arch] = actual["config"]["Labels"][REVISION_LABEL]
            if equivalent:
                retained = baseline
                writes = []
                for tag in tags:
                    tagged = baseline if tag == entry["tagMinor"] else registry.manifest(f"{repository}:{tag}", missing_ok=True)
                    if tagged is None or tagged.digest != baseline.digest:
                        writes.append(tag)
    except COMPARISON_ERRORS as error:
        warnings.append(comparison_warning(error))
        retained, writes = None, tags

    result = {"result": "Unchanged", "revisions": retained_revisions if retained else revisions, "warnings": warnings}
    # Keep source/alias ownership checking immediately before the first possible write.
    if superseded():
        return {**result, "result": "Superseded"}
    if retained is None:
        descriptors = [{"mediaType": selected[arch].document["mediaType"], "digest": selected[arch].digest,
                        "size": len(selected[arch].raw), "platform": {"os": "linux", "architecture": arch}}
                       for arch in ARCHITECTURES]
        retained = Manifest.parse(json.dumps({"schemaVersion": 2, "mediaType": INDEX_TYPE, "manifests": descriptors},
                                            separators=(",", ":")).encode())
        result["result"] = "Published"
    elif writes:
        result["result"] = "Retagged"
    for tag in writes:
        registry.put(repository, tag, retained)
    expected = retained.children()
    for tag in tags:
        actual = registry.manifest(f"{repository}:{tag}")
        if actual.digest != retained.digest or actual.children() != expected:
            raise ValueError(f"Published tag verification failed: {tag}")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        entry = json.loads(args.entry)
        records = {arch: json.loads((args.candidates / f"{arch}.json").read_text()) for arch in ARCHITECTURES}
        for arch, record in records.items():
            if (args.candidates / f"{arch}.digest").read_text().strip() != record["digest"]:
                raise ValueError("Candidate digest and metadata files disagree")
        result = publish(Registry(), args.image, entry, records, args.attempt,
                         lambda: is_candidate_superseded(entry, ROOT))
        print(json.dumps(result, sort_keys=True))
        if args.github_output:
            with args.github_output.open("a") as output:
                output.write(f"result={result['result']}\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                summary.write("\nRetained/published source revisions: " + json.dumps(result["revisions"], sort_keys=True) + "\n")
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        print(f"Publication failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
