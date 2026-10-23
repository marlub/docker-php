# Building and maintaining the images

## Dependency selection

Dependency selection requires Python 3, a running Docker daemon, and network access. It pulls PHP base images and runs PIE inside native-architecture containers pinned by digest to resolve compatible stable extension versions.

Resolve all variants without building or publishing:

```sh
python3 scripts/select_images.py --select-all --out /tmp/php-build-matrix.json
```

External extensions use these PIE packages:

| Extension | Package |
| --- | --- |
| Redis | `phpredis/phpredis` |
| AMQP | `php-amqp/php-amqp` |
| Xdebug | `xdebug/xdebug` |
| PCOV | `pecl/pcov` |

`pecl/pcov` is the package identifier; installation does not use the PECL service. Bundled extensions use Docker's PHP configure and install helpers. XHProf is built from a checksum-verified GitHub commit archive. Its upstream tag and module version are recorded separately because they can differ; tag `v2.3.10` reports module version `2.3.9`.

PIE and Composer use versioned PHARs verified with SHA-256. Dependency snapshots include base image digests, tool checksums, extension versions, configure options, XHProf source identity, and build script fingerprints. CI passes the same resolved inputs to test and publication builds, and requires both architectures to pass before updating image tags.

## Build inputs

`EXTENSIONS` is the single list of runtime module names. `DEPENDENCIES` accepts the resolver's `dependencies` JSON for pinned builds. Standalone Docker and Compose builds use the pins in `scripts/build-defaults.json` when `DEPENDENCIES` is omitted. Update that file when refreshing standalone defaults.

PIE is removed after installation. CI blocks PECL endpoints during resolution and builds, and the images do not include PECL clients.

## Tests

Run dependency-selection and publication unit tests:

```sh
python3 -m unittest discover -s tests -v
```

The image workflows also build and test each variant on `linux/amd64` and `linux/arm64`, including extension controls and versions, Redis compression, GD formats, Composer, Node.js, FPM, and custom user IDs.
