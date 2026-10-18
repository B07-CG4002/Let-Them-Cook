from pathlib import Path

from tools import sync_glove_module

ROOT = Path(__file__).resolve().parents[1]


def test_firmware_copies_match_the_glove_owners_module():
    # The firmware builds copies; they must be the module in hardware/, byte for byte.
    assert sync_glove_module.differences() == []


def test_check_reports_a_stale_copy(tmp_path, monkeypatch, capsys):
    source = tmp_path / "integration_files"
    source.mkdir()
    for name, _ in sync_glove_module.COPIES:
        (source / name).write_bytes((sync_glove_module.SOURCE / name).read_bytes())
    assert sync_glove_module.differences(source) == []
    (source / "glove_sensor.cpp").write_bytes(b"// changed upstream\n")
    assert sync_glove_module.differences(source) == ["glove_sensor.cpp"]
    monkeypatch.setattr(sync_glove_module, "SOURCE", source)
    assert sync_glove_module.main(["--check"]) == 1
    assert "glove_sensor.cpp" in capsys.readouterr().out


def test_only_the_glove_environments_link_the_module():
    ini = (ROOT / "firmware" / "esp32" / "platformio.ini").read_text(encoding="utf-8")
    sections = {}
    current = None
    for line in ini.splitlines():
        if line.startswith("["):
            current = line.strip("[]")
            sections[current] = []
        elif current is not None:
            sections[current].append(line.strip())
    assert "lib_ignore = glove_sensor" in sections["env"]
    for side, device in (("left", 1), ("right", 2)):
        glove = sections["env:firebeetle32-%s-glove" % side]
        assert "lib_ignore =" in glove and "-DCOMMS_GLOVE_MODULE=1" in glove
        assert "-DCOMMS_DEVICE_ID=%d" % device in glove
        assert not any("lib_ignore" in line for line in sections["env:firebeetle32-%s" % side])
