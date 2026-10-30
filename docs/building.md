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

The minor's Bake file owns its enabled extensions, PHP image, package pins, XHProf source, and optional configure arguments. PIE and Composer remain pinned in the selected Dockerfile. Node targets may override `NODE_BASE_IMAGE` with an exact version and digest; otherwise they use that Dockerfile’s pinned default. Custom local selections can override the Bake `EXTENSIONS` variable; all build switches are derived from that selection:

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

Each command replaces the output file. Selection needs Git history and the Docker CLI with Buildx, but does not require a running Docker daemon or network access. Fetch the comparison reference beforehand if it is unavailable locally. Without a selection option, all variants are included. The output retains `phpimages`, the per-variant selection report, and the existing flags, and adds `buildgroups`: one `{id, minor, distribution, distribution_release, variants}` object per selected base family. `id` is the canonical plain Bake target; variants are ordered plain PHP first, then Node majors numerically. Grouping never adds an unselected variant; a Node-only change produces Node-only groups.

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

To add a PHP minor, add its HCL file, a `ci-<minor>` group (for example `ci-8-4`), its plain/Node targets and inherited base identity, its Renovate compatibility rules, the manual workflow choice, and documentation. If an external extension does not support that minor, omit it from `EXTENSIONS` and omit its version pin. Bake derives a false compiler switch; installation is skipped and runtime/version tests do not expect the module. Other minors retain their own pins. Do not suppress a failed installation of an enabled extension. PHP 8.5 and later must not include built-in OPcache in the managed extension list.

### Add another Node major or distribution family

Each minor’s `ci-<minor>` Bake group explicitly lists every publishable target, including each canonical plain target. Targets inherit the `io.github.marlub.docker-php.base-target` label identifying that canonical target. CI discovers this inventory through `docker buildx bake --print`; target names are not reconstructed from Node 24 conventions.

To add another Node major, inherit the same PHP family, select the `php-node` stage, set a version-and-digest-pinned `NODE_BASE_IMAGE`, and add the target to the minor’s CI group. Its local tag must be the canonical plain tag plus `-node<major>`. Node must match the PHP distribution and release; Debian Node images may use the matching `-slim` tag. All PHP build arguments must remain identical; only `NODE_BASE_IMAGE` may differ. There can be one image per Node major in each family. Renovate detects target-specific Node pins and updates digests, patches, and minors; automated Node major upgrades are disabled, so new majors are an explicit configuration change.

To add a distribution or release, define a separate canonical plain target, its inherited base label, and its own package profile and Dockerfile with `php` and `php-node` stages. Plain image tags start with `<PHP minor>-fpm-`; select globally unique minor and patch tags, including release identity when multiple releases would otherwise collide. PHP and Node source pins determine distribution/release metadata. Register the family’s targets in the same minor’s CI group. Distribution families get separate groups/builders even when the PHP minor matches. Shared PHP inputs and Dockerfile must match within a family.

The controller dispatches package-inventory checks through `PACKAGE_CHECKS` in `scripts/build_group.py`. Alpine uses the existing APK check. Debian metadata is supported for planning and fixtures, but a real Debian family also needs its package recipe, lock/version policy, and package-check backend: an unregistered backend fails explicitly before building. The APK refresh command selects only canonical plain Alpine targets and remains usable without existing locks. Today’s production inventory still contains only the original Alpine/Node 24 variants; test-only Debian and extra-major pins are never published.

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

The PR workflow and automatic publication use explicit `paths-ignore` filters: any changed path that is not ignored triggers the corresponding workflow. Both ignore `docs/**`, the root `README.md` and `LICENSE`, and the registry-cache cleanup workflow and script (`.github/workflows/cache-cleanup.yml` and `scripts/cleanup_registry_cache.py`). Cleanup-only changes neither test nor publish images. Publication additionally ignores `renovate.json` and `.github/workflows/template-renovate.yml`; the PR workflow deliberately allows those paths so it can validate Renovate updates. A mixed change containing another path still triggers the workflow. Other new files, scripts, directories, and workflows run by default without needing an inclusion-list update.

When triggered, PRs run the image-related Python unit tests, then the existing selection job computes image selection and a separate `renovate_changes` flag from one Git diff against the target-base commit. Images are tested from the checked-out merge result without publishing. Changes to a PHP-minor Bake file select its plain and Node variants; runtime package locks select variants using that Alpine release. Node-only Dockerfile changes select Node variants. Renovate configuration and the Renovate template select no images; every other non-ignored path selects all variants, including new or unrecognized files. New commits cancel superseded runs for the same PR, including called workflows.

