# Building and maintaining the images

This guide covers local builds, dependency updates, and publication. For available images and runtime configuration, see the [README](../README.md).

## Build and test locally

Run commands from the repository root. Builds require Docker, Buildx, and network access. Bake is the supported build entry point. Python checks, selection, and lock refreshes also require the Docker CLI with Buildx: configuration is resolved using `docker buildx bake --print`, which needs neither a running Docker daemon nor registry access.

Run the unit tests:

```sh
python3 -m unittest discover -s tests -v
```

Build the pinned PHP 8.4 image:

```sh
docker buildx bake -f docker-bake.hcl -f build/php/8.4.hcl --load php-8-4
```

Use `php-8-4-node24` for its Node variant. Other minor files provide corresponding targets, such as `php-8-5` and `php-8-5-node24`. Always specify both Bake files explicitly so local Compose overrides are not loaded implicitly. Local tags are `docker-php:<variant>`, for example `docker-php:8.4-fpm-alpine`.

The minor's Bake file owns its enabled extensions, PHP image, package pins, XHProf source, and optional configure arguments. Shared tool images remain pinned in the Dockerfile. Custom local selections can override the Bake `EXTENSIONS` variable; all build switches are derived from that selection:

```sh
docker buildx bake -f docker-bake.hcl -f build/php/8.4.hcl \
  --var 'EXTENSIONS=bcmath redis' --load php-8-4
```

An empty `--var EXTENSIONS=` builds without additional extensions or Composer. Direct Docker builds require the explicit image, selection switches, and version arguments; the former four-argument Docker build interface is not maintained. The ignored local `compose.override.yaml`, if present, must be updated separately when adopting Bake.

Test a default build:

```sh
image=docker-php:8.4-fpm-alpine
extensions=$(docker image inspect "$image" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^EXTENSIONS=//p')
docker run --rm --entrypoint php -v "$PWD:/work:ro" "$image" /work/tests/test_apk_lock.php
docker run --rm --entrypoint sh -v "$PWD:/work:ro" "$image" /work/tests/test_installer.sh
tests/test_image.sh --image="$image" --extensions="$extensions"
```

For a Node variant, also pass `--node=24` to the image test. Checks cover disabled defaults, independent activation, combined loading, extension functionality, and container users. CI also verifies installed versions against the resolved pins.

Local builds use the host architecture. Override a target with `--set php-8-4.platform=linux/arm64` when needed; building or running another architecture requires emulation. CI builds and tests `linux/amd64` and `linux/arm64` on native runners.

### Preview the build matrix

Generate the complete matrix, select a PHP minor, or select committed changes against a Git reference:

```sh
python3 scripts/plan_ci.py --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --php 8.4 --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --base-ref origin/main --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --purpose publish --base-ref origin/main --out /tmp/php-build-matrix.json
```

Each command replaces the output file. Selection needs Git history and the Docker CLI with Buildx, but does not require a running Docker daemon or network access. Fetch the comparison reference beforehand if it is unavailable locally. Without a selection option, all variants are included.

`scripts/plan_ci.py` plans image builds and Renovate validation, checks publication supersession, and writes CI outputs. `scripts/image_config.py` extracts CI metadata and expected versions from resolved Bake targets. Compiler selection belongs to Bake; package inventory validation belongs to `scripts/apk_lock.php` in the build. Image functionality and version checks live under `tests/`. `--purpose test` is the default; `--purpose publish` applies publication exclusions to Git changes. Selection JSON and GitHub outputs include `renovate_changes`, which is true when the diff touches `renovate.json` or the Renovate template. An unavailable reference fails planning; a zero `before` SHA on an initial push selects all variants and Renovate validation. Manual PHP selection behaves the same for both purposes and sets `renovate_changes=false` because no PR diff is evaluated.

## Update dependencies and extensions

The build inputs live in these files:

