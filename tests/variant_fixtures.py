"""Offline Bake fixtures; fake additional image pins must never be built or published."""

from pathlib import Path
import re


def add_family_variants(root: Path, extra_major=26):
    path = root / 'build/php/8.4.hcl'
    text = path.read_text()
    names = ['php-8-4', 'php-8-4-node24', f'php-8-4-node{extra_major}',
             'php-8-4-bookworm', 'php-8-4-bookworm-node24', f'php-8-4-bookworm-node{extra_major}']
    text = re.sub(r'(group "ci-8-4" \{\n  targets = )\[[^\n]+\]',
                  lambda m: m[1] + '[' + ', '.join('"' + name + '"' for name in names) + ']', text)
    text += f'''
target "php-8-4-node{extra_major}" {{
  inherits = ["_php-8-4"]
  target = "php-node"
  args = {{ NODE_BASE_IMAGE = "node:{extra_major}.1.0-alpine3.24@sha256:{'1' * 64}" }}
  tags = ["docker-php:8.4-fpm-alpine-node{extra_major}"]
}}
target "_php-8-4-bookworm" {{
  inherits = ["_common"]
  dockerfile = "Dockerfile.debian"
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
    (root / 'Dockerfile.debian').write_text((root / 'Dockerfile').read_text())
