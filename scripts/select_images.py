#!/usr/bin/env python3
"""Resolve build inputs and select images that differ from their published tags."""

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


PLATFORMS = ("linux/amd64", "linux/arm64")
LABEL_PREFIX = "io.github.marlub.docker-php."
FINGERPRINT_LABEL = LABEL_PREFIX + "inputs-sha256"
INPUTS_LABEL = LABEL_PREFIX + "inputs"
BUNDLED = frozenset("""
    bcmath bz2 calendar ctype curl dba dom enchant exif ffi fileinfo filter ftp gd
    gettext gmp hash iconv imap intl json ldap mbstring mysqli oci8 odbc opcache
    openssl pcntl pdo pdo_dblib pdo_firebird pdo_mysql pdo_oci pdo_odbc pdo_pgsql
    pdo_sqlite pgsql phar posix pspell readline reflection session shmop simplexml
    snmp soap sockets sodium spl sqlite3 standard sysvmsg sysvsem sysvshm tidy
    tokenizer xml xmlreader xmlwriter xsl zip zlib
""".split())
# These moved out of PHP's source tree in 8.4 and are now PECL packages.
MOVED_TO_PECL = frozenset(("imap", "oci8", "pdo_oci", "pspell"))
SOURCE_FILES = (
    "Dockerfile", "entrypoint.sh", "scripts/test_image.sh",
    "scripts/select_images.py", "scripts/publish_images.py", "scripts/verify_image.py",
    ".github/workflows/build-test-publish.yml",
    ".github/workflows/build-test.yml", ".github/workflows/template-images.yml",
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ResolutionError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def version_tuple(value):
    match = re.fullmatch(r"(\d+(?:\.\d+){0,3})(?:[-.]?(dev|alpha|a|beta|b|rc|pl|p)(\d*))?", value, re.I)
    if not match:
        raise ResolutionError(f"Unsupported release version: {value!r}")
    parts = tuple(map(int, match[1].split(".")))
    stage = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2,
             "rc": -1, "": 0, "pl": 1, "p": 1}[(match[2] or "").lower()]
    return parts + (0,) * (4 - len(parts)) + (stage, int(match[3] or 0))


def tags(entry):
    suffix = "-".join(filter(None, (
        entry["variant"], entry["os"], "node" + entry["node"] if entry["node"] else "",
    )))
    minor = f'{entry["major"]}.{entry["minor"]}'
    return f'{minor}-{suffix}'.rstrip("-"), f'{minor}.{entry["patch"]}-{suffix}'.rstrip("-")