| File | Contents |
| --- | --- |
| `build/php/<minor>.hcl` | PHP base image, extension list, PIE version arguments, reported module versions, XHProf tag/commit, and optional configure-option overrides |
| `build/apk/<release>/<architecture>.json` | Additional runtime packages and their exact versions, including transitive dependencies |
| `docker-bake.hcl` | Shared build settings, build/runtime package profiles, and derivation of compiler switches |
| `Dockerfile` | Compiler/runtime recipes and PIE, Composer, and Node image pins |

PHP bases specify an Alpine release and an immutable image digest. Unchanged packages inherited from the base are covered by that digest. The runtime stage checks its package inventory against the architecture's lockfile before compiler stages are derived from it. Missing pins, unavailable versions, and unexpected runtime dependencies fail the build.

Build tools and development packages use the configured Alpine repositories and stay in intermediate stages, outside the final image. Their versions are not pinned, so rebuilding the same committed inputs can use newer build tools. Normal builds use the committed extension and runtime package pins without refreshing them.

### Add or update an extension

1. Update `EXTENSIONS` and the relevant `<EXTENSION>_VERSION` argument in each affected minor's Bake file. Bundled extensions use Docker's PHP helpers; PIE packages have independent compiler stages. XHProf uses `XHPROF_TAG` and `XHPROF_COMMIT`.
2. Package pins are exact releases. When the reported module version differs, set `<EXTENSION>_MODULE_VERSION`; otherwise verification expects the package version. Protobuf currently needs this override for the `extport/protobuf` mirror.
3. A new external extension needs a Dockerfile compiler stage and artifact copy, plus an entry in Bake's `EXTERNAL_EXTENSIONS`. Keep package names and default configure options in the corresponding recipe. A minor can override `<EXTENSION>_CONFIGURE_OPTIONS` with validated whitespace-separated flags.
4. Update system package profiles and refresh both architecture locks when runtime dependencies change. Add a functional image test and update the README and Compose examples as needed.
5. Run the checks and require successful native CI builds on both architectures before publication.

To add a PHP minor, add its HCL file and matching plain/Node targets, its Renovate compatibility rules, the manual workflow choice, and documentation. If an external extension does not support that minor, omit it from `EXTENSIONS` and omit its version pin. Bake derives a false compiler switch; installation is skipped and runtime/version tests do not expect the module. Other minors retain their own pins. Do not suppress a failed installation of an enabled extension. PHP 8.5 and later must not include built-in OPcache in the managed extension list.

All extensions added by this project remain disabled by default. The entrypoint enables them with `PHP_EXTENSION_<NAME>=true` or `1`. Keep the PHP 8.5 built-in OPcache exception when updating extension lists.

### Refresh runtime package locks

Refresh the locks after adding runtime dependencies or changing the Alpine release:

```sh
python3 scripts/refresh_apk_locks.py --platform linux/amd64
python3 scripts/refresh_apk_locks.py --platform linux/arm64
```

The refresh reads the same `RUNTIME_PACKAGES` profile from resolved Bake arguments that the Dockerfile uses, including any additional `PACKAGES`. It runs disposable PHP base containers to resolve these packages and their dependencies. It requires Docker, network access, and a native runner or emulation for the selected architecture. It does not compile extensions or build the final images.

Review and commit both lockfiles, then rebuild and test the affected images. Extra packages requested through the `PACKAGES` build argument must also be covered by the runtime lock.

Alpine repositories can remove historical package versions. If a pin becomes unavailable, refresh and review the locks before rebuilding.

## Automated updates and publication

The hosted Renovate app proposes updates to base images, extension versions, XHProf source pins, PIE and Composer images, GitHub Actions, and Alpine runtime packages. Stable patch, minor, package revision, and digest updates can merge after required checks pass; major updates require review. PHP minor versions and Node major 24 stay fixed until explicitly changed.

The PR workflow and automatic publication use explicit `paths-ignore` filters: any changed path that is not ignored triggers the corresponding workflow. Both ignore exactly `docs/**` and the root `README.md` and `LICENSE`. Publication additionally ignores `renovate.json` and `.github/workflows/template-renovate.yml`; the PR workflow deliberately allows those paths so it can validate Renovate updates. A mixed change containing another path still triggers the workflow. New files, scripts, directories, and workflows run by default without needing an inclusion-list update.

