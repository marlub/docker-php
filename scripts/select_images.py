#!/usr/bin/env python3
"""Resolve build inputs and select images that differ from their published tags."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
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
MOVED_FROM_CORE = frozenset(("imap", "oci8", "pdo_oci", "pspell"))
PIE_PACKAGES = {
    "redis": "phpredis/phpredis", "amqp": "php-amqp/php-amqp",
    "xdebug": "xdebug/xdebug", "pcov": "pecl/pcov",
}
PIE_OPTIONS = {"redis": [
    "--enable-redis-lzf", "--with-liblzf=yes", "--enable-redis-zstd", "--with-libzstd=yes",
    "--enable-redis-lz4", "--with-liblz4=yes",
]}
PIE_API = "https://api.github.com/repos/php/pie/releases/latest"
XHPROF_API = "https://api.github.com/repos/longxinH/xhprof"
SOURCE_FILES = (
    "Dockerfile", "entrypoint.sh", "scripts/test_image.sh",
    "scripts/select_images.py", "scripts/publish_images.py", "scripts/verify_image.py",
    "scripts/install_extensions.sh", "scripts/build_config.php", "scripts/build-defaults.json",
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
        # Upstream package.xml files may use a default namespace.
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
        raise ResolutionError("Source package metadata has no PHP requirements")
    actual = version_tuple(php_version)
    minimum = requirements.findtext("min")
    maximum = requirements.findtext("max")
    if minimum and actual < version_tuple(minimum):
        return False
    if maximum and actual > version_tuple(maximum):
        return False
    return all(actual != version_tuple(item.text) for item in requirements.findall("exclude"))


class PieRunner:
    """Resolve with PIE in the Docker daemon's native PHP base, without building."""

    def __init__(self, run=None):
        self.run = run or subprocess.run
        self.platform = None

    def command(self, command, **kwargs):
        try:
            result = self.run(command, capture_output=True, text=True, timeout=300, **kwargs)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ResolutionError(f"PIE resolution failed: {exc}") from exc
        if result.returncode:
            raise ResolutionError(f"PIE resolution failed: {result.stderr.strip()} {result.stdout.strip()}")
        return result.stdout

    def native_platform(self):
        if self.platform is None:
            architecture = self.command(["docker", "info", "--format", "{{.Architecture}}"]).strip()
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture, architecture)
            self.platform = "linux/" + architecture
            if self.platform not in PLATFORMS:
                raise ResolutionError(f"Unsupported Docker architecture: {architecture}")
        return self.platform

    def info(self, images, pie_bytes, package):
        platform = self.native_platform()
        # Stream the verified PHAR over stdin instead of relying on host bind mounts
        # (the daemon may be Docker Desktop or remote).
        script = (
            "file_put_contents('/tmp/pie.phar', base64_decode(stream_get_contents(STDIN))); "
            "require '/tmp/pie.phar';"
        )
        return self.command([
            "docker", "run", "--rm", "-i", "--platform", platform,
            "--add-host", "pecl.php.net:127.0.0.1", "--entrypoint", "php", images[platform],
            "-r", script, "--", "--no-interaction", "--no-ansi", "info", package + ":*@stable",
        ], input=base64.b64encode(pie_bytes).decode())


def parse_pie_info(output, name, package):
    values = {}
    for label in ("Extension name", "Composer package name", "Version"):
        matches = re.findall(r"^" + re.escape(label) + r": ([^\r\n]+)$", output, re.M)
        if len(matches) != 1:
            raise ResolutionError(f"Missing or ambiguous PIE {label} for {package}")
        values[label] = matches[0].strip()
    version = values["Version"].removeprefix("v")
    if (values["Extension name"] != name or values["Composer package name"] != package
            or not re.fullmatch(r"\d+(?:\.\d+){1,3}", version)):
        raise ResolutionError(f"Unexpected PIE package, extension, or non-stable version for {name}")
    return version


