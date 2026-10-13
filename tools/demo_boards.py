"""One local mapping shared by sequential flashing, pairing and capture."""
import json
import os
from pathlib import Path
import re
import tempfile


CONFIG_PATH = Path(__file__).resolve().parents[1] / '.comms-local' / 'boards.json'
LEGACY_CONFIG_PATH = Path(__file__).resolve().parents[1] / '.week7-local' / 'boards.json'
DEFAULT_BOARDS = (('left', '38:18:2B:19:82:AE'), ('right', '38:18:2B:18:9D:6A'))


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', value):
        raise ValueError('Invalid board Bluetooth address.')
    if int(value[:2], 16) & 1 or int(value.replace(':', ''), 16) == 0:
        raise ValueError('Board address must be a nonzero unicast address.')
    return value.upper()


def load_boards():
    path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if not path.exists():
        return DEFAULT_BOARDS
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('Invalid board mapping version.')
        if data.get('status') == 'incomplete':
            raise ValueError('Board setup is incomplete; finish python flash.py before pairing or capture.')
        if data.get('status') != 'ready':
            raise ValueError('Invalid board mapping status.')
        left, right = address(data.get('left')), address(data.get('right'))
        if left == right:
            raise ValueError('LEFT and RIGHT must be different physical boards.')
        return (('left', left), ('right', right))
    except ValueError as error:
        raise ValueError(f'{path}: {error}') from error


def save_boards(left, right=None):
    left = address(left)
    right = address(right) if right is not None else None
    if left == right:
        raise ValueError('LEFT and RIGHT must be different physical boards.')
    data = dict(version=1, status='ready' if right is not None else 'incomplete', left=left, right=right)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CONFIG_PATH.parent,
                                         prefix='boards-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, CONFIG_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
