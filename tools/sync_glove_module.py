"""Copy the glove owner's sensor module into the firmware build, unchanged.

The module (Siqiang) lives in hardware/gloves/integration_files/, which
Communications must not edit. Its glove_sensor.cpp includes
"../../../movement_model_params.h", which resolves only in a PlatformIO
library layout: lib/<name>/src/glove_sensor.cpp with the header at the
project root. So the firmware builds byte-identical copies:

    hardware/gloves/integration_files/glove_sensor.cpp  -> firmware/esp32/lib/glove_sensor/src/
    hardware/gloves/integration_files/glove_sensor.h    -> firmware/esp32/lib/glove_sensor/src/
    hardware/gloves/integration_files/movement_model_params.h -> firmware/esp32/ and lib/glove_sensor/src/

Never edit the copies; run this after the module changes in the team repo.

    python -m tools.sync_glove_module           # copy
    python -m tools.sync_glove_module --check   # exit 1 if a copy differs from its source
"""
import argparse
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hardware" / "gloves" / "integration_files"
FIRMWARE = ROOT / "firmware" / "esp32"
COPIES = (
    ("glove_sensor.cpp", FIRMWARE / "lib" / "glove_sensor" / "src" / "glove_sensor.cpp"),
    ("glove_sensor.h", FIRMWARE / "lib" / "glove_sensor" / "src" / "glove_sensor.h"),
    ("movement_model_params.h", FIRMWARE / "movement_model_params.h"),
    # Also beside the .cpp, so the plain #include "movement_model_params.h" works too.
    ("movement_model_params.h", FIRMWARE / "lib" / "glove_sensor" / "src" / "movement_model_params.h"),
)


def _text(path):
    # Compare content, not the working copy's line endings (core.autocrlf).
    return path.read_bytes().replace(b"\r\n", b"\n")


def differences(source=SOURCE):
    """Names whose copy is missing or differs from the module in hardware/."""
    return [name for name, copy in COPIES
            if not copy.is_file() or _text(Path(source) / name) != _text(copy)]


def sync(source=SOURCE):
    for name, copy in COPIES:
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(source) / name, copy)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only compare; change nothing")
    args = parser.parse_args(argv)
    for name, _ in COPIES:
        if not (SOURCE / name).is_file():
            print(f"FAILED: {SOURCE / name} not found; merge the team main first", file=sys.stderr)
            return 2
    if args.check:
        stale = differences(SOURCE)
        for name in stale:
            print(f"out of date: {name} (run python -m tools.sync_glove_module)")
        return 1 if stale else 0
    sync(SOURCE)
    print("Copied the glove module into firmware/esp32 (lib/glove_sensor and movement_model_params.h).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
