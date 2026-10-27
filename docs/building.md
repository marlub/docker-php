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
python3 scripts/plan_ci.py --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --php 8.4 --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --base-ref origin/main --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --purpose publish --base-ref origin/main --out /tmp/php-build-matrix.json
```

Each command replaces the output file. Selection needs Git history but does not require Docker or network access. Fetch the comparison reference beforehand if it is unavailable locally. Without a selection option, all variants are included.

`scripts/plan_ci.py` plans image builds and Renovate validation, checks publication supersession, and writes CI outputs. `scripts/image_config.py` loads and validates image configuration independently of Git and shares image metadata definitions with verification. `--purpose test` is the default; `--purpose publish` applies publication exclusions to Git changes. Selection JSON and GitHub outputs include `renovate_changes`, which is true when the diff touches `renovate.json` or the Renovate template. An unavailable reference fails planning; a zero `before` SHA on an initial push selects all variants and Renovate validation. Manual PHP selection behaves the same for both purposes and sets `renovate_changes=false` because no PR diff is evaluated.

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

The PR workflow and automatic publication use explicit `paths-ignore` filters: any changed path that is not ignored triggers the corresponding workflow. Both ignore exactly `docs/**` and the root `README.md` and `LICENSE`. Publication additionally ignores `renovate.json` and `.github/workflows/template-renovate.yml`; the PR workflow deliberately allows those paths so it can validate Renovate updates. A mixed change containing another path still triggers the workflow. New files, scripts, directories, and workflows run by default without needing an inclusion-list update.

When triggered, PRs run the image-related Python unit tests, then the existing selection job computes image selection and a separate `renovate_changes` flag from one Git diff against the target-base commit. Images are tested from the checked-out merge result without publishing. Changes to a PHP-minor manifest select its plain and Node variants; runtime package locks select variants using that Alpine release. Node-only Dockerfile changes select Node variants. Renovate configuration and the Renovate template select no images; every other non-ignored path selects all variants, including new or unrecognized files. New commits cancel superseded runs for the same PR, including called workflows.

Automatic publication on `main` additionally ignores `tests/**`, `scripts/test_image.sh`, `scripts/verify_image.py`, `scripts/plan_ci.py`, `scripts/image_config.py`, `scripts/refresh_apk_locks.py`, and `.github/workflows/build-test.yml`. These explicit test/tooling exclusions do not trigger publication or broaden a mixed push's publication selection. For example, a PHP 8.3 manifest change combined with test changes tests all variants in the PR but publishes only the two PHP 8.3 variants. Publication selection covers the entire push, retains PHP-minor, Alpine-release, and Node-only targeting, and selects all variants for every other non-ignored path. Keep the planner's exclusion lists aligned with the workflow filters. Review exclusions if an ignored file becomes an image input.

Renovate validation is a separate job within the same PR workflow, calling `.github/workflows/template-renovate.yml` only when `needs.read-matrix.outputs.renovate_changes == 'true'`. The reusable template keeps the version pin inline, installs Node, and runs `renovate-config-validator --strict --no-global renovate.json`. It has only a `workflow_call` trigger and no separate change-detection job or concurrency group. Changes to `renovate.json` or the template select Renovate validation; a validator-only update skips Docker image builds, while mixed image/Renovate changes run both jobs. Additions, deletions, and renames are detected by the existing selector, and Git errors fail selection. The final **Validate build outcome** job checks both jobs, requiring success when selected and an intentional skip otherwise.

Each selected variant calls the reusable workflow independently. Its publication job depends on both architecture builds succeeding; a failed variant does not block other complete variants. Selected variants build and test on both architectures before updating minor and patch tags. Each architecture image is compiled once, tested locally on its CI runner, and transferred to the registry by digest. Candidate source/variant labels and the final tags' architecture digests are verified. A variant changed by a newer publication-relevant commit on `main` is reported as `Superseded`; its older candidate does not update tags. Later test-only or other excluded changes do not supersede publication candidates. Publication runs queue without replacing pending runs.

### Rebuild or recover images

In GitHub Actions, run **Build & publish PHP images with extensions** from `main`. Choose `All` or a PHP minor; selecting a minor rebuilds its PHP and Node variants on both architectures using current `main`.

Use a fresh manual run to recover failed variants, restore missing tags, or recover from partially updated tags. Failed variants are not automatically retried by unrelated pushes, and there is no scheduled tag repair. Check the workflow summaries for `Published`, `Not selected`, `Superseded`, and `Failed`.

Rerunning an existing push workflow repeats its original selection.

During publication, `plan_ci.py --check-current <variant-json> --github-output <file>` fetches current `main` and emits `superseded=true|false` using publication selection rules. Fetch errors fail the check. `verify_image.py --metadata --entry <variant-json> --platform linux/amd64` verifies registry architecture, revision, and variant identity; normal `--image` mode verifies the locally built image and installed versions.

### Set up a repository or fork

Install the [hosted Renovate app](https://github.com/apps/renovate) for this repository and allow the bot to merge passing PRs under the repository's review rules. PR image tests and Renovate validation now share the **Validate build outcome** aggregate check. The former standalone **Validate Renovate configuration** workflow is replaced by the called job, so any branch-protection reference to that former check must be updated during rollout. The parent PR workflow still uses `paths-ignore` for documentation and can be absent on documentation-only PRs; globally requiring its checks would leave those PRs pending. Keep the parent workflow unfiltered or provide separate conditional enforcement if globally required checks are needed. Branch protection is configured separately from these repository files and is not changed by this update. Renovate merges PRs itself (`platformAutomerge: false`); no publication-workflow GitHub App secret is needed.
