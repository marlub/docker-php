"""Offline Bake fixtures; fake additional image pins must never be built or published."""

from pathlib import Path
import json
import re


def add_family_variants(root: Path, extra_major=26):
    path = root / 'build/php/8.4.hcl'
    text = path.read_text()
    names = ['php-8-4-trixie', 'php-8-4-trixie-node24', 'php-8-4-alpine', 'php-8-4-alpine-node24', f'php-8-4-alpine-node{extra_major}',
             'php-8-4-bookworm', 'php-8-4-bookworm-node24', f'php-8-4-bookworm-node{extra_major}']
    text = re.sub(r'(group "ci-8-4" \{\n  targets = )\[[^\n]+\]',
                  lambda m: m[1] + '[' + ', '.join('"' + name + '"' for name in names) + ']', text)
    text += f'''
target "php-8-4-alpine-node{extra_major}" {{
  inherits = ["_php-8-4-alpine"]
  target = "php-node"
  args = {{ NODE_BASE_IMAGE = "node:{extra_major}.1.0-alpine3.24@sha256:{'1' * 64}" }}
  tags = ["docker-php:8.4-fpm-alpine3.24-node{extra_major}"]
}}
target "_php-8-4-bookworm" {{
  inherits = ["_common"]
  dockerfile = "Dockerfile.fixture"
  labels = {{ "io.github.marlub.docker-php.base-target" = "php-8-4-bookworm" }}
  args = extension_args(merge(PHP_8_4, {{
    PHP_BASE_IMAGE = "php:8.4.27-fpm-bookworm@sha256:{'b' * 64}"
  }}))
}}
target "php-8-4-bookworm" {{
  inherits = ["_php-8-4-bookworm"]
  target = "php"
  tags = ["docker-php:8.4-fpm-bookworm"]
}}
'''
    for major in (24, extra_major):
        text += f'''
target "php-8-4-bookworm-node{major}" {{
  inherits = ["_php-8-4-bookworm"]
  target = "php-node"
  args = {{ NODE_BASE_IMAGE = "node:{major}.1.0-bookworm-slim@sha256:{'2' * 64}" }}
  tags = ["docker-php:8.4-fpm-bookworm-node{major}"]
}}
'''
    path.write_text(text)
    (root / 'Dockerfile.fixture').write_text((root / 'Dockerfile').read_text())


def add_os_release_variants(root: Path):
    """Add an older Alpine family and locks, alongside the existing Debian fixture."""
    add_family_variants(root)
    path = root / 'build/php/8.4.hcl'
    text = path.read_text()
    names = ['php-8-4-alpine3-23', 'php-8-4-alpine3-23-node24', 'php-8-4-alpine3-23-node28']
    text = re.sub(r'(group "ci-8-4" \{\n  targets = \[[^\n]+)\]',
                  lambda m: m[1] + ', ' + ', '.join('"' + name + '"' for name in names) + ']', text)
    text += f'''
target "_php-8-4-alpine3-23" {{
  inherits = ["_common"]
  labels = {{ "io.github.marlub.docker-php.base-target" = "php-8-4-alpine3-23" }}
  args = extension_args(merge(PHP_8_4, {{
    PHP_BASE_IMAGE = "php:8.4.27-fpm-alpine3.23@sha256:{'c' * 64}"
    RUNTIME_PACKAGES = "libffi"
    BUILD_PACKAGES = "libffi-dev"
  }}))
}}
target "php-8-4-alpine3-23" {{
  inherits = ["_php-8-4-alpine3-23"]
  target = "php"
  tags = ["docker-php:8.4-fpm-alpine3.23"]
}}
'''
    for major in (24, 28):
        text += f'''
target "php-8-4-alpine3-23-node{major}" {{
  inherits = ["_php-8-4-alpine3-23"]
  target = "php-node"
  args = {{ NODE_BASE_IMAGE = "node:{major}.1.0-alpine3.23@sha256:{'d' * 64}" }}
  tags = ["docker-php:8.4-fpm-alpine3.23-node{major}"]
}}
'''
    path.write_text(text)
    for backend, source, release, field in (('apk', '3.24', '3.23', 'alpine'), ('apt', 'trixie', 'bookworm', 'debian')):
        for arch in ('amd64', 'arm64'):
            lock = json.loads((root / f'build/{backend}/{source}/{arch}.json').read_text())
            lock[field] = release
            destination = root / f'build/{backend}/{release}/{arch}.json'
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(lock, indent=2) + '\n')
