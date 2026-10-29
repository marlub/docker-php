"""Read Bake configuration and extract CI/publication expectations."""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, TypedDict


REVISION_LABEL = "org.opencontainers.image.revision"
VARIANT_LABEL = "io.github.marlub.docker-php.variant"
BASE_TARGET_LABEL = "io.github.marlub.docker-php.base-target"


class ConfigurationError(ValueError):
    """Committed image configuration is invalid or unsupported."""


class ImageVariant(TypedDict):
    bake_file: str
    bake_target: str
    bake_args: dict[str, str]
    minor: str
    php_version: str
    base_target: str
    distribution: str
    distribution_release: str
    dockerfile: str
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
    if not {"PIE_IMAGE", "COMPOSER_IMAGE"} <= images.keys():
        raise ConfigurationError("Missing pinned tool image arguments")
    for reference in images.values():
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", reference):
            raise ConfigurationError("Tool images must have version tags and digests")
    return images


def _extension_versions(args: dict[str, str], minor: str) -> dict[str, str]:
    if any(key in args for key in ("PIE_IMAGE", "COMPOSER_IMAGE")):
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


def variant_order(variant: ImageVariant) -> tuple[int, int]:
    return (1, int(variant["node"])) if variant["node"] else (0, 0)


def distribution_from_tag(tag: str, *, node: bool = False) -> tuple[str, str]:
    alpine = re.fullmatch(r"alpine(\d+\.\d+)", tag)
    if alpine:
        return "alpine", alpine[1]
    if node and tag.endswith("-slim"):
        tag = tag.removesuffix("-slim")
    if re.fullmatch(r"[a-z][a-z0-9]*", tag) and not tag.startswith("alpine"):
        return "debian", tag
    raise ConfigurationError(f"Unsupported distribution tag: {tag}")


def resolve_bake_config(root: Path) -> dict[str, Any]:
    """Resolve the explicit per-minor CI groups without a daemon or network."""
    paths = sorted((root / "build/php").glob("*.hcl"))
    if not paths:
        raise ConfigurationError("No PHP Bake files found")
    command = ["docker", "buildx", "bake", "-f", "docker-bake.hcl"]
    groups = []
    for path in paths:
        if not re.fullmatch(r"8\.\d+", path.stem):
            raise ConfigurationError(f"Invalid PHP minor file: {path.name}")
        command += ["-f", path.relative_to(root).as_posix()]
        groups.append("ci-" + path.stem.replace(".", "-"))
    try:
        result = subprocess.run(
            [*command, "--print", *groups], cwd=root, text=True, capture_output=True,
            env={**os.environ, "BUILDX_BAKE_DISABLE_VARS_ENV_LOOKUP": "1"}, check=True,
        )
        document = json.loads(result.stdout)
        if not isinstance(document.get("target"), dict) or not isinstance(document.get("group"), dict):
            raise ConfigurationError("Missing Bake CI groups or targets")
        return document
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
        detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        raise ConfigurationError(f"Cannot resolve Bake configuration: {detail}") from error


def resolve_bake(root: Path) -> dict[str, dict[str, Any]]:
    return resolve_bake_config(root)["target"]


def _local_tag(config: dict[str, Any]) -> str:
    tags = config.get("tags", [])
    if len(tags) != 1 or not re.fullmatch(r"docker-php:[a-zA-Z0-9_][a-zA-Z0-9_.-]*", tags[0]):
        raise ConfigurationError("Each CI target requires one unique docker-php:<variant> tag")
    return tags[0].removeprefix("docker-php:")


