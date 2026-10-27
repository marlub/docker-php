# Building and maintaining the images

## Updates and publication

The hosted Renovate app proposes updates to committed image digests, extension versions, XHProf source commits, GitHub Actions, and Alpine packages. Stable patch, minor, package revision, and digest updates can merge after the required tests pass. Major updates require review. PHP minors and Node major 24 stay fixed until explicitly changed.

The PR workflow and automatic publication use explicit `paths-ignore` filters: any changed path that is not ignored triggers the corresponding workflow. Both ignore exactly `docs/**` and the root `README.md` and `LICENSE`. Publication additionally ignores `renovate.json` and `.github/workflows/template-renovate.yml`; the PR workflow deliberately allows those paths so it can validate Renovate updates. A mixed change containing another path still triggers the workflow. New files, scripts, directories, and workflows run by default without needing an inclusion-list update.

When triggered, PRs run the image-related Python unit tests, then the existing selection job computes image selection and a separate `renovate_changes` flag from one Git diff against the target-base commit. Images are tested from the checked-out merge result without publishing. Changes to a PHP-minor manifest select its plain and Node variants; runtime package locks select variants using that Alpine release. Node-only Dockerfile changes select Node variants. Renovate configuration and the Renovate template select no images; every other non-ignored path selects all variants, including new or unrecognized files. New commits cancel superseded runs for the same PR, including called workflows.

Automatic publication on `main` additionally ignores `tests/**`, `scripts/test_image.sh`, `scripts/verify_image.py`, `scripts/plan_ci.py`, `scripts/image_config.py`, `scripts/refresh_apk_locks.py`, and `.github/workflows/build-test.yml`. These explicit test/tooling exclusions do not trigger publication or broaden a mixed push's publication selection. For example, a PHP 8.3 manifest change combined with test changes tests all variants in the PR but publishes only the two PHP 8.3 variants. Publication selection covers the entire push, retains PHP-minor, Alpine-release, and Node-only targeting, and selects all variants for every other non-ignored path. Keep the planner's exclusion lists aligned with the workflow filters. Review exclusions if an ignored file becomes an image input.

Renovate validation is a separate job within the same PR workflow, calling `.github/workflows/template-renovate.yml` only when `needs.read-matrix.outputs.renovate_changes == 'true'`. The reusable template keeps the version pin inline, installs Node, and runs `renovate-config-validator --strict --no-global renovate.json`. It has only a `workflow_call` trigger and no separate change-detection job or concurrency group. Changes to `renovate.json` or the template select Renovate validation; a validator-only update skips Docker image builds, while mixed image/Renovate changes run both jobs. Additions, deletions, and renames are detected by the existing selector, and Git errors fail selection. The final **Validate build outcome** job checks both jobs, requiring success when selected and an intentional skip otherwise.

Each selected variant calls the reusable workflow independently. Its publication job depends on both architecture builds succeeding; a failed variant does not block other complete variants. Selected variants build and test on both architectures before updating minor and patch tags. Each architecture image is compiled once, tested locally on its CI runner, and transferred to the registry by digest. Candidate source/variant labels and the final tags' architecture digests are verified. A variant changed by a newer publication-relevant commit on `main` is reported as `Superseded`; its older candidate does not update tags. Later test-only or other excluded changes do not supersede publication candidates. Publication runs queue without replacing pending runs.

Renovate discovers upstream updates on its hosted schedule; there is no scheduled publication check or automatic tag repair. Formatting-only changes to selected build files can rebuild images. Publication does not compare inputs with existing images.

## Configuration

`build/php/<minor>.json` holds each PHP base reference, extension list, PIE package versions and configure options, and the XHProf tag/commit pair. PHP bases include an explicit Alpine release and an immutable multi-platform digest. PIE, Composer, and Node image pins live in the Dockerfile.

`build/apk/<release>/<architecture>.json` holds the exact additional runtime package versions, including their transitive dependencies and `-rN` revisions. Inherited, unchanged packages are covered by the PHP base digest. Renovate looks up amd64 packages in `x86_64` repositories and arm64 packages in `aarch64` repositories. Related runtime package updates are grouped across architectures. Temporary build tools and development packages are resolved from the configured Alpine branch and removed after compilation; their versions are not tracked.

The installer enforces runtime pins during package installation and checks the final package inventory after build-dependency cleanup. It rejects missing pins, unrecorded runtime dependencies, and version drift. Incompatible or unavailable runtime pins fail the build. Build-tool updates alone do not trigger rebuilds, so rebuilding unchanged committed inputs can use newer temporary build tools. Normal builds do not query upstream extension releases or regenerate package locks.

External modules use PIE: Redis (`phpredis/phpredis`), AMQP (`php-amqp/php-amqp`), Xdebug (`xdebug/xdebug`), and PCOV (`pecl/pcov`). The PCOV package identifier does not require the PECL service. Bundled modules use Docker's PHP helpers. XHProf is compiled from the pinned Git checkout; its source tag and actual module version are recorded separately in `/usr/local/lib/php/xhprof-build.json`. Tag `v2.3.10` reports module version `2.3.9`.

The Dockerfile invokes `scripts/install_extensions.sh` with POSIX shell. It reads JSON using temporary `jq` tooling and executes commands directly, without generated shell code or `eval`. The base inventory is captured before installing `jq`; both JSON tooling and extension build dependencies are removed before checking the final runtime inventory. APK inventory validation remains in PHP, and package profiles remain shared with the lock-refresh command.

