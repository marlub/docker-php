#!/usr/bin/env python3
"""Build selected variants on one builder and transfer independently tested candidates."""

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from typing import Mapping

from image_config import ImageVariant
from plan_ci import load_variants, prepare_build_group, run_git

ROOT = Path(__file__).resolve().parent.parent
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
PACKAGE_CHECKS = {"alpine": "tests/test_apk_lock.php"}


@dataclass(frozen=True)
class CachePolicy:
    image: str
    scope: str = ""
    merged_prs: tuple[int, ...] = ()

    @property
    def registry(self) -> str:
        return self.image.split("/", 1)[0]

    def settings(self, tag: str, arch: str, *, no_cache: bool = False) -> list[str]:
        main = f"{self.image}:main-{tag}-{arch}"
        write = f"{self.image}:{self.scope}-{tag}-{arch}" if self.scope else ""
        settings = []
        if not no_cache:
            settings.append(f"*.cache-from=type=registry,ref={main}")
            settings += [f"*.cache-from+=type=registry,ref={self.image}:pr-{number}-{tag}-{arch}"
                         for number in self.merged_prs]
            if write and write != main:
                settings.append(f"*.cache-from+=type=registry,ref={write}")
        if write:
            settings.append(f"*.cache-to=type=registry,ref={write},mode=max,ignore-error=true")
        return settings


def cache_policy(image: str, publish: bool, env: Mapping[str, str], merged_prs: str = "[]") -> CachePolicy:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]+", image) or "/" not in image:
        raise ValueError("Invalid cache repository")
    scope = ""
    numbers: tuple[int, ...] = ()
    if env.get("ACTOR") != "dependabot[bot]":
        if publish and env.get("REF") == "refs/heads/main" and env.get("EVENT_NAME") in ("push", "workflow_dispatch"):
            scope = "main"
            try:
                values = json.loads(merged_prs)
                if not isinstance(values, list) or any(type(n) is not int or not 0 < n <= 9007199254740991 for n in values):
                    raise ValueError("Expected positive PR numbers")
                numbers = tuple(sorted(set(values)))
            except (ValueError, TypeError):
                print("::warning::Invalid merged PR cache input; using main cache only", file=sys.stderr)
        elif not publish and env.get("EVENT_NAME") == "pull_request" and env.get("PR_HEAD_REPOSITORY") == env.get("REPOSITORY"):
            number = env.get("PR_NUMBER", "")
            if not re.fullmatch(r"[1-9][0-9]*", number):
                raise ValueError("Invalid PR cache number")
            scope = "pr-" + number
    return CachePolicy(image, scope, numbers)


def bake_command(entry: ImageVariant, request: dict, directory: Path, *, fresh_php: bool) -> list[str]:
    target = entry["bake_target"]
    command = ["docker", "buildx", "bake", "-f", "docker-bake.hcl", "-f", entry["bake_file"],
               "--builder", request["builder"], "--progress", "plain"]
    if request["no_cache"] and not fresh_php:
        command.append("--no-cache")
    if request["publish"]:
        command += ["--allow", f"fs.write={directory}"]
    settings = [f"*.platform=linux/{request['arch']}", f"*.tags=docker-php-ci:{entry['tagMinor']}",
                "*.output=type=docker"]
    if request["publish"]:
        settings.append(f"*.output+=type=oci,dest={directory / 'image.tar'}")
    settings += CachePolicy(**request["cache"]).settings(entry["tagMinor"], request["arch"], no_cache=request["no_cache"])
    settings += [f"*.labels.io.github.marlub.docker-php.variant={entry['tagMinor']}",
                 f"*.labels.org.opencontainers.image.revision={request['source_ref']}"]
    for setting in settings:
        command += ["--set", setting]
    return command + [target]


class Commands:
    def __init__(self, root: Path):
        self.root = root

    def run(self, command: list[str], log: Path) -> None:
        """Stream output to the step log while retaining each phase's output."""
        env = {**os.environ, "BUILDX_BAKE_DISABLE_VARS_ENV_LOOKUP": "1"}
        with log.open("w") as output, subprocess.Popen(
            command, cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ) as process:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    output.write(line)
                    print(line, end="", flush=True)
                if process.wait():
                    raise subprocess.CalledProcessError(process.returncode, command)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise

    def capture(self, command: list[str]) -> str:
        return subprocess.check_output(command, cwd=self.root, text=True).strip()


