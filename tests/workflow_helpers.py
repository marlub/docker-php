"""Read and exercise individual workflow shell steps without a YAML dependency."""

from pathlib import Path
import re
import textwrap


ROOT = Path(__file__).resolve().parent.parent
BUILD_ACTION = ROOT / '.github/actions/build-group/action.yml'
IMAGE_WORKFLOW = ROOT / '.github/workflows/template-images.yml'


def named_step(path, name):
    content = path.read_text()
    match = re.search(r'^( +)- name: ' + re.escape(name) + r'\n', content, re.M)
    if match is None:
        raise AssertionError(f'Missing step {name!r} in {path}')
    remainder = content[match.end():]
    following = re.search(r'^' + match[1] + r'- ', remainder, re.M)
    return remainder[:following.start()] if following else remainder


def step_script(path, name):
    step = named_step(path, name)
    match = re.search(r'^( +)run: \|\n', step, re.M)
    if match is None:
        raise AssertionError(f'Missing shell script in {name!r}')
    # Only the block scalar; exclude any following job or metadata field.
    lines = []
    for line in step[match.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= len(match[1]):
            break
        lines.append(line)
    return textwrap.dedent('\n'.join(lines)) + '\n'