def load_image_variants(root: Path, source_ref: str) -> list[ImageVariant]:
    """Read publishable families and all their selected Node image configurations."""
    document = resolve_bake_config(root)
    resolved = document["target"]
    variants: list[ImageVariant] = []
    seen_targets: set[str] = set()
    seen_tags: set[str] = set()
    tools_by_recipe = {}
    for path in sorted((root / "build/php").glob("*.hcl")):
        try:
            names = document["group"]["ci-" + path.stem.replace(".", "-")]["targets"]
            if not names or len(set(names)) != len(names):
                raise ConfigurationError("CI groups must contain distinct publishable targets")
            family_nodes: dict[str, set[str]] = {}
            for target in names:
                if target in seen_targets:
                    raise ConfigurationError("A target cannot belong to multiple CI groups")
                seen_targets.add(target)
                config = resolved[target]
                base_target = config.get("labels", {}).get(BASE_TARGET_LABEL)
                if base_target not in names:
                    raise ConfigurationError("The canonical plain target must belong to the same CI group")
                plain = resolved[base_target]
                if plain.get("target") != "php" or plain.get("labels", {}).get(BASE_TARGET_LABEL) != base_target:
                    raise ConfigurationError("Invalid canonical plain target")
                recipe = config.get("dockerfile", "Dockerfile")
                if not isinstance(recipe, str) or not (root / recipe).resolve().is_relative_to(root.resolve()):
                    raise ConfigurationError("Dockerfiles must be inside the build context")
                if config.get("context", ".") != "." or "dockerfile-inline" in config:
                    raise ConfigurationError("CI targets require the repository build context and a Dockerfile")
                if recipe != plain.get("dockerfile", "Dockerfile"):
                    raise ConfigurationError("Family targets must share their Dockerfile")
                args = config["args"]
                shared = {key: value for key, value in args.items() if key != "NODE_BASE_IMAGE"}
                plain_shared = {key: value for key, value in plain["args"].items() if key != "NODE_BASE_IMAGE"}
                if shared != plain_shared:
                    raise ConfigurationError("Family targets must share their PHP configuration")
                php = re.fullmatch(
                    r"php:(?P<version>(?P<minor>8\.\d+)\.\d+)-fpm-(?P<os>[^@]+)@sha256:[a-f0-9]{64}",
                    args["PHP_BASE_IMAGE"],
                )
                if php is None or php["minor"] != path.stem:
                    raise ConfigurationError("PHP Bake file does not match its image")
                minor = php["minor"]
                distribution, release = distribution_from_tag(php["os"])
                expected = _extension_versions(shared, minor)
                if recipe not in tools_by_recipe:
                    tools_by_recipe[recipe] = _load_tool_images(root / recipe)
                tools = tools_by_recipe[recipe]
                node_major = node_version = node_image = ""
                if target != base_target:
                    if config.get("target") != "php-node":
                        raise ConfigurationError("Derived CI targets must use the php-node stage")
                    node_image = args.get("NODE_BASE_IMAGE", tools.get("NODE_BASE_IMAGE", ""))
                    node = re.fullmatch(
                        r"node:(?P<version>(?P<major>[1-9]\d*)\.\d+\.\d+)-(?P<os>[^@]+)@sha256:[a-f0-9]{64}",
                        node_image,
                    )
                    if node is None or distribution_from_tag(node["os"], node=True) != (distribution, release):
                        raise ConfigurationError("Node must have a pinned version and match the PHP distribution/release")
                    node_major, node_version = node["major"], node["version"]
                majors = family_nodes.setdefault(base_target, set())
                if node_major in majors:
                    raise ConfigurationError("Duplicate Node major or plain image in a family")
                majors.add(node_major)
                base_tag = _local_tag(plain)
                if not base_tag.startswith(minor + "-fpm-"):
                    raise ConfigurationError("Plain image tag must identify its PHP minor and FPM distribution")
                tag = _local_tag(config)
                if tag != base_tag + ("-node" + node_major if node_major else ""):
                    raise ConfigurationError("Node image tag must identify its configured major")
                patch_tag = php["version"] + tag[len(minor):]
                if tag in seen_tags or patch_tag in seen_tags:
                    raise ConfigurationError("Conflicting publication tags")
                seen_tags.update((tag, patch_tag))
                variants.append({
                    "bake_file": path.relative_to(root).as_posix(), "bake_target": target,
                    "bake_args": dict(args), "minor": minor, "php_version": php["version"],
                    "base_target": base_target, "distribution": distribution,
                    "distribution_release": release, "dockerfile": recipe,
                    "php_image": args["PHP_BASE_IMAGE"], "node_image": node_image,
                    "node": node_major, "node_version": node_version,
                    "extensions": args["EXTENSIONS"], "packages": args.get("PACKAGES", ""),
                    "tagMinor": tag, "tagPatch": patch_tag, "source_ref": source_ref,
                    "extension_versions": dict(expected),
                    "xhprof": {"tag": args["XHPROF_TAG"], "commit": args["XHPROF_COMMIT"]} if "xhprof" in args["EXTENSIONS"].split() else None,
                    "composer_version": tools["COMPOSER_IMAGE"].split("@")[0].rsplit(":", 1)[1].removesuffix("-bin"),
                })
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise ConfigurationError(f"{path}: {error}") from error
    return sorted(variants, key=lambda v: (tuple(map(int, v["minor"].split("."))), v["base_target"], variant_order(v)))