Automatic publication on `main` additionally ignores `tests/**`, `scripts/plan_ci.py`, `scripts/image_config.py`, `scripts/refresh_apk_locks.py`, and `.github/workflows/build-test.yml`. These explicit test/tooling exclusions do not trigger publication or broaden a mixed push's publication selection. For example, a PHP 8.3 Bake change combined with test changes tests all variants in the PR but publishes only the two PHP 8.3 variants. Publication selection covers the entire push, retains PHP-minor, Alpine-release, and Node-only targeting, and selects all variants for every other non-ignored path. Keep the planner's exclusion lists aligned with the workflow filters. Review exclusions if an ignored file becomes an image input.

PIE version/digest-only updates are tested in PRs for every variant using the changed recipe, but do not automatically publish images. The publication planner disregards only a recognized `PIE_IMAGE` version/digest change with the same image repository and tag suffix; changes to the PIE installer helper or other shared recipe instructions still publish normally. A PIE update combined with a Node-only recipe change publishes only that recipe's Node variants; combining it with a PHP-minor configuration change publishes only that minor's variants. A PIE-only push still runs publication planning, which emits an empty image selection and skips builds and publication. Missing, ambiguous, or unfamiliar recipe layouts do not qualify for this exception.

Use the existing manual **Build & publish PHP images with extensions** workflow on `main`, selecting a PHP minor or `All`, when a PIE fix warrants replacing published images. Normal caching remains the default: a changed PIE binary invalidates its dependent compiler stages, while `no-cache` requests a fresh rebuild. Otherwise, later image-relevant builds adopt the updated PIE pin. This policy changes publication selection only; OS build-dependency handling, cache references, and cache permissions are unchanged.

Renovate validation is a separate job within the same PR workflow, calling `.github/workflows/template-renovate.yml` only when `needs.read-matrix.outputs.renovate_changes == 'true'`. The reusable template keeps the version pin inline, installs Node, and runs `renovate-config-validator --strict --no-global renovate.json`. It has only a `workflow_call` trigger and no separate change-detection job or concurrency group. Changes to `renovate.json` or the template select Renovate validation; a validator-only update skips Docker image builds, while mixed image/Renovate changes run both jobs. Additions, deletions, and renames are detected by the existing selector, and Git errors fail selection. The final **Validate build outcome** job checks both jobs, requiring success when selected and an intentional skip otherwise.

Each selected base family calls the reusable workflow with its nonempty selected `variants` JSON array. Every entry must match the checked-out pinned configuration and share the same canonical base target, PHP minor, distribution, and release. Each architecture job creates one fresh Buildx builder with compilation parallelism limited to one. A group action invokes `scripts/build_group.py` to run plain PHP first when selected, then every selected Node major in numeric order. Node builds reuse shared PHP stages from that builder’s local cache, including on fork and Dependabot PRs without registry exports. Selection never adds a plain warm-up image. Families and architectures remain parallel; today’s full selection still uses eight native build runners.

Each variant still runs all image tests and version checks. A failed build, test, or transfer does not stop later selected variants, and the architecture job fails after all attempts if any selected variant failed. Publication runs independently per variant even when a grouped build job failed; only variants with successful tested candidates on both architectures can update tags. Cancellation stops further builds and publication. One `build-results-<base-target>-<arch>-attempt-<attempt>` artifact contains the group's `results.json`, even if individual variants failed. Its identity includes the canonical base target, source commit, architecture, and workflow attempt. Each selected variant has an outcome and last stage; only successfully built, tested, verified, and transferred candidates have a digest. Each publisher downloads both architecture bundles, validates their identities and its own successful results, then writes the digest files used by the existing candidate and tag checks. A failed sibling is ignored for successful publication; missing, malformed, or earlier-attempt results cannot satisfy a failed rebuild. Temporary OCI archives and extracted blobs are removed after each variant's transfer, while the builder cache remains available for Node. Selected variants build and test on both architectures before updating minor and patch tags. Each architecture image is compiled once, tested locally on its CI runner, and transferred to the registry by digest. Candidate source/variant labels and the final tags' architecture digests are verified. A variant changed by a newer publication-relevant commit on `main` is reported as `Superseded`; its older candidate does not update tags. Later PIE version/digest-only updates, test-only changes, or other excluded changes do not supersede publication candidates. Publication runs queue without replacing pending runs.