PIE is mounted only during the build. Composer is copied from its upstream binary image. Temporary sources, build dependencies, and legacy PECL clients are removed.

## Local checks and builds

`scripts/plan_ci.py` plans image builds and Renovate validation, checks publication supersession, and writes the CI outputs. `scripts/image_config.py` loads and validates image configuration independently of Git and shares image metadata definitions with verification. The planner retains the existing CLI options and matrix fields.

Run unit tests and prepare the complete matrix without building or contacting registries:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/plan_ci.py --out /tmp/php-build-matrix.json
```

Select changes against a Git reference, or select one PHP minor explicitly:

```sh
python3 scripts/plan_ci.py --base-ref origin/main --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --purpose publish --base-ref origin/main --out /tmp/php-build-matrix.json
python3 scripts/plan_ci.py --php 8.4 --out /tmp/php-build-matrix.json
```

Selection requires Git history but does not need Docker or registry access. `--purpose test` is the default; `--purpose publish` applies the narrower publication policy to Git changes. Selection JSON and GitHub outputs include `renovate_changes`; it is true when the diff touches `renovate.json` or the Renovate template. An unavailable Git reference fails selection; a zero `before` SHA on an initial push selects all variants and Renovate validation. Omitting selection options or passing `--php All` selects all variants. Manual PHP selection behaves the same for both purposes and sets `renovate_changes=false` because no PR diff is evaluated.

During publication, `plan_ci.py --check-current <variant-json> --github-output <file>` fetches current `main` and emits `superseded=true|false`, always using publication selection rules. This mode needs Git/network access and fails on fetch errors. `verify_image.py --metadata --entry <variant-json> --platform linux/amd64` reads registry image metadata from stdin and verifies architecture, revision, and variant identity; normal `--image` mode still checks the locally built image and installed versions.

Only Alpine PHP base images are supported. Standalone Docker/Compose builds use the extension and package pins matching the actual PHP minor and Alpine release. `EXTENSIONS` remains the module list; `PACKAGES` on Alpine must be covered by the package lock. The `DEPENDENCIES` build argument and separate defaults file have been removed. Override `PHP_BASE_IMAGE` or `NODE_BASE_IMAGE` for custom bases. The default Node image is pinned; changing `NODE_VERSION` alone does not replace that image.

## Refreshing package locks

Refreshing locks requires Docker, network access, and execution of disposable PHP base containers. It installs only the runtime package profile and records its additional dependency closure; it does not install compilers, compile extensions, or build the image variants. Cross-architecture resolution requires a matching native runner or emulation.

```sh
python3 scripts/refresh_apk_locks.py --platform linux/amd64
python3 scripts/refresh_apk_locks.py --platform linux/arm64
```

Review and commit the resulting lock changes. New runtime dependencies and Alpine release migrations require this explicit refresh. When using an already cached platform-specific base, select its PHP minor with `--php` and supply its reference with `--base-image`.

Alpine repositories may remove historical package versions. Unavailable pins fail explicitly; exact pins do not provide an archival package mirror.

## Repository setup

Install the [hosted Renovate app](https://github.com/apps/renovate) for this repository and allow the bot to merge passing PRs under the repository's review rules. PR image tests and Renovate validation now share the **Validate build outcome** aggregate check. The former standalone **Validate Renovate configuration** workflow is replaced by the called job, so any branch-protection reference to that former check must be updated during rollout. The parent PR workflow still uses `paths-ignore` for documentation and can be absent on documentation-only PRs; globally requiring its checks would leave those PRs pending. Keep the parent workflow unfiltered or provide separate conditional enforcement if globally required checks are needed. Branch protection is configured separately from these repository files and is not changed by this update. Renovate merges PRs itself (`platformAutomerge: false`); no publication-workflow GitHub App secret is needed.

## Manual recovery

Run **Build & publish PHP images with extensions** from `main` and choose `All` or a PHP minor. A chosen minor rebuilds both its plain and Node variants on both architectures. Dispatches from other branches are rejected. Manual dispatch reads current `main` when selection starts.

Rerunning an existing push run repeats its original Git selection and may rebuild or republish variants that previously succeeded. Architecture digests are stored in separate, exactly named artifacts per variant. Reruns overwrite the matching artifact; failed-job retries can reuse the successful architecture’s artifact from the same run. If artifacts have expired, rerun all jobs or dispatch a fresh build. Failed variants are not automatically added to later unrelated runs. Use a fresh manual dispatch to recover failed variants or recreate missing tags. Complete variants can publish even if another variant fails; variants with failed or incomplete architecture tests retain their existing tags. A registry error during tag updates can leave a partial publication; recover it with a fresh manual dispatch. Candidates superseded by relevant newer changes leave publication to the newer run. Inspect publication summaries for `Published`, `Not selected`, `Superseded`, and `Failed`.

No image migration or new repository secrets are required. The first push containing this workflow change selects all variants. Existing image tags remain compatible; image identity now uses the source revision and variant labels.

Tests run on native amd64 and arm64 GitHub runners. When the PR image workflow runs, its aggregate check must pass even when selection is intentionally empty; failed selection or unexpectedly skipped builds cannot pass it.