When triggered, PRs run the image-related Python unit tests, then the existing selection job computes image selection and a separate `renovate_changes` flag from one Git diff against the target-base commit. Images are tested from the checked-out merge result without publishing. Changes to a PHP-minor Bake file select its plain and Node variants; runtime package locks select variants using that Alpine release. Node-only Dockerfile changes select Node variants. Renovate configuration and the Renovate template select no images; every other non-ignored path selects all variants, including new or unrecognized files. New commits cancel superseded runs for the same PR, including called workflows.

Automatic publication on `main` additionally ignores `tests/**`, `scripts/plan_ci.py`, `scripts/image_config.py`, `scripts/refresh_apk_locks.py`, and `.github/workflows/build-test.yml`. These explicit test/tooling exclusions do not trigger publication or broaden a mixed push's publication selection. For example, a PHP 8.3 Bake change combined with test changes tests all variants in the PR but publishes only the two PHP 8.3 variants. Publication selection covers the entire push, retains PHP-minor, Alpine-release, and Node-only targeting, and selects all variants for every other non-ignored path. Keep the planner's exclusion lists aligned with the workflow filters. Review exclusions if an ignored file becomes an image input.

Renovate validation is a separate job within the same PR workflow, calling `.github/workflows/template-renovate.yml` only when `needs.read-matrix.outputs.renovate_changes == 'true'`. The reusable template keeps the version pin inline, installs Node, and runs `renovate-config-validator --strict --no-global renovate.json`. It has only a `workflow_call` trigger and no separate change-detection job or concurrency group. Changes to `renovate.json` or the template select Renovate validation; a validator-only update skips Docker image builds, while mixed image/Renovate changes run both jobs. Additions, deletions, and renames are detected by the existing selector, and Git errors fail selection. The final **Validate build outcome** job checks both jobs, requiring success when selected and an intentional skip otherwise.

Each selected variant calls the reusable workflow independently. Its publication job depends on both architecture builds succeeding; a failed variant does not block other complete variants. Selected variants build and test on both architectures before updating minor and patch tags. Each architecture image is compiled once, tested locally on its CI runner, and transferred to the registry by digest. Candidate source/variant labels and the final tags' architecture digests are verified. A variant changed by a newer publication-relevant commit on `main` is reported as `Superseded`; its older candidate does not update tags. Later test-only or other excluded changes do not supersede publication candidates. Publication runs queue without replacing pending runs.