By default, image builds import [BuildKit's registry cache](https://docs.docker.com/build/cache/backends/registry/) from the dedicated public package `ghcr.io/marlub/php-buildcache`. The reusable workflow takes its full address through `cache-image`, independently of the runtime image's publication inputs. Main publication builds import their `main-<variant>-<arch>` cache plus eligible merged-PR caches for the same variant and architecture, and export only to their main tag. There are 16 main tags across the current variants and architectures. Same-repository PR builds, including Renovate PRs, import both main's cache and their own `pr-<number>-<variant>-<arch>` cache, and export only their PR cache. Fork PRs and Dependabot runs import main's public cache anonymously and skip registry login and export. Same-repository contributor branches are trusted to receive package write access; GitHub restricts fork and Dependabot tokens to read-only access.

Publication planning discovers merged PR cache sources once, only when images are selected. Push runs inspect the first-parent commits introduced between the event's `before` SHA and the pinned build source, supporting ordinary merge, squash, and rebase merges and multiple PRs in one push. Manual rebuilds, initial pushes, and non-ancestor history inspect only the pinned source commit, never a moving branch tip. The planner queries GitHub's paginated associated-PR API and checks each PR's details: it must be closed and merged into this repository's `main`, have a head in this same repository, have a merge SHA in the inspected commit set, and not be authored by Dependabot. The publication planning job has `pull-requests: read`; its `merged-prs` JSON output becomes the reusable workflow's optional `merged-prs` input (default `[]`). Only eligible main publication builds honor that input. API, history-discovery, or response-validation failures warn and fall back to main's cache alone; existing image-selection failures still fail the run. Workflow summaries report the chosen PR numbers and per-build cache references.

Only the image-build caller jobs request `packages: write`. Eligible groups log in before the build loop using `GITHUB_TOKEN`; no PAT is required. Authentication is performed once per registry for the group. Exports use `mode=max`, the default gzip compression, and `ignore-error=true`. Matching layers reuse compiled extensions; changed build inputs invalidate their dependent layers. All image tests and version checks still run on every selected image. A missing cache starts a cold build, and cache export failures do not fail otherwise successful builds. PR jobs load only the Docker image for testing; publication jobs also export the OCI archive used to transfer the tested image by digest.

Each PIE package and XHProf compiles in an independent stage; bundled extensions compile together in one stage. Each stage declares only its own build switch, version, and configure arguments, so a Redis version or configure-option change invalidates only the Redis compiler. XHProf source changes invalidate only XHProf. The final image installs the pinned runtime packages and copies the compiled modules, INI files, and XHProf support files; compiler packages and sources stay in intermediate stages. Composer is copied separately, so Composer updates do not recompile extensions. PIE binary updates invalidate the PIE compiler stages, while PHP base, runtime-lock, or shared package-profile changes can invalidate all compiler stages; PIE-helper changes affect only PIE stages. Each architecture runner executes one compiler step at a time to bound memory usage; `mode=max` preserves intermediate compiler caches across runs.

Compiled PIE and XHProf modules are stripped of unnecessary native symbols before artifact collection, in the same build step as compilation. Bundled extensions are stripped by `docker-php-ext-install`. This keeps debug information out of both the exported compiler cache and final images; stripping only in a later layer would retain the original large binaries in the cache.

To measure a complete compressed cache locally, add `--allow=fs.write=/tmp --set '<target>.cache-to=type=local,dest=/tmp/php-build-cache,mode=max'` to a Bake build and inspect `du -sh /tmp/php-build-cache`. Use a fresh destination for each measurement: reusing a local export directory can retain old blobs. Local BuildKit disk usage is uncompressed and includes temporary snapshots, so it is not the same as exported cache storage.

To verify family reuse and cache isolation with a real builder, run `python3 tests/check_extension_cache.py --builder <builder-name> --logs /tmp/php-extension-cache-checks`. It builds PHP 8.4 by default, then checks reuse across every configured Node major, warm-cache reuse, a forced plain rebuild on a separate disposable fresh builder followed by Node reuse with identical PHP module hashes, and isolated Redis, XHProf, and Composer changes in a disposable build context. This requires Docker and network access; allow time for an initial build plus a forced rebuild. Use an otherwise idle builder configured with `[worker.oci] max-parallelism = 1` to match CI's compilation concurrency. Concurrent builds can share progress records and make the check report shared compilation as a cache miss. The forced phase creates and removes its own builder to exclude older matching cache records, mirroring CI; its plain/Node module hashes are saved alongside the logs. Add `--paired-only` to stop after the normal/forced pair checks and run all image tests and version checks against every freshly built variant. If the minor has multiple families, also pass `--base-target <canonical-target>`. Build logs report compiler execution and elapsed times; hosted AMD64 and ARM64 runs must confirm reuse on their native runners. Compare a PHP-minor change, a Node-only change, and a forced rebuild with the previous workflow; avoid assuming that less compilation necessarily reduces total elapsed time.

To exercise the actual group controller against a configured family, run `python3 tests/check_build_group.py --builder <builder-name> --php 8.4 --logs /tmp/php-build-group-checks`. It calls the same preparation and execution CLI as CI and runs all image tests; cache writes and publication are disabled. Use `--base-target` to choose among multiple families. The local helper expects a native AMD64 engine; hosted CI must also confirm native ARM64 behavior.

For a small real BuildKit check with synthetic Node variants, run `python3 tests/check_build_group.py --fixture --logs /tmp/php-group-cache-checks`. It creates and removes separate fresh builders for a plain-plus-two-Node group and a Node-only group. Each must execute its shared compiler fixture once and retain matching artifact hashes. It uses the pinned PHP base and verifies matching Docker/OCI configurations with a stubbed registry upload. It performs no registry pushes or cache exports and removes its temporary images. Configuration tests separately resolve fake Debian and second-major pins without downloading them.

To check registry cache transfer independently of a full PHP build, run `python3 tests/check_registry_cache.py --logs /tmp/php-registry-cache-checks`. It creates a disposable local registry and fresh builders for PR, main, and main-only phases on `amd64` and `arm64`. A small native intermediate stage generates a target-specific artifact without emulation. Main must reuse the PR cache and export its own cache; after both PR manifests are deleted, a fresh main-only builder must still reuse the stage. Each build also verifies matching Docker and OCI configurations and the target architecture. Separate PR/main cache repositories let the check delete a PR manifest even if both exports have the same digest. The script removes its builders, registry, and network when finished; logs remain at the requested path. This checks cache transport and independence, while the full PHP build and hosted post-merge checks verify actual extension compilation.

The first build or a cache miss still requires compilation. A changed extension is cached for subsequent runs of the same writable PR. After merging, main can reuse that PR's compiler stages when their inputs match, while still building and testing the pinned main source. Main's successful `mode=max` export includes the reused layers in its own cache, so later main builds can use them after the PR cache is removed. This exports the current build's cache graph; it does not retag or copy the entire PR cache. Identical cache exports may share a registry version carrying both main and PR tags; cleanup preserves such versions. A missing or expired PR cache only causes normal compilation. Fork PRs and Dependabot runs do not save their changed stages. Cached layers also retain the unpinned temporary build tools used during their original compilation. Registry caches do not consume the Actions cache allowance. GHCR container storage and bandwidth are [currently free](https://docs.github.com/en/billing/concepts/product-billing/github-packages), with separate registry limits, including a [10 GB maximum per layer](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#troubleshooting). Transfer speed depends on the workload and should be measured rather than assumed.

The registry-cache cleanup workflow runs when a PR closes, daily at **03:17 UTC**, and on manual dispatch. It checks out only the trusted default branch, never PR content. Closed, unmerged PRs are eligible for immediate deletion. Merged PR caches remain protected for **48 hours after `merged_at`**, independently of the cache version's age; at that boundary they become eligible for the next daily or manual sweep. This gives queued main builds time to reuse them without tracking publication completion. Runs delayed beyond that window may compile normally. Closed-event runs consider only versions tagged exclusively for that PR. Daily and manual runs consider all eligible closed PRs and untagged versions whose `updated_at` is more than seven days old. Versions carrying main, open-PR, unknown, or multiple PR numbers' tags are preserved. It finishes pagination before deletion and rechecks each candidate's tags and PR state immediately before deleting it. The daily sweep also catches cache uploads that finish after a PR closes. Missing packages and already-deleted versions are harmless; other API errors fail cleanup. Deletions use only the dedicated `php-buildcache` package, leaving published PHP images separate.

Cleanup uses `GITHUB_TOKEN` with `packages: write` and package administration access. A repository that first publishes a package through its workflow receives package administration access automatically; for an existing package, grant the repository the **Admin** role under the package's Actions access settings. GitHub may refuse deletion of a public version with more than 5,000 downloads; such failures remain visible in the cleanup run. See [package deletion permissions](https://docs.github.com/en/packages/learn-github-packages/deleting-and-restoring-a-package).

For migration, merge the workflow changes into `main`, then run **Build & publish PHP images with extensions** with **All** to warm main's 16 registry cache tags. The new package starts private: in the `php-buildcache` package settings, change its visibility to **Public** and confirm that the repository has administration access. Verify an anonymous read using a temporary empty Docker configuration, for example `DOCKER_CONFIG=$(mktemp -d) docker buildx imagetools inspect ghcr.io/marlub/php-buildcache:main-8.4-fpm-alpine-amd64`. Then run and rerun a representative same-repository PR on both architectures and confirm that its changed compiler stages are cached on the second run; all image tests must still execute. Rollout is complete only after public reads and PR reuse are verified. Existing Actions caches are no longer imported or exported and can expire normally; no deletion is required. To inspect cleanup without deleting anything, run `GH_TOKEN=<token> python3 scripts/cleanup_registry_cache.py --repository marlub/docker-php --dry-run` with an appropriate token.

For merged-PR reuse, merge a representative passing same-repository PR and inspect main's cache-source summary on both architectures. Matching compiler stages should be cached, every image test must still execute, and main's registry cache export must succeed. A later main build must be able to use those layers through main's cache alone. The 48-hour retention change applies to existing PR cache tags too; there is no tag migration or publication-completion state to initialize.

### Rebuild or recover images

In GitHub Actions, run **Build & publish PHP images with extensions** from `main`. Choose `All` or a PHP minor; selecting a minor rebuilds its PHP and Node variants on both architectures using current `main`.

Enable **no-cache** to force fresh compilation, for example to pick up newer temporary build tools or diagnose a cached build. Each architecture job starts with a fresh builder and disables all registry cache imports for every selected variant. Builds run without cache until one image successfully builds and loads. Every later variant reuses only the PHP layers freshly produced in that job, including when only Node majors were selected. Build completion is recorded before tests and transfer, so later failures still leave its fresh PHP layers reusable. A failed build leaves the next attempt uncached until a build succeeds. Fresh results are exported under the existing cache policy. The default is to reuse matching local and registry layers.

Use a fresh manual run to recover failed variants, restore missing tags, or recover from partially updated tags. Failed variants are not automatically retried by unrelated pushes, and there is no scheduled tag repair. Check the workflow summaries for `Published`, `Not selected`, `Superseded`, and `Failed`.

Use **Re-run all jobs** when rerunning a publication workflow; it repeats the original selection and produces both architectures’ result bundles for the new attempt. Rerunning only failed or publication jobs cannot reuse artifacts from an earlier attempt and fails closed when current-attempt artifacts are missing. A fresh manual run remains the supported way to recover selected variants against current `main`.

During publication, `plan_ci.py --check-current <variant-json> --github-output <file>` fetches current `main` and emits `superseded=true|false` using publication selection rules. Fetch errors fail the check. `verify_image.py --metadata --entry <variant-json> --platform linux/amd64` verifies registry architecture, revision, and variant identity; normal `--image` mode verifies the locally built image and installed versions.

### Set up a repository or fork

Install the [hosted Renovate app](https://github.com/apps/renovate) for this repository and allow the bot to merge passing PRs under the repository's review rules. When adopting the workflows in another repository, update both callers' `cache-image` address and the publication image namespace; the dedicated cache package remains named `php-buildcache` and cleanup uses the repository owner as its package owner. Make that package public and grant the repository package administration access as described above. PR image tests and Renovate validation now share the **Validate build outcome** aggregate check. The former standalone **Validate Renovate configuration** workflow is replaced by the called job, so any branch-protection reference to that former check must be updated during rollout. The parent PR workflow still uses `paths-ignore` for documentation and cleanup and can be absent on such PRs; globally requiring its checks would leave those PRs pending. Keep the parent workflow unfiltered or provide separate conditional enforcement if globally required checks are needed. Branch protection is configured separately from these repository files and is not changed by this update. Renovate merges PRs itself (`platformAutomerge: false`); no publication-workflow GitHub App secret is needed.
