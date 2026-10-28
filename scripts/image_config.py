"""Read Bake configuration and extract CI/publication expectations."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, TypedDict


REVISION_LABEL = "org.opencontainers.image.revision"
VARIANT_LABEL = "io.github.marlub.docker-php.variant"


class ConfigurationError(ValueError):
    """Committed image configuration is invalid or unsupported."""


class ImageVariant(TypedDict):
    bake_file: str
    bake_target: str
    bake_args: dict[str, str]
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


def _extension_versions(args: dict[str, str], minor: str) -> dict[str, str]:
    if any(key in args for key in ("NODE_BASE_IMAGE", "PIE_IMAGE", "COMPOSER_IMAGE")):
        raise ConfigurationError("Shared tool image pins belong in the Dockerfile")
    names = args["EXTENSIONS"].split()
    if len(set(names)) != len(names) or any(
        not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names
    ):
        raise ConfigurationError("Extensions must be unique module names")
    if tuple(map(int, minor.split("."))) >= (8, 5) and "opcache" in names:
        raise ConfigurationError("PHP 8.5+ opcache is built in")
    expected = {}
    for name in names:
        # Selection and compiler switches belong to Bake. Here we only need
        # the expected versions for selected external modules.
        if f"BUILD_{name.upper()}" not in args:
            continue
        if name == "xhprof":
            if not re.fullmatch(r"v?\d+\.\d+\.\d+", args.get("XHPROF_TAG", "")) or not re.fullmatch(
                r"[a-f0-9]{40}", args.get("XHPROF_COMMIT", "")):
                raise ConfigurationError("XHProf requires a stable tag and full commit")
            continue
        version = args.get(f"{name.upper()}_VERSION", "")
        if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", version):
            raise ConfigurationError(f"Missing or invalid PIE pin: {name}")
        module = args.get(f"{name.upper()}_MODULE_VERSION", version)
        if not isinstance(module, str) or not re.fullmatch(r"\d+(?:\.\d+){1,3}", module):
            raise ConfigurationError(f"Invalid reported module version: {name}")
        expected[name] = module
    return expected


def resolve_bake(root: Path) -> dict[str, dict[str, Any]]:
    """Use Buildx's HCL evaluator; --print needs neither a daemon nor network."""
    paths = sorted((root / "build/php").glob("*.hcl"))
    if not paths:
        raise ConfigurationError("No PHP Bake files found")
    command = ["docker", "buildx", "bake", "-f", "docker-bake.hcl"]
    targets = []
    for path in paths:
        if not re.fullmatch(r"8\.\d+", path.stem):
            raise ConfigurationError(f"Invalid PHP minor file: {path.name}")
        command += ["-f", path.relative_to(root).as_posix()]
        target = "php-" + path.stem.replace(".", "-")
        targets += [target, target + "-node24"]
    try:
        result = subprocess.run(
            [*command, "--print", *targets], cwd=root, text=True, capture_output=True,
            env={**os.environ, "BUILDX_BAKE_DISABLE_VARS_ENV_LOOKUP": "1"}, check=True,
        )
        return json.loads(result.stdout)["target"]
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise ConfigurationError(f"Cannot resolve Bake configuration: {detail}") from error


def load_image_variants(root: Path, source_ref: str) -> list[ImageVariant]:
    """Read the resolved Bake targets and validate publication/test expectations."""
    tools = _load_tool_images(root / "Dockerfile")
    resolved = resolve_bake(root)
    variants = []
    for path in sorted((root / "build/php").glob("*.hcl")):
        try:
            base_target = "php-" + path.stem.replace(".", "-")
            plain = resolved[base_target]
            args = plain["args"]
            php = re.fullmatch(
                r"php:(?P<version>(?P<minor>8\.\d+)\.\d+)-fpm-alpine"
                r"(?P<alpine>\d+\.\d+)@sha256:[a-f0-9]{64}", args["PHP_BASE_IMAGE"],
            )
            if php is None or php["minor"] != path.stem:
                raise ConfigurationError("PHP Bake file does not match its image")
            minor, alpine = php["minor"], php["alpine"]
            expected = _extension_versions(args, minor)
            node_image = tools["NODE_BASE_IMAGE"]
            node = re.fullmatch(r"node:(24\.\d+\.\d+)-alpine" + re.escape(alpine) + r"@sha256:[a-f0-9]{64}", node_image)
            if node is None:
                raise ConfigurationError("Node must stay on major 24 and the PHP Alpine release")
            for node_major in ("", "24"):
                target = base_target + ("-node24" if node_major else "")
                config = resolved[target]
                if config["args"] != args or config["target"] != ("php-node" if node_major else "php"):
                    raise ConfigurationError("Plain and Node targets must share their PHP configuration")
                suffix = "-fpm-alpine" + ("-node24" if node_major else "")
                variants.append({
                    "bake_file": path.relative_to(root).as_posix(), "bake_target": target,
                    "bake_args": dict(args), "minor": minor, "php_version": php["version"],
                    "alpine": alpine, "php_image": args["PHP_BASE_IMAGE"],
                    "node_image": node_image, "node": node_major,
                    "node_version": node[1] if node_major else "",
                    "extensions": args["EXTENSIONS"], "packages": args.get("PACKAGES", ""),
                    "tagMinor": minor + suffix, "tagPatch": php["version"] + suffix,
                    "source_ref": source_ref, "extension_versions": dict(expected),
                    "xhprof": {"tag": args["XHPROF_TAG"], "commit": args["XHPROF_COMMIT"]} if "xhprof" in args["EXTENSIONS"].split() else None,
                    "composer_version": tools["COMPOSER_IMAGE"].split("@")[0].rsplit(":", 1)[1].removesuffix("-bin"),
                })
        except (KeyError, TypeError, AttributeError, ConfigurationError) as error:
            raise ConfigurationError(f"{path}: {error}") from error
    return variants