All image builds import [BuildKit's GitHub Actions cache](https://docs.docker.com/build/cache/backends/gha/), with a separate scope for every variant and architecture. Only publication builds running on `refs/heads/main` export caches; PR builds only read them. This keeps cache writes within main's 16 scopes regardless of the number of PRs. Matching layers reuse compiled extensions; changed build inputs invalidate their dependent layers. All image tests and version checks still run on every selected image. Cache export failures do not fail otherwise successful builds. PR jobs load only the Docker image for testing; publication jobs also export the OCI archive used to transfer the tested image by digest.

Each PIE package and XHProf compiles in an independent stage; bundled extensions compile together in one stage. Each stage declares only its own build switch, version, and configure arguments, so a Redis version or configure-option change invalidates only the Redis compiler. XHProf source changes invalidate only XHProf. The final image installs the pinned runtime packages and copies the compiled modules, INI files, and XHProf support files; compiler packages and sources stay in intermediate stages. Composer is copied separately, so Composer updates do not recompile extensions. PIE binary updates invalidate the PIE compiler stages, while PHP base, runtime-lock, or shared package-profile changes can invalidate all compiler stages; PIE-helper changes affect only PIE stages. CI runs one build step at a time to bound compiler memory usage; `mode=max` preserves intermediate compiler caches across runs.

Compiled PIE and XHProf modules are stripped of unnecessary native symbols before artifact collection, in the same build step as compilation. Bundled extensions are stripped by `docker-php-ext-install`. This keeps debug information out of both the exported compiler cache and final images; stripping only in a later layer would retain the original large binaries in the cache.

To measure a complete compressed cache locally, add `--allow=fs.write=/tmp --set '<target>.cache-to=type=local,dest=/tmp/php-build-cache,mode=max'` to a Bake build and inspect `du -sh /tmp/php-build-cache`. Use a fresh destination for each measurement: reusing a local export directory can retain old blobs. Local BuildKit disk usage is uncompressed and includes temporary snapshots, so it is not the same as exported cache storage.

To verify cache isolation with a real builder, run `python3 tests/check_extension_cache.py --builder <builder-name> --logs /tmp/php-extension-cache-checks`. It builds PHP 8.4 by default, then checks warm-cache reuse and isolated Redis, XHProf, and Composer changes in a disposable build context. This requires Docker and network access and can take as long as a full initial build. Use an otherwise idle builder configured with `[worker.oci] max-parallelism = 1` to match CI's compilation concurrency. Concurrent builds can share progress records and make the check report shared compilation as a cache miss.

The first build or a cache miss still requires compilation. PRs can read main's caches under GitHub's branch visibility rules. A changed extension recompiles on each PR run because PR builds do not export the result. After merging, main also compiles that change and updates the cache for subsequent builds. Unchanged extension stages remain reusable. Cached layers also retain the unpinned temporary build tools used during their original compilation. Old layers can accumulate as main changes and remain subject to GitHub's storage limits and eviction policy.

During migration, remove existing PR caches in the repository's **Actions > Caches** panel, or use `gh cache delete --all --ref refs/pull/<number>/merge --repo marlub/docker-php` for each old PR. New PR builds do not create caches, so a closed-PR cleanup workflow is no longer needed. Once this policy is on main, a full publication run warms all scopes; use the manual workflow with **All** if needed. Deleting all repository caches also removes main's caches, so the following builds start cold. Changing the cache policy does not immediately remove existing entries.

### Rebuild or recover images

In GitHub Actions, run **Build & publish PHP images with extensions** from `main`. Choose `All` or a PHP minor; selecting a minor rebuilds its PHP and Node variants on both architectures using current `main`.

Enable **no-cache** to force fresh compilation, for example to pick up newer temporary build tools or diagnose a cached build. This bypasses cached layers and exports the fresh results back to the cache. The default is to reuse matching layers.

Use a fresh manual run to recover failed variants, restore missing tags, or recover from partially updated tags. Failed variants are not automatically retried by unrelated pushes, and there is no scheduled tag repair. Check the workflow summaries for `Published`, `Not selected`, `Superseded`, and `Failed`.

Rerunning an existing push workflow repeats its original selection.

During publication, `plan_ci.py --check-current <variant-json> --github-output <file>` fetches current `main` and emits `superseded=true|false` using publication selection rules. Fetch errors fail the check. `verify_image.py --metadata --entry <variant-json> --platform linux/amd64` verifies registry architecture, revision, and variant identity; normal `--image` mode verifies the locally built image and installed versions.

### Set up a repository or fork

Install the [hosted Renovate app](https://github.com/apps/renovate) for this repository and allow the bot to merge passing PRs under the repository's review rules. PR image tests and Renovate validation now share the **Validate build outcome** aggregate check. The former standalone **Validate Renovate configuration** workflow is replaced by the called job, so any branch-protection reference to that former check must be updated during rollout. The parent PR workflow still uses `paths-ignore` for documentation and can be absent on documentation-only PRs; globally requiring its checks would leave those PRs pending. Keep the parent workflow unfiltered or provide separate conditional enforcement if globally required checks are needed. Branch protection is configured separately from these repository files and is not changed by this update. Renovate merges PRs itself (`platformAutomerge: false`); no publication-workflow GitHub App secret is needed.
