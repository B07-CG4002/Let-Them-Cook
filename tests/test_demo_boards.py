"""Physical upload order must become the shared, persistent device mapping."""
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

import demo
import flash
from test_flash import environment, uploads


def board_module(monkeypatch, tmp_path):
    assert importlib.util.find_spec('tools.demo_boards'), 'shared board configuration is missing'
    module = importlib.import_module('tools.demo_boards')
    monkeypatch.setattr(module, 'CONFIG_PATH', tmp_path / 'boards.json')
    monkeypatch.setattr(module, 'LEGACY_CONFIG_PATH', tmp_path / 'legacy' / 'boards.json', raising=False)
    return module


def test_legacy_mapping_is_read_until_new_mapping_is_saved(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    legacy = boards.LEGACY_CONFIG_PATH
    legacy.parent.mkdir()
    legacy.write_text(json.dumps(dict(version=1, status='ready',
                                     left='24:6F:28:00:00:22', right='24:6F:28:00:00:42')))
    assert dict(boards.load_boards()) == {
        'left': '24:6F:28:00:00:22', 'right': '24:6F:28:00:00:42'}
    boards.save_boards('24:6F:28:00:00:62', '24:6F:28:00:00:82')
    assert dict(boards.load_boards()) == {
        'left': '24:6F:28:00:00:62', 'right': '24:6F:28:00:00:82'}
    assert json.loads(legacy.read_text())['left'] == '24:6F:28:00:00:22'


def test_new_boards_persist_in_upload_order_and_drive_both_capture_and_pairing(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    module, commands, _ = environment(monkeypatch)
    detected = iter(['24:6F:28:00:00:22', '24:6F:28:00:00:42'])
    monkeypatch.setattr(module, '_read_board_address', lambda port: next(detected), raising=False)
    monkeypatch.setattr(module, 'save_boards', boards.save_boards, raising=False)
    monkeypatch.setattr(module, 'load_boards', boards.load_boards, raising=False)
    assert module.main([]) == 0
    assert dict(boards.load_boards()) == {'left': '24:6F:28:00:00:22', 'right': '24:6F:28:00:00:42'}
    captured = []
    monkeypatch.setattr(demo, 'run_capture', lambda args: captured.append(args) or 0)
    assert demo.main(['run']) == 0
    assert (captured[0].left_address, captured[0].right_address) == tuple(dict(boards.load_boards()).values())
    assert module.main(['--pair']) == 0
    assert [c[-1] for c in commands if 'laptop.windows_pairing' in c] == list(dict(boards.load_boards()).values())


def test_same_board_twice_is_rejected_before_right_upload_and_capture_stays_blocked(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    module, commands, _ = environment(monkeypatch)
    monkeypatch.setattr(module, '_read_board_address', lambda port: '24:6F:28:00:00:22', raising=False)
    monkeypatch.setattr(module, 'save_boards', boards.save_boards, raising=False)
    monkeypatch.setattr(module, 'load_boards', boards.load_boards, raising=False)
    assert module.main([]) != 0
    assert len(uploads(commands)) == 1
    with pytest.raises(ValueError, match='incomplete'):
        boards.load_boards()
    monkeypatch.setattr(demo, 'run_capture', lambda _: pytest.fail('incomplete flashing must block capture'))
    assert demo.main(['run']) != 0


@pytest.mark.parametrize('failed_role', ['left', 'right'])
def test_upload_failure_does_not_publish_a_ready_mapping(monkeypatch, tmp_path, failed_role):
    boards = board_module(monkeypatch, tmp_path)
    boards.save_boards('24:6F:28:00:00:02', '24:6F:28:00:00:12')
    module, commands, _ = environment(monkeypatch, fail=lambda c: 'upload' in c and 'firebeetle32-'+failed_role in c)
    addresses = iter(['24:6F:28:00:00:22', '24:6F:28:00:00:42'])
    monkeypatch.setattr(module, '_read_board_address', lambda port: next(addresses), raising=False)
    monkeypatch.setattr(module, 'save_boards', boards.save_boards, raising=False)
    assert module.main([]) != 0
    with pytest.raises(ValueError, match='incomplete'):
        boards.load_boards()


def test_default_compatibility_and_config_validation(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    assert dict(boards.load_boards())['left'] == '38:18:2B:19:82:AE'
    for content in ['{broken', '{}', json.dumps({'version': 1, 'status': 'ready', 'left': 'bad', 'right': 'bad'})]:
        boards.CONFIG_PATH.write_text(content)
        with pytest.raises(ValueError):
            boards.load_boards()
    with pytest.raises(ValueError):
        boards.save_boards('24:6F:28:00:00:22', '24:6f:28:00:00:22')


def test_mapping_write_failure_keeps_previous_complete_file(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    boards.save_boards('24:6F:28:00:00:22', '24:6F:28:00:00:42')
    before = boards.CONFIG_PATH.read_bytes()
    monkeypatch.setattr(boards.os, 'replace', lambda *_: (_ for _ in ()).throw(OSError('write failed')))
    with pytest.raises(OSError):
        boards.save_boards('24:6F:28:00:00:62')
    assert boards.CONFIG_PATH.read_bytes() == before
    assert list(tmp_path.iterdir()) == [boards.CONFIG_PATH]


@pytest.mark.parametrize('base,expected', [('38:18:2b:19:82:ac','38:18:2B:19:82:AE'), ('38:18:2b:18:9d:68','38:18:2B:18:9D:6A')])
def test_esp32_factory_mac_is_converted_to_the_project_bluetooth_address(base, expected):
    assert hasattr(flash, '_bluetooth_address'), 'serial hardware identification is missing'
    assert flash._bluetooth_address('Chip is ESP32\nMAC: '+base+'\nMAC: '+base+'\n') == expected


@pytest.mark.parametrize('output', ['', 'MAC: invalid', 'MAC: ff:ff:ff:ff:ff:ff', 'MAC: 00:00:00:00:00:00',
                                      'MAC: 24:6f:28:00:00:ff', 'MAC: 24:6f:28:00:00:20\nMAC: 24:6f:28:00:00:40'])
def test_ambiguous_or_invalid_chip_identity_is_rejected(output):
    assert hasattr(flash, '_bluetooth_address'), 'serial hardware identification is missing'
    with pytest.raises(ValueError):
        flash._bluetooth_address(output)


def test_read_identity_uses_only_rom_read_mac_with_explicit_chip_and_port(monkeypatch):
    assert hasattr(flash, '_read_board_address'), 'serial hardware identification is missing'
    monkeypatch.setattr(flash, '_esptool', lambda: ['python', 'esptool.py'])
    seen = []
    def run(command, **kwargs):
        seen.append(command)
        assert kwargs['check'] and kwargs['capture_output'] and kwargs['timeout'] == 30
        return subprocess.CompletedProcess(command, 0, 'MAC: 38:18:2b:19:82:ac\n')
    monkeypatch.setattr(flash.subprocess, 'run', run)
    assert flash._read_board_address('COM4') == '38:18:2B:19:82:AE'
    assert seen == [['python','esptool.py','--chip','esp32','--port','COM4','read_mac']]


def test_board_display_is_read_only(monkeypatch, tmp_path, capsys):
    boards = board_module(monkeypatch, tmp_path)
    boards.save_boards('24:6F:28:00:00:22', '24:6F:28:00:00:42')
    module, commands, prompts = environment(monkeypatch)
    monkeypatch.setattr(module, 'load_boards', boards.load_boards)
    assert module.main(['--boards']) == 0
    output = capsys.readouterr().out
    assert 'LEFT / ID 1: 24:6F:28:00:00:22' in output
    assert 'RIGHT / ID 2: 24:6F:28:00:00:42' in output
    assert not commands and not prompts


def test_identity_failure_prevents_upload_and_preserves_existing_mapping(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    boards.save_boards('24:6F:28:00:00:22', '24:6F:28:00:00:42')
    original = boards.CONFIG_PATH.read_bytes()
    module, commands, _ = environment(monkeypatch)
    monkeypatch.setattr(module, 'save_boards', boards.save_boards)
    monkeypatch.setattr(module, '_read_board_address', lambda _: (_ for _ in ()).throw(ValueError('identity unavailable')))
    assert module.main([]) != 0
    assert uploads(commands) == []
    assert boards.CONFIG_PATH.read_bytes() == original


def test_explicit_address_overrides_remain_available(monkeypatch, tmp_path):
    boards = board_module(monkeypatch, tmp_path)
    boards.save_boards('24:6F:28:00:00:22', '24:6F:28:00:00:42')
    calls = []
    monkeypatch.setattr(demo, 'run_capture', lambda args: calls.append(args) or 0)
    assert demo.main(['live','--left-address','24:6F:28:00:00:62']) == 0
    assert (calls[0].left_address,calls[0].right_address) == ('24:6F:28:00:00:62','24:6F:28:00:00:42')