class Resolver:
    def __init__(self, http, registry, source_root, pie_runner=None):
        self.http = http
        self.registry = registry
        self.pie_runner = pie_runner or PieRunner()
        self.sources = {name: sha256((source_root / name).read_bytes()) for name in SOURCE_FILES}
        self.releases = {}

    def pie(self):
        release = self.http.json(PIE_API)
        version = release["tag_name"].removeprefix("v")
        if release.get("prerelease") or release.get("draft") or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ResolutionError("PIE release must be stable")
        assets = [asset for asset in release["assets"] if asset["name"] == "pie.phar"]
        if len(assets) != 1:
            raise ResolutionError("PIE release must contain exactly one pie.phar asset")
        url = assets[0]["browser_download_url"]
        if url != f"https://github.com/php/pie/releases/download/{release['tag_name']}/pie.phar":
            raise ResolutionError("PIE asset URL is not a versioned upstream PHAR")
        content = self.http.get(url)
        checksum = sha256(content)
        if assets[0].get("digest") and assets[0]["digest"] != "sha256:" + checksum:
            raise ResolutionError("PIE release asset checksum mismatch")
        return {"version": version, "url": url, "sha256": checksum}, content

    def extension(self, name, php_images, pie, pie_bytes):
        key = (name, canonical(php_images), pie["sha256"])
        if key not in self.releases:
            package = PIE_PACKAGES[name]
            output = self.pie_runner.info(php_images, pie_bytes, package)
            version = parse_pie_info(output, name, package)
            self.releases[key] = {
                "package": package, "version": version,
                "configure_options": PIE_OPTIONS.get(name, []),
            }
        return self.releases[key]

    def xhprof(self, php_version):
        key = ("xhprof", php_version)
        if key in self.releases:
            return self.releases[key]
        tags = []
        page = 1
        while True:
            batch = self.http.json(f"{XHPROF_API}/tags?per_page=100&page={page}")
            tags.extend(item for item in batch if re.fullmatch(r"v?\d+\.\d+\.\d+", item["name"]))
            if len(batch) < 100:
                break
            page += 1
        tags.sort(key=lambda item: version_tuple(item["name"].removeprefix("v")), reverse=True)
        for tag in tags:
            commit = tag["commit"]["sha"]
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise ResolutionError("Invalid XHProf source commit")
            base = f"https://raw.githubusercontent.com/longxinH/xhprof/{commit}/"
            if not compatible_php(self.http.xml(base + "package.xml"), php_version):
                continue
            header = self.http.get(base + "extension/php_xhprof.h").decode()
            versions = re.findall(r'^#define\s+XHPROF_VERSION\s+"(\d+\.\d+\.\d+)"', header, re.M)
            if len(versions) != 1:
                raise ResolutionError("Missing or ambiguous XHProf module version")
            url = f"https://codeload.github.com/longxinH/xhprof/tar.gz/{commit}"
            archive = self.http.get(url)
            # Validate that the metadata used above belongs to the exact build archive.
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
                root = "xhprof-" + commit + "/"
                for path in ("package.xml", "extension/php_xhprof.h"):
                    file = source.extractfile(root + path)
                    if file is None or file.read() != self.http.get(base + path):
                        raise ResolutionError("XHProf archive metadata mismatch")
            result = {"tag": tag["name"], "commit": commit, "url": url,
                      "sha256": sha256(archive), "version": versions[0]}
            self.releases[key] = result
            return result
        raise ResolutionError(f"No stable XHProf source supports PHP {php_version}")

    def composer(self, entry):
        if not entry["extensions"].strip():
            return {"version": "", "url": "", "sha256": ""}
        releases = self.http.json("https://getcomposer.org/versions")["stable"]
        numeric_php = entry["major"] * 10000 + entry["minor"] * 100 + entry["patch"]
        compatible = [item for item in releases if item["min-php"] <= numeric_php
                      and re.fullmatch(r"\d+\.\d+\.\d+", item["version"])]
        if not compatible:
            raise ResolutionError("No stable Composer release supports this PHP version")
        version = max(compatible, key=lambda item: version_tuple(item["version"]))["version"]
        url = f"https://getcomposer.org/download/{version}/composer.phar"
        checksum = sha256(self.http.get(url))
        if self.http.get(url + ".sha256sum").decode().split()[0] != checksum:
            raise ResolutionError("Composer PHAR checksum mismatch")
        return {"version": version, "url": url, "sha256": checksum}

    def resolve(self, entry):
        if "pecl" in entry:
            raise ResolutionError("The pecl matrix field was removed; use extensions")
        minor_tag, patch_tag = tags(entry)
        php_version = f'{entry["major"]}.{entry["minor"]}.{entry["patch"]}'
        base_tag = "-".join(filter(None, (php_version, entry["variant"], entry["os"])))
        php_images = self.registry.bases(f"php:{base_tag}")
        node_images = {}
        if entry["node"]:
            node_images = self.registry.bases(f'node:{entry["node"]}-{entry["os"] or "slim"}')
        names = entry["extensions"].split()
        core, packages, versions, xhprof = [], {}, {}, {}
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ResolutionError(f"Use plain extension names in matrix.json, got {name!r}")
            bundled = name in BUNDLED and not (
                name in MOVED_FROM_CORE and version_tuple(php_version) >= version_tuple("8.4")
            )
            if name not in PIE_PACKAGES and name != "xhprof" and not bundled:
                raise ResolutionError(f"No PIE package or source builder configured for {name}")
            if name == "opcache" and version_tuple(php_version) >= version_tuple("8.5"):
                raise ResolutionError("PHP 8.5+ opcache is built in; remove it from extensions")
            if bundled:
                core.append(name)
        pie, pie_bytes = self.pie() if any(name in PIE_PACKAGES for name in names) else ({}, b"")
        for name in names:
            if name in PIE_PACKAGES:
                packages[name] = self.extension(name, php_images, pie, pie_bytes)
                versions[name] = packages[name]["version"]
            elif name == "xhprof":
                xhprof = self.xhprof(php_version)
                versions[name] = xhprof["version"]
        composer = self.composer(entry)
        resolved = {
            "php_images": php_images, "node_images": node_images,
            "composer": composer, "pie": pie, "pie_packages": packages,
            "extensions_install": " ".join(core), "xhprof": xhprof,
        }
        inputs = {"schema": 2, "variant": entry, "resolved": resolved,
                  "extension_versions": versions, "sources": self.sources}
        build_inputs = canonical(inputs)
        fingerprint = sha256(build_inputs.encode())
        label_output = io.StringIO()
        writer = csv.writer(label_output, lineterminator="\n")
        writer.writerow([FINGERPRINT_LABEL + "=" + fingerprint])
        writer.writerow([INPUTS_LABEL + "=" + build_inputs])
        dependencies = canonical({
            key: value for key, value in resolved.items() if key not in ("php_images", "node_images")
        })
        # The action parses build arguments as CSV too. Quote the whole argument
        # so quotes around individual configure options in JSON arrays survive.
        argument_output = io.StringIO()
        csv.writer(argument_output, lineterminator="\n").writerow(["DEPENDENCIES=" + dependencies])
        return {**entry, "tagMinor": minor_tag, "tagPatch": patch_tag,
                "resolved": resolved, "dependencies": dependencies,
                "dependency_build_arg": argument_output.getvalue().rstrip("\n"), "fingerprint": fingerprint,
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
    except (ResolutionError, KeyError, TypeError, ValueError, OSError, tarfile.TarError) as exc:
        print(f"Dependency check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