class HTTP:
    def __init__(self):
        self.cache = {}

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        headers = {"User-Agent": "docker-php-dependency-check"}
        if url.startswith("https://api.github.com/") and os.environ.get("GH_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
        for attempt in range(3):
            try:
                with urlopen(Request(url, headers=headers), timeout=30) as response:
                    data = response.read()
                self.cache[url] = data
                return data
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ResolutionError(f"GET {url}: HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    raise ResolutionError(f"GET {url}: {exc}") from exc
            time.sleep(attempt + 1)
        raise AssertionError("unreachable")

    def json(self, url):
        try:
            return json.loads(self.get(url))
        except ValueError as exc:
            raise ResolutionError(f"Invalid JSON from {url}") from exc

    def xml(self, url):
        try:
            root = ET.fromstring(self.get(url))
        except ET.ParseError as exc:
            raise ResolutionError(f"Invalid XML from {url}") from exc
        # PECL's REST documents use several different default namespaces.
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        return root


class Registry:
    """Use Buildx's registry authentication and fetch metadata, never image layers."""

    def __init__(self):
        self.cache = {}

    def inspect(self, reference, field="Manifest", missing_ok=False):
        key = (reference, field)
        if key in self.cache:
            return self.cache[key]
        command = ["docker", "buildx", "imagetools", "inspect", reference,
                   "--format", "{{json ." + field + "}}"]
        for attempt in range(3):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                if attempt == 2:
                    raise ResolutionError(f"Cannot inspect {reference}: {exc}") from exc
            else:
                if result.returncode == 0:
                    try:
                        value = json.loads(result.stdout)
                    except ValueError as exc:
                        raise ResolutionError(f"Invalid registry metadata for {reference}") from exc
                    if not isinstance(value, dict):
                        raise ResolutionError(f"Invalid registry metadata for {reference}")
                    self.cache[key] = value
                    return value
                error = result.stderr.strip()
                # Do not turn authentication or generic network failures into cache misses.
                missing = any(marker in error.lower() for marker in (
                    "manifest unknown", "name unknown", "404 not found",
                )) or error.endswith(f"{reference}: not found")
                if missing_ok and missing:
                    self.cache[key] = None
                    return None
                if attempt == 2:
                    raise ResolutionError(f"Cannot inspect {reference}: {error}")
            time.sleep(attempt + 1)
        raise AssertionError("unreachable")

    def bases(self, reference):
        manifest = self.inspect(reference)
        repository = reference.rsplit(":", 1)[0]
        result = {}
        for platform in PLATFORMS:
            digest = platform_digest(manifest, platform)
            if not digest:
                raise ResolutionError(f"{reference} is missing {platform}")
            result[platform] = f"{repository}@{digest}"
        return result


def platform_digest(manifest, platform):
    matches = []
    for child in manifest.get("manifests", []):
        info = child.get("platform", {})
        if f'{info.get("os")}/{info.get("architecture")}' == platform:
            matches.append(child.get("digest", ""))
    if len(matches) == 1 and DIGEST_RE.fullmatch(matches[0]):
        return matches[0]
    return None


def compatible_php(package, php_version):
    requirements = package.find("dependencies/required/php")
    if requirements is None:
        raise ResolutionError("PECL package metadata has no PHP requirements")
    actual = version_tuple(php_version)
    minimum = requirements.findtext("min")
    maximum = requirements.findtext("max")
    if minimum and actual < version_tuple(minimum):
        return False
    if maximum and actual > version_tuple(maximum):
        return False
    return all(actual != version_tuple(item.text) for item in requirements.findall("exclude"))


class Resolver:
    def __init__(self, http, registry, source_root):
        self.http = http
        self.registry = registry
        self.sources = {name: sha256((source_root / name).read_bytes()) for name in SOURCE_FILES}
        self.releases = {}

    def extension(self, name, php_version):
        key = (name, php_version)
        if key in self.releases:
            return self.releases[key]
        base = f"https://pecl.php.net/rest/r/{name}/"
        releases = self.http.xml(base + "allreleases.xml")
        stable = [item for item in releases.findall("r") if item.findtext("s") == "stable"
                  and re.fullmatch(r"\d+(?:\.\d+){0,3}", item.findtext("v", ""))]
        stable.sort(key=lambda item: version_tuple(item.findtext("v", "")), reverse=True)
        for release in stable:
            version = release.findtext("v")
            minimum = release.findtext("m")
            if minimum and version_tuple(php_version) < version_tuple(minimum):
                continue
            package = self.http.xml(base + f"package.{version}.xml")
            if compatible_php(package, php_version):
                self.releases[key] = version
                return version
        raise ResolutionError(f"No stable {name} release supports PHP {php_version}")

    def resolve(self, entry):
        minor_tag, patch_tag = tags(entry)
        php_version = f'{entry["major"]}.{entry["minor"]}.{entry["patch"]}'
        base_tag = "-".join(filter(None, (php_version, entry["variant"], entry["os"])))
        php_images = self.registry.bases(f"php:{base_tag}")
        node_images = {}
        if entry["node"]:
            node_images = self.registry.bases(f'node:{entry["node"]}-{entry["os"] or "slim"}')

        installer = self.http.json("https://api.github.com/repos/mlocati/docker-php-extension-installer/releases/latest")
        assets = [asset for asset in installer["assets"] if asset["name"] == "install-php-extensions"]
        if len(assets) != 1:
            raise ResolutionError("Extension installer release must contain exactly one installer asset")
        installer_url = assets[0]["browser_download_url"]
        installer_sha256 = sha256(self.http.get(installer_url))
        extensions = {}
        lists = {}
        for field in ("extensions", "pecl"):
            installs = []
            for name in entry[field].split():
                if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                    raise ResolutionError(f"Use plain extension names in matrix.json, got {name!r}")
                bundled = name in BUNDLED and not (
                    name in MOVED_TO_PECL and version_tuple(php_version) >= version_tuple("8.4")
                )
                if field == "extensions" and bundled:
                    installs.append(name)
                else:
                    version = self.extension(name, php_version)
                    extensions[name] = version
                    installs.append(f"{name}-{version}")
            lists[field] = " ".join(installs)

        composer = ""
        if entry["extensions"].strip():
            releases = self.http.json("https://getcomposer.org/versions")["stable"]
            numeric_php = entry["major"] * 10000 + entry["minor"] * 100 + entry["patch"]
            compatible = [item for item in releases if item["min-php"] <= numeric_php]
            if not compatible:
                raise ResolutionError(f"No stable Composer release supports PHP {php_version}")
            composer = max(compatible, key=lambda item: version_tuple(item["version"]))["version"]

        resolved = {
            "php_images": php_images, "node_images": node_images,
            "installer_url": installer_url, "installer_sha256": installer_sha256,
            "composer_version": composer,
            "extensions_install": lists["extensions"], "pecl_install": lists["pecl"],
        }
        inputs = {"schema": 1, "variant": entry, "resolved": resolved,
                  "extension_versions": extensions, "sources": self.sources}
        build_inputs = canonical(inputs)
        fingerprint = sha256(build_inputs.encode())
        # build-push-action parses list inputs as CSV, including embedded JSON quotes.
        label_output = io.StringIO()
        writer = csv.writer(label_output, lineterminator="\n")
        writer.writerow([FINGERPRINT_LABEL + "=" + fingerprint])
        writer.writerow([INPUTS_LABEL + "=" + build_inputs])
        return {**entry, "tagMinor": minor_tag, "tagPatch": patch_tag,
                "resolved": resolved, "fingerprint": fingerprint,
                "build_inputs": build_inputs, "build_labels": label_output.getvalue()}


def selection_reason(entry, registry, image):
    manifests = [registry.inspect(f'{image}:{entry[tag]}', missing_ok=True)
                 for tag in ("tagMinor", "tagPatch")]
    if any(manifest is None for manifest in manifests):
        return "published tag missing"
    digests = [manifest.get("digest", "") for manifest in manifests]
    if not all(DIGEST_RE.fullmatch(digest) for digest in digests):
        raise ResolutionError(f"Invalid published manifest digest for {entry['tagMinor']}")
    if digests[0] != digests[1]:
        return "minor and patch tags differ"
    for platform in PLATFORMS:
        digest = platform_digest(manifests[0], platform)
        if not digest:
            return f"published platform missing: {platform}"
        config = registry.inspect(f"{image}@{digest}", "Image")
        labels = config.get("config", {}).get("Labels", {}) or {}
        if (labels.get(FINGERPRINT_LABEL) != entry["fingerprint"] or
                labels.get(INPUTS_LABEL) != entry["build_inputs"]):
            previous = labels.get(INPUTS_LABEL)
            if not previous:
                return "published dependency labels missing"
            try:
                before = json.loads(previous)
                after = json.loads(entry["build_inputs"])
            except ValueError:
                return "published dependency labels invalid"
            if not isinstance(before, dict):
                return "published dependency labels invalid"
            changed = [key for key in after if before.get(key) != after[key]]
            return "changed inputs: " + ", ".join(changed or ["fingerprint"])
    return None


def select(entries, resolver, registry, image, select_all=False):
    selected, report = [], []
    for raw in entries:
        entry = resolver.resolve(raw)
        reason = "forced/full validation" if select_all else selection_reason(entry, registry, image)
        report.append({"tag": entry["tagMinor"], "reason": reason or "unchanged", "selected": bool(reason)})
        if reason:
            selected.append(entry)
    return {"phpimages": selected}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", default="matrix.json")
    parser.add_argument("--out", required=True)
    parser.add_argument("--image", default="ghcr.io/marlub/php")
    parser.add_argument("--select-all", action="store_true")
    parser.add_argument("--github-output")
    parser.add_argument("--summary")
    args = parser.parse_args()
    try:
        registry = Registry()
        resolver = Resolver(HTTP(), registry, Path(__file__).resolve().parent.parent)
        entries = json.loads(Path(args.matrix).read_text())["phpimages"]
        matrix, report = select(entries, resolver, registry, args.image, args.select_all)
        Path(args.out).write_text(canonical(matrix) + "\n")
        if args.github_output:
            with open(args.github_output, "a") as output:
                output.write(f"matrix={canonical(matrix)}\n")
                output.write(f"has_changes={str(bool(matrix['phpimages'])).lower()}\n")
        lines = ["| Variant | Decision | Reason |", "| --- | --- | --- |"]
        for item in report:
            decision = "Build" if item["selected"] else "Skip"
            lines.append(f"| {item['tag']} | {decision} | {item['reason']} |")
        summary = "\n".join(lines) + "\n"
        print(summary)
        if args.summary:
            with open(args.summary, "a") as output:
                output.write("### Dependency check\n\n" + summary)
    except (ResolutionError, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"Dependency check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
