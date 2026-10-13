"""Build both protected firmware profiles, then flash LEFT and RIGHT using one programming USB cable."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from tools.demo_boards import CONFIG_PATH, LEGACY_CONFIG_PATH, address, load_boards, save_boards

ROOT = Path(__file__).resolve().parent
ROLES = ("left", "right")


def _pio():
    installed = Path.home() / ".platformio" / "penv" / "Scripts" / "platformio.exe"
    executable = str(installed) if installed.is_file() else shutil.which("platformio")
    if not executable:
        raise FileNotFoundError("PlatformIO not found; expected " + str(installed))
    return [executable]


def _run(command):
    # Inherit the terminal: build colors, upload progress and pairing input stay live.
    print("\n> " + subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _ports():
    # Capture only public port metadata, never serial-monitor or passkey output.
    result = subprocess.run(_pio() + ["device", "list", "--serial", "--json-output"],
                            cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list):
        raise ValueError("Unexpected PlatformIO serial-port data.")
    ports = []
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"COM[1-9][0-9]*", str(row.get("port", "")), re.I):
            raise ValueError("Unexpected PlatformIO COM-port data.")
        port = row["port"].upper()
        if port in ports:
            raise ValueError("PlatformIO returned a duplicate COM port.")
        ports.append(port)
        print(f"{port} | {row.get('description', '')} | {row.get('hwid', '')}")
    if not ports:
        print("No serial ports detected.")
    return ports


def _answer(prompt):
    answer = input(prompt).strip()
    if answer.lower() == "q":
        raise KeyboardInterrupt
    return answer


def _esptool():
    # Use PlatformIO's installed tool and interpreter, without changing its packages.
    core = Path.home() / '.platformio'
    script = core / 'packages' / 'tool-esptoolpy' / 'esptool.py'
    python = core / 'penv' / 'Scripts' / 'python.exe'
    if not script.is_file() or not python.is_file():
        raise FileNotFoundError('PlatformIO ESP32 tools not found under ' + str(core))
    return [str(python), str(script)]


def _bluetooth_address(output):
    # ESP32's default four-universal-address SDK uses factory base + 2 for BT.
    # Firmware has a build assertion for this setting; never use a COM number as identity.
    # https://docs.espressif.com/projects/esp-idf/en/v4.4.8/esp32/api-reference/system/system.html#mac-address
    matches = {value.upper() for value in re.findall(r'^MAC: ((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s*$', output, re.M)}
    if len(matches) != 1:
        raise ValueError('Could not read one unambiguous ESP32 factory MAC; nothing was uploaded.')
    base = address(matches.pop())
    octets = [int(part, 16) for part in base.split(':')]
    if octets[-1] > 253:
        raise ValueError('Unexpected ESP32 factory address range; cannot derive Bluetooth identity.')
    octets[-1] += 2
    return ':'.join(f'{part:02X}' for part in octets)


def _read_board_address(port):
    # ROM read_mac resets the attached ESP, but does not flash or capture application serial/PIN logs.
    result = subprocess.run(_esptool() + ['--chip', 'esp32', '--port', port, 'read_mac'],
                            cwd=ROOT, check=True, capture_output=True, text=True, encoding='utf-8', timeout=30)
    return _bluetooth_address(result.stdout)


def _flash():
    _run([sys.executable, "-m", "tools.generate_dummy_fixtures"])
    base = _pio() + ["run", "-d", str(ROOT / "firmware" / "esp32")]
    # Finish both builds before asking the operator to connect either board.
    for label in ROLES:
        _run(base + ["-e", "firebeetle32-" + label])
    detected = []
    for device_id, label in enumerate(ROLES, start=1):
        swap = "Disconnect LEFT, then connect" if device_id == 2 else "Connect"
        answer = _answer(f"\n{swap} board {device_id}; it will become {label.upper()} / device {device_id}. "
                         "Use a different board for RIGHT; attach labels after upload. Enter / q to cancel: ")
        if answer:
            raise ValueError("Press Enter after connecting the board for this stage, or q to cancel.")
        # Detect afresh after each swap; one programming USB cable can reuse a COM number.
        ports = _ports()
        if not ports:
            raise ValueError("Connect the labelled board with a data cable, then rerun flash.py.")
        port = ports[0] if len(ports) == 1 else _answer("Select the attached board's COM port / q to cancel: ").upper()
        if port not in ports:
            raise ValueError("Select an actual COM port from the displayed list.")
        bluetooth = _read_board_address(port)
        if bluetooth in detected:
            raise ValueError('This is still the first board. Connect a DIFFERENT board and rerun flash.py.')
        if not detected:
            # Block stale capture defaults before an upload can change physical board roles.
            save_boards(bluetooth)
        print(f"Uploading {label.upper()} / device {device_id} on {port}; Bluetooth {bluetooth}.")
        _run(base + ["-e", "firebeetle32-" + label, "-t", "upload", "--upload-port", port])
        detected.append(bluetooth)
        print(f'Upload succeeded: attach {label.upper()} / ID {device_id} label to this board ({bluetooth}).')
    save_boards(*detected)
    print(f'Board mapping saved: {CONFIG_PATH}\nLEFT={detected[0]}\nRIGHT={detected[1]}')
    print("Both uploads succeeded. Disconnect laptop USB and power both boards independently for the BLE demo.")


def _pair(selection):
    print("Existing authenticated bonds are reused. Power the selected board(s).")
    print("First pairing: use --pair left/right with that board's --monitor COMx in another terminal.")
    print("Keep serial passkeys private and off camera.")
    # Delegate to the existing authenticated pairing flow; stop on its first failure.
    for label, address in load_boards():
        if selection in ("both", label):
            print("\nPairing " + label.upper())
            _run([sys.executable, "-m", "laptop.windows_pairing", "--address", address])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--ports", action="store_true", help="Only list current COM ports.")
    modes.add_argument("--boards", action="store_true", help="Show the saved LEFT/RIGHT Bluetooth mapping.")
    modes.add_argument("--pair", nargs="?", const="both", choices=("left", "right", "both"),
                       help="Check or establish authenticated bonds; default: both.")
    modes.add_argument("--monitor", metavar="COMx", help="Private 115200-baud serial monitor in this terminal.")
    args = parser.parse_args(argv)  # --help exits before any hardware operation.
    try:
        if args.ports:
            _ports()
        elif args.boards:
            for device_id, (label, bluetooth) in enumerate(load_boards(), start=1):
                print(f'{label.upper()} / ID {device_id}: {bluetooth}')
            if CONFIG_PATH.exists():
                print(f'Mapping: {CONFIG_PATH}')
            elif LEGACY_CONFIG_PATH.exists():
                print(f'Mapping: {LEGACY_CONFIG_PATH} (existing mapping; next upload writes {CONFIG_PATH})')
            else:
                print('Using legacy defaults; flash.py records new upload order.')
        elif args.pair:
            _pair(args.pair)
        elif args.monitor:
            port = args.monitor.strip().upper()
            if not re.fullmatch(r"COM[1-9][0-9]*", port):
                raise ValueError("Use an actual COM port, such as COM4.")
            print("Private serial monitor: keep passkeys off camera. No log is saved; Ctrl+C exits.")
            _run(_pio() + ["device", "monitor", "--port", port, "--baud", "115200", "--no-reconnect"])
        else:
            _flash()
        return 0
    except KeyboardInterrupt:
        print("\nCancelled; remaining operations were not run.")
        return 130
    except subprocess.CalledProcessError as error:
        print(f"FAILED: command exited with code {error.returncode}; remaining operations were not run.")
        return error.returncode or 1
    except subprocess.TimeoutExpired:
        print('FAILED: ESP32 identity read timed out; close serial monitors and check the data cable.')
        return 2
    except (OSError, ValueError, EOFError) as error:
        print(f"FAILED: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
