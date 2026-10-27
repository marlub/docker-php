"""Load and validate committed PHP image configuration."""

import json
import re
from pathlib import Path
from typing import Any, TypedDict


REVISION_LABEL = "org.opencontainers.image.revision"
VARIANT_LABEL = "io.github.marlub.docker-php.variant"
APK_ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}


class ConfigurationError(ValueError):
    """Committed image configuration is invalid or unsupported."""


class ImageVariant(TypedDict):
    minor: str
    php_version: str
    alpine: str
    php_image: str
    node_image: str
    node: str
    node_version: str
    extensions: str
    packages: str
    tagMinor: str
    tagPatch: str
    source_ref: str
    extension_versions: dict[str, str]
    xhprof: dict[str, str] | None
    composer_version: str


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"{path}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path}: expected a JSON object")
    return value


def _load_tool_images(dockerfile: Path) -> dict[str, str]:
    images = dict(re.findall(
        r'^ARG (PIE_IMAGE|COMPOSER_IMAGE|NODE_BASE_IMAGE)="([^"\n]+)"$',
        dockerfile.read_text(encoding="utf-8"),
        re.MULTILINE,
    ))
    if set(images) != {"PIE_IMAGE", "COMPOSER_IMAGE", "NODE_BASE_IMAGE"}:
        raise ConfigurationError("Missing pinned tool image arguments")
    for reference in images.values():
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", reference):
            raise ConfigurationError("Tool images must have version tags and digests")
    return images


def _validate_extensions(config: dict[str, Any], minor: str) -> None:
    names = config["extensions"].split()
    if len(set(names)) != len(names) or any(
        not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names
    ):
        raise ConfigurationError("Extensions must be unique module names")
    if minor == "8.5" and "opcache" in names:
        raise ConfigurationError("PHP 8.5 opcache is built in")
    for package in config["pie_packages"].values():
        if not re.fullmatch(r"\d+(?:\.\d+){1,3}", package["version"]):
            raise ConfigurationError("PIE versions must be exact stable pins")
        if "module_version" in package:
            module_version = package["module_version"]
            if not isinstance(module_version, str) or not re.fullmatch(
                r"\d+(?:\.\d+){1,3}", module_version
            ):
                raise ConfigurationError("PIE module versions must be exact stable pins")
    xhprof = config["xhprof"]
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", xhprof["tag"]) or not re.fullmatch(
        r"[a-f0-9]{40}", xhprof["commit"]
    ):
        raise ConfigurationError("XHProf requires a stable tag and full commit")


def _validate_package_locks(root: Path, alpine: str) -> None:
    for architecture, apk_architecture in APK_ARCHITECTURES.items():
        path = root / "build" / "apk" / alpine / f"{architecture}.json"
        lock = _read_json_object(path)
        if (
            lock["alpine"] != alpine
            or lock["architecture"] != apk_architecture
            or not lock["packages"]
        ):
            raise ConfigurationError("APK lock release/architecture mismatch")
        for name, version in lock["packages"].items():
            if not re.fullmatch(r"[a-zA-Z0-9+_.-]+", name) or not re.fullmatch(
                r"[a-zA-Z0-9+_.~-]+-r\d+", version
            ):
                raise ConfigurationError("APK packages require exact revision pins")


def _manifest_variants(
    root: Path, path: Path, tools: dict[str, str], source_ref: str
) -> list[ImageVariant]:
    config = _read_json_object(path)
    php = re.fullmatch(
        r"php:(?P<version>(?P<minor>8\.\d+)\.\d+)-fpm-alpine"
        r"(?P<alpine>\d+\.\d+)@sha256:[a-f0-9]{64}",
        config["image"],
    )
    if php is None or php["minor"] != path.stem:
        raise ConfigurationError("PHP manifest does not match its image")
    minor, alpine = php["minor"], php["alpine"]
    _validate_extensions(config, minor)
    _validate_package_locks(root, alpine)

    node_image = tools["NODE_BASE_IMAGE"]
    node = re.fullmatch(
        r"node:(24\.\d+\.\d+)-alpine" + re.escape(alpine) + r"@sha256:[a-f0-9]{64}",
        node_image,
    )
    if node is None:
        raise ConfigurationError("Node must stay on major 24 and the PHP Alpine release")

    extensions = config["extensions"].split()
    extension_versions = {
        name: package.get("module_version", package["version"])
        for name, package in config["pie_packages"].items()
        if name in extensions
    }
    composer_version = tools["COMPOSER_IMAGE"].split("@")[0].rsplit(":", 1)[1]
    variants: list[ImageVariant] = []
    for node_major in ("", "24"):
        suffix = "-fpm-alpine" + (f"-node{node_major}" if node_major else "")
        variants.append({
            "minor": minor,
            "php_version": php["version"],
            "alpine": alpine,
            "php_image": config["image"],
            "node_image": node_image,
            "node": node_major,
            "node_version": node[1] if node_major else "",
            "extensions": config["extensions"],
            "packages": config.get("packages", ""),
            "tagMinor": minor + suffix,
            "tagPatch": php["version"] + suffix,
            "source_ref": source_ref,
            "extension_versions": dict(extension_versions),
            "xhprof": config["xhprof"] if "xhprof" in extensions else None,
            "composer_version": composer_version.removesuffix("-bin"),
        })
    return variants


def load_image_variants(root: Path, source_ref: str) -> list[ImageVariant]:
    """Resolve the build matrix without Git, Docker, or registry access."""
    tools = _load_tool_images(root / "Dockerfile")
    variants: list[ImageVariant] = []
    for path in sorted((root / "build" / "php").glob("*.json")):
        try:
            variants.extend(_manifest_variants(root, path, tools, source_ref))
        except (ConfigurationError, KeyError, TypeError, AttributeError) as error:
            raise ConfigurationError(f"{path}: {error}") from error
    if not variants:
        raise ConfigurationError("No PHP manifests found")
    return variants
