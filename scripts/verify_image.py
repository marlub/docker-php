#!/usr/bin/env python3
"""Check that the tested container matches its dependency labels and snapshot."""

import argparse
import json
import re
import subprocess

from select_images import FINGERPRINT_LABEL, INPUTS_LABEL


def verify(image, build_inputs, fingerprint, run=subprocess.check_output):
    labels = json.loads(run([
        "docker", "image", "inspect", image, "--format", "{{json .Config.Labels}}",
    ], text=True))
    if labels.get(FINGERPRINT_LABEL) != fingerprint or labels.get(INPUTS_LABEL) != build_inputs:
        raise ValueError("Test image dependency labels do not match the resolved inputs")
    inputs = json.loads(build_inputs)
    expected = inputs["extension_versions"]
    variant = inputs["variant"]
    php_version = run(["docker", "run", "--rm", image, "php", "-r", "echo PHP_VERSION;"], text=True).strip()
    if php_version != f'{variant["major"]}.{variant["minor"]}.{variant["patch"]}':
        raise ValueError(f"Unexpected PHP version: {php_version}")
    if expected:
        command = ["docker", "run", "--rm"]
        for name in expected:
            command.extend(["-e", f"PHP_EXTENSION_{name.upper()}=true"])
        command.extend([image, "php", "-r",
                        "echo json_encode(array_combine(array_slice($argv, 1), "
                        "array_map('phpversion', array_slice($argv, 1))));", "--", *expected])
        actual = json.loads(run(command, text=True))
        if actual != expected:
            raise ValueError(f"Installed extension versions differ: expected {expected}, got {actual}")
    composer = inputs["resolved"]["composer_version"]
    if composer:
        output = run(["docker", "run", "--rm", image, "composer", "--version", "--no-ansi"], text=True)
        match = re.search(r"Composer version (\S+)", output)
        if not match or match[1] != composer:
            raise ValueError(f"Unexpected Composer version: {output.strip()}")
    print("Installed versions and dependency labels match the build snapshot")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--fingerprint", required=True)
    args = parser.parse_args()
    verify(args.image, args.inputs, args.fingerprint)


if __name__ == "__main__":
    main()
