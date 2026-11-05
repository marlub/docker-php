#!/usr/bin/env python3
"""Opt-in publication check using disposable registry and regctl containers."""

import argparse
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from image_config import REVISION_LABEL, VARIANT_LABEL
from publication import (ARCHITECTURES, Manifest, Registry, content_digest, publish, reusable_digest)

REGCTL_IMAGE = "ghcr.io/regclient/regctl:v0.11.7"


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


class ContainerRegistry(Registry):
    def __init__(self, network, hostname, logs):
        self.network = network
        self.hostname = hostname
        self.calls = []
        self.logs = logs

    def command(self, arguments, *, data=None):
        self.calls.append(arguments)
        command = ["docker", "run", "--rm", "-i", "--network", self.network, REGCTL_IMAGE,
                   "--host", f"reg={self.hostname}:5000,tls=disabled", *arguments]
        result = subprocess.run(command, input=data, capture_output=True, timeout=60)
        with (self.logs / "registry-commands.log").open("a") as output:
            output.write(json.dumps({"arguments": arguments, "returncode": result.returncode}) + "\n")
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
        return result.stdout

    @property
    def writes(self):
        return [call for call in self.calls if call[:2] in (["manifest", "put"], ["blob", "put"])]


def upload_image(registry, repository, config):
    raw = encoded(config)
    digest = content_digest(raw)
    registry.command(["blob", "put", repository, "--digest", digest], data=raw)
    manifest = Manifest.parse(encoded({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest, "size": len(raw)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "digest": config["rootfs"]["diff_ids"][0],
                    "size": 10240}],
    }))
    registry.command(["manifest", "put", repository + "@" + manifest.digest, "--by-digest",
                      "--content-type", manifest.document["mediaType"]], data=manifest.raw)
    return manifest.digest


def candidates(registry, repository, entry, configs):
    result = {}
    for arch in ARCHITECTURES:
        reused, warning = reusable_digest(registry, repository, entry, arch, configs[arch])
        if warning:
            raise AssertionError(warning)
        result[arch] = {"group_id": entry["base_target"], "source_ref": entry["source_ref"], "arch": arch,
                        "run_attempt": "1", "status": "success", "tested_config": configs[arch],
                        "publication": "reused" if reused else "uploaded",
                        "digest": reused or upload_image(registry, repository, configs[arch])}
    return result


def check(registry, repository, logs):
    entry = {"tagMinor": "8.4-fpm-alpine3.24", "tagPatch": "8.4.27-fpm-alpine3.24",
             "publishTags": ["8.4-fpm-alpine3.24", "8.4.27-fpm-alpine3.24", "8.4-fpm-alpine", "8.4.27-fpm-alpine"],
             "base_target": "php-8-4-alpine", "source_ref": "a" * 40}
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as archive:
        info = tarfile.TarInfo("payload")
        info.size = len(b"publication fixture\n")
        archive.addfile(info, io.BytesIO(b"publication fixture\n"))
    layer = layer.getvalue()
    if len(layer) != 10240:
        raise AssertionError("Unexpected fixture tar size")
    layer_digest = content_digest(layer)
    registry.command(["blob", "put", repository, "--digest", layer_digest], data=layer)
    published = {arch: {"os": "linux", "architecture": arch, "created": "2025-01-01T00:00:00Z",
                        "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
                        "history": [{"created_by": "old build"}],
                        "config": {"Env": ["PATH=/bin"], "Labels": {
                            REVISION_LABEL: "b" * 40, VARIANT_LABEL: entry["tagMinor"]}}}
                 for arch in ARCHITECTURES}
    configs = copy.deepcopy(published)
    for config in configs.values():
        config["created"] = "2026-01-01T00:00:00Z"
        config["history"] = [{"created_by": "new build"}]
        config["config"]["Labels"][REVISION_LABEL] = entry["source_ref"]
    children = {arch: upload_image(registry, repository, config) for arch, config in published.items()}
    seed = {arch: {"group_id": entry["base_target"], "source_ref": "b" * 40, "arch": arch, "run_attempt": "1",
                  "status": "success", "publication": "uploaded", "tested_config": published[arch], "digest": children[arch]}
            for arch in ARCHITECTURES}
    publish(registry, repository, {**entry, "source_ref": "b" * 40}, seed, "1", lambda: False)
    reports = []
    before = len(registry.writes)
    records = candidates(registry, repository, entry, configs)
    assert all(record["publication"] == "reused" for record in records.values())
    result = publish(registry, repository, entry, records, "1", lambda: False)
    assert result["result"] == "Unchanged" and len(registry.writes) == before
    reports.append({"scenario": "unchanged", "writes": 0, **result})
    print("Both architectures reused: no candidate uploads or tag writes", flush=True)

    repair_entry = {**entry, "publishTags": entry["publishTags"] + ["recovered-alias"]}
    before = len(registry.writes)
    result = publish(registry, repository, repair_entry, records, "1", lambda: False)
    assert result["result"] == "Retagged" and len(registry.writes) == before + 1
    assert registry.writes[-1][:3] == ["manifest", "put", repository + ":recovered-alias"]
    reports.append({"scenario": "alias repair", "writes": 1, **result})
    print("Missing alias repaired using one index write and no candidate uploads", flush=True)

    configs["arm64"]["config"]["Env"].append("CHANGED=1")
    before = len(registry.writes)
    records = candidates(registry, repository, entry, configs)
    assert records["amd64"]["publication"] == "reused" and records["arm64"]["publication"] == "uploaded"
    assert len(registry.writes) == before + 2  # One configuration blob and one architecture manifest.
    result = publish(registry, repository, entry, records, "1", lambda: False)
    assert result["result"] == "Published"
    current = registry.manifest(repository + ":" + entry["tagMinor"])
    assert current.children() == {arch: records[arch]["digest"] for arch in ARCHITECTURES}
    reports.append({"scenario": "one changed architecture", "candidate_uploads": 1, **result})
    print("Only ARM64 uploaded; final tags contain reused AMD64 and new ARM64", flush=True)
    assert all(call[3] != layer_digest for call in registry.calls if call[:2] == ["blob", "get"])
    (logs / "results.json").write_text(json.dumps(reports, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    prefix = "php-publication-check-" + uuid.uuid4().hex[:10]
    network = prefix + "-network"
    name = prefix + "-registry"
    network_created = registry_created = False
    try:
        subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
        network_created = True
        subprocess.run(["docker", "run", "-d", "--name", name, "--network", network, "registry:2"],
                       check=True, capture_output=True)
        registry_created = True
        registry = ContainerRegistry(network, name, args.logs)
        repository = name + ":5000/php"
        for attempt in range(10):
            try:
                registry.manifest(repository + ":missing", missing_ok=True)
                break
            except subprocess.CalledProcessError:
                if attempt == 9:
                    raise
                time.sleep(0.2)
        check(registry, repository, args.logs)
    finally:
        if registry_created:
            result = subprocess.run(["docker", "logs", name], capture_output=True)
            (args.logs / "registry.log").write_bytes(result.stdout + result.stderr)
            subprocess.run(["docker", "rm", "-fv", name], check=True, stdout=subprocess.DEVNULL)
        if network_created:
            subprocess.run(["docker", "network", "rm", network], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
