# Building and maintaining the images

This guide covers local builds, dependency updates, and publication. For available images and runtime configuration, see the [README](../README.md).

## Build and test locally

Run commands from the repository root. Local builds require Docker with Buildx, access to the Docker daemon, `jq`, and network access. The Python checks and image selection tools also require Python 3 and Git history. Only Alpine PHP base images are supported.

Run the unit tests:

```sh
python3 -m unittest discover -s tests -v
```

Build a PHP 8.4 image using its committed base image and extension pins:

```sh
php_manifest=build/php/8.4.json
extensions=$(jq -r '.extensions' "$php_manifest")

docker buildx build --load --target php --tag docker-php:local \
  --build-arg PHP_BASE_IMAGE="$(jq -r '.image' "$php_manifest")" \
  --build-arg EXTENSIONS="$extensions" \
  --build-arg PACKAGES="$(jq -r '.packages' "$php_manifest")" \
  .
```

Choose another manifest to build a different PHP minor. To include Node.js, use `--target php-node`; its base image is pinned in the Dockerfile. Custom bases can be supplied through `PHP_BASE_IMAGE` and `NODE_BASE_IMAGE`, and must match the supported PHP, Node, and Alpine versions. Changing `NODE_VERSION` alone does not select a different Node image.

Test the resulting image:

```sh
docker run --rm --entrypoint php -v "$PWD:/work:ro" \
  docker-php:local /work/tests/test_apk_lock.php
docker run --rm --entrypoint sh -v "$PWD:/work:ro" \
  docker-php:local /work/tests/test_installer.sh
scripts/test_image.sh --image=docker-php:local --extensions="$extensions"
```

For a Node variant, also pass `--node=24` to `scripts/test_image.sh`. The image checks cover disabled extension defaults, independent activation, combined loading, extension functionality, and container user settings. CI also verifies installed versions against the committed pins.

Builds use the host architecture by default. Add `--platform linux/amd64` or `--platform linux/arm64` to select one explicitly; building or running another architecture requires emulation. CI tests both architectures on native runners.

### Preview the build matrix

Generate the complete matrix, select a PHP minor, or select committed changes against a Git reference:

```sh
python3 scripts/select_images.py --out /tmp/php-build-matrix.json
python3 scripts/select_images.py --php 8.4 --out /tmp/php-build-matrix.json
python3 scripts/select_images.py --base-ref origin/main --out /tmp/php-build-matrix.json
```

Each command replaces the output file. Selection needs Git history but does not require Docker or network access. Fetch the comparison reference beforehand if it is unavailable locally. Without a selection option, all variants are included.

## Update dependencies and extensions

The build inputs live in these files:

| File | Contents |
| --- | --- |
| `build/php/<minor>.json` | PHP base image, extension list, PIE package pins and configure options, and XHProf source tag and commit |
| `build/apk/<release>/<architecture>.json` | Additional runtime packages and their exact versions, including transitive dependencies |
| `Dockerfile` | PIE, Composer, and Node base image pins |
| `scripts/alpine_packages.sh` | Build and runtime package profiles used during installation and lock refreshes |

PHP bases specify an Alpine release and an immutable image digest. Unchanged packages inherited from the base are covered by that digest. Additional runtime packages are checked against the architecture's lockfile after build dependencies are removed. Missing pins, unavailable versions, and unexpected runtime dependencies fail the build.

Temporary build tools and development packages use the configured Alpine repositories and are removed after compilation. Their versions are not pinned, so rebuilding the same committed inputs can use newer build tools. Normal builds use the committed extension and runtime package pins without refreshing them.

### Add or update an extension

1. Update the extension list and package or source pin in each affected PHP manifest. Bundled extensions use Docker's PHP helpers; external extensions use PIE. XHProf is built from its pinned Git commit.
2. For PIE packages, set `version` to the exact package release. If the PHP module reports a different version, also set `module_version`; otherwise verification defaults to `version`. Review both values when updating Protobuf, which uses the third-party `extport/protobuf` mirror.
3. Add any system dependencies to the package profiles and refresh both architecture locks when runtime dependencies change.
4. Update the README and Compose examples for new extensions, and add a functional check to `scripts/test_image.sh`.
5. Run the local checks and require successful CI builds on both architectures before publication.

All extensions added by this project remain disabled by default. The entrypoint enables them with `PHP_EXTENSION_<NAME>=true` or `1`. Keep the PHP 8.5 built-in OPcache exception when updating extension lists.

### Refresh runtime package locks

Refresh the locks after adding runtime dependencies or changing the Alpine release:

```sh
python3 scripts/refresh_apk_locks.py --platform linux/amd64
python3 scripts/refresh_apk_locks.py --platform linux/arm64
```

The refresh runs disposable PHP base containers and resolves the runtime package profile, including its dependencies. It requires Docker, network access, and a native runner or emulation for the selected architecture. It does not compile extensions or build the final images.

Review and commit both lockfiles, then rebuild and test the affected images. Extra packages requested through the `PACKAGES` build argument must also be covered by the runtime lock.

Alpine repositories can remove historical package versions. If a pin becomes unavailable, refresh and review the locks before rebuilding.

## Automated updates and publication

The hosted Renovate app proposes updates to base images, extension versions, XHProf source pins, PIE and Composer images, GitHub Actions, and Alpine runtime packages. Stable patch, minor, package revision, and digest updates can merge after required checks pass; major updates require review. PHP minor versions and Node major 24 stay fixed until explicitly changed.

Pull requests test the affected variants without publishing. Build-related pushes to `main` select variants from all changes in the push:

| Changed input | Variants selected |
| --- | --- |
| PHP minor manifest | That minor's PHP and Node variants |
| Runtime package lock | All variants using that Alpine release |
| Node-only Dockerfile configuration | Node variants |
| Shared build sources, scripts, tests, or CI workflows | All variants |
| Documentation, Renovate configuration, or unrelated files | None |

Each variant publishes its minor and patch tags only after its amd64 and arm64 builds and tests pass. CI publishes the tested images and verifies their source identity and architecture digests. Successful variants can publish independently of failures in other variants. A candidate overtaken by relevant changes on `main` is marked `Superseded` and leaves publication to the newer build.

### Rebuild or recover images

In GitHub Actions, run **Build & publish PHP images with extensions** from `main`. Choose `All` or a PHP minor; selecting a minor rebuilds its PHP and Node variants on both architectures using current `main`.

Use a fresh manual run to recover failed variants, restore missing tags, or recover from partially updated tags. Failed variants are not automatically retried by unrelated pushes, and there is no scheduled tag repair. Check the workflow summaries for `Published`, `Not selected`, `Superseded`, and `Failed`.

Rerunning an existing push workflow repeats its original selection.

### Set up a repository or fork

Install the [hosted Renovate app](https://github.com/apps/renovate). Protect `main` with the required **Validate build outcome** check, require branches to be up to date, and allow Renovate to merge passing updates under the repository's review rules. Renovate performs the merges itself.