def inspect_image(commands: Commands, image: str) -> str:
    config = commands.capture(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    if DIGEST.fullmatch(config) is None:
        raise ValueError("Invalid loaded image configuration digest")
    return config


def test_image(commands: Commands, entry: ImageVariant, image: str, directory: Path) -> None:
    check = PACKAGE_CHECKS.get(entry["distribution"])
    if check is None:
        raise ValueError(f"Unsupported package-check backend: {entry['distribution']}")
    volume = f"{commands.root}:/work:ro"
    commands.run(["docker", "run", "--rm", "--entrypoint", "php", "-v", volume, image, "/work/" + check], directory / "packages.log")
    commands.run(["docker", "run", "--rm", "--entrypoint", "sh", "-v", volume, image,
                  "/work/tests/test_installer.sh"], directory / "installer.log")
    commands.run(["tests/test_image.sh", f"--node={entry['node']}", f"--extensions={entry['extensions']}",
                  f"--image={image}"], directory / "image-tests.log")
    commands.run([sys.executable, "tests/verify_image.py", "--image", image, "--entry", json.dumps(entry)], directory / "versions.log")


def transfer_image(commands: Commands, image: str, destination: str, directory: Path, config_id: str) -> str:
    oci = directory / "oci"
    oci.mkdir()
    with tarfile.open(directory / "image.tar") as archive:
        archive.extractall(oci, filter="data")
    index = json.loads((oci / "index.json").read_text())
    if not isinstance(index, dict):
        raise ValueError("Malformed candidate image index")
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1 or not isinstance(manifests[0], dict):
        raise ValueError("Ambiguous candidate image")
    digest = manifests[0].get("digest", "")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError("Malformed candidate image digest")
    manifest = json.loads((oci / "blobs/sha256" / digest.removeprefix("sha256:")).read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
        raise ValueError("Malformed candidate image manifest")
    if manifest["config"].get("digest") != config_id or inspect_image(commands, image) != config_id:
        raise ValueError("OCI candidate does not match the tested image")
    commands.run(["regctl", "image", "copy", f"ocidir://{oci}@{digest}", f"{destination}@{digest}"], directory / "transfer.log")
    return digest


def clean_exports(directory: Path) -> None:
    (directory / "image.tar").unlink(missing_ok=True)
    shutil.rmtree(directory / "oci", ignore_errors=True)


def write_bundle(path: Path, bundle: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(bundle, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def run_group(request: dict, root: Path, directory: Path, commands: Commands | None = None) -> dict:
    commands = commands or Commands(root)
    directory.mkdir(parents=True, exist_ok=True)
    bundle = {key: request[key] for key in ("group_id", "source_ref", "arch", "run_attempt")}
    bundle["results"] = {v["tagMinor"]: {"status": "not-run"} for v in request["variants"]}
    result_path = directory / "results.json"
    write_bundle(result_path, bundle)
    fresh_php = False
    try:
        for entry in request["variants"]:
            tag = entry["tagMinor"]
            work = directory / tag
            work.mkdir(exist_ok=True)
            clean_exports(work)
            started = time.monotonic()
            result = bundle["results"][tag]
            result.update(status="failure", stage="build", built=False)
            print(f"::group::{tag} ({request['arch']})", flush=True)
            try:
                if entry["distribution"] not in PACKAGE_CHECKS:
                    raise ValueError(f"Unsupported package-check backend: {entry['distribution']}")
                policy = CachePolicy(**request["cache"])
                print("Cache settings: " + "; ".join(policy.settings(tag, request["arch"], no_cache=request["no_cache"])), flush=True)
                commands.run(bake_command(entry, request, work, fresh_php=fresh_php), work / "build.log")
                image = "docker-php-ci:" + tag
                config_id = inspect_image(commands, image)
                result["built"] = True
                fresh_php = True
                result["stage"] = "test"
                test_image(commands, entry, image, work)
                if request["publish"]:
                    result["stage"] = "transfer"
                    digest = transfer_image(commands, image, request["image"], work, config_id)
                    result["digest"] = digest
                result.update(status="success", stage="complete")
            except (subprocess.CalledProcessError, OSError, ValueError, RuntimeError) as error:
                result["error"] = str(error)
                print(f"::error::{tag}: {error}", flush=True)
            finally:
                result["seconds"] = round(time.monotonic() - started, 1)
                clean_exports(work)
                write_bundle(result_path, bundle)
                print("::endgroup::", flush=True)
    finally:
        # Keep completed siblings available after unexpected errors; cancellation still gates upload/publication.
        write_bundle(result_path, bundle)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a") as output:
                output.write(f"### {request['group_id']} ({request['arch']})\n\n")
                output.write("| Variant | Result | Last stage | Seconds |\n| --- | --- | --- | --- |\n")
                for tag, result in bundle["results"].items():
                    output.write(f"| {tag} | {result['status']} | {result.get('stage', '')} | {result.get('seconds', '')} |\n")
                output.write("\n")
                policy = CachePolicy(**request["cache"])
                for tag in bundle["results"]:
                    output.write(f"#### Cache settings: {tag}\n\n")
                    if request["no_cache"]:
                        output.write("Remote imports disabled for forced rebuild.\n\n")
                    for setting in policy.settings(tag, request["arch"], no_cache=request["no_cache"]):
                        output.write(f"- `{setting}`\n")
                    output.write("\n")
    return bundle


def selected_digest(bundle: dict, entry: ImageVariant, arch: str, attempt: str) -> str:
    if not isinstance(bundle, dict) or not isinstance(bundle.get("results"), dict):
        raise ValueError("Malformed build result bundle")
    expected = {"group_id": entry["base_target"], "source_ref": entry["source_ref"], "arch": arch, "run_attempt": attempt}
    if any(bundle.get(key) != value for key, value in expected.items()):
        raise ValueError("Build result bundle identity mismatch")
    result = bundle["results"].get(entry["tagMinor"], {})
    if not isinstance(result, dict):
        raise ValueError("Malformed variant result")
    digest = result.get("digest", "")
    if result.get("status") != "success" or not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError(f"No successful tested candidate for {entry['tagMinor']} on {arch}")
    return digest


def prepare_request(root: Path, env: Mapping[str, str]) -> tuple[dict, Path]:
    source = run_git(root, "rev-parse", "HEAD").strip()
    if env["SOURCE_REF"] != source:
        raise ValueError("Build source does not match checkout")
    variants = prepare_build_group(json.loads(env["VARIANTS"]), load_variants(root))
    if env["ARCH"] not in ("amd64", "arm64") or not re.fullmatch(r"[1-9][0-9]*", env["GITHUB_RUN_ATTEMPT"]):
        raise ValueError("Invalid build architecture or workflow attempt")
    policy = cache_policy(env["CACHE_IMAGE"], env["PUBLISH"] == "true", env, env.get("MERGED_PRS", "[]"))
    request = {"group_id": variants[0]["base_target"], "source_ref": source, "arch": env["ARCH"],
               "run_attempt": env["GITHUB_RUN_ATTEMPT"], "variants": variants, "cache": asdict(policy),
               "builder": env["BUILDER"], "publish": env["PUBLISH"] == "true", "no_cache": env["NO_CACHE"] == "true",
               "image": env["IMAGE"]}
    directory = Path(env["RUNNER_TEMP"]) / "php-build-groups" / request["group_id"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").unlink(missing_ok=True)
    (directory / "request.json").write_text(json.dumps(request) + "\n")
    return request, directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("prepare")
    run = sub.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    extract = sub.add_parser("extract")
    extract.add_argument("--entry", required=True)
    extract.add_argument("--bundles", type=Path, required=True)
    extract.add_argument("--attempt", required=True)
    extract.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "prepare":
            request, directory = prepare_request(ROOT, os.environ)
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                for key, value in {"id": request["group_id"], "directory": str(directory),
                                   "request": str(directory / "request.json"), "registry": CachePolicy(**request["cache"]).registry,
                                   "write": str(bool(request["cache"]["scope"])).lower()}.items():
                    output.write(f"{key}={value}\n")
        elif args.mode == "run":
            request = json.loads(args.request.read_text())
            request["variants"] = prepare_build_group(request["variants"], load_variants(ROOT))
            if request["group_id"] != request["variants"][0]["base_target"]:
                raise ValueError("Build request family mismatch")
            if (request["source_ref"] != request["variants"][0]["source_ref"] or
                request["arch"] not in ("amd64", "arm64") or
                not re.fullmatch(r"[1-9][0-9]*", request["run_attempt"]) or
                type(request["publish"]) is not bool or type(request["no_cache"]) is not bool):
                raise ValueError("Invalid build request identity or options")
            def cancelled(signum, frame):
                raise KeyboardInterrupt("Build cancelled")
            signal.signal(signal.SIGTERM, cancelled)
            bundle = run_group(request, ROOT, args.request.parent)
            return int(any(result["status"] != "success" for result in bundle["results"].values()))
        else:
            entry = prepare_build_group([json.loads(args.entry)], load_variants(ROOT))[0]
            digests = {arch: selected_digest(json.loads((args.bundles / arch / "results.json").read_text()), entry, arch, args.attempt)
                       for arch in ("amd64", "arm64")}
            args.out.mkdir(parents=True, exist_ok=True)
            for arch, digest in digests.items():
                (args.out / (arch + ".digest")).write_text(digest + "\n")
    except KeyboardInterrupt:
        print("Build group cancelled", file=sys.stderr)
        return 130
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Build group failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
