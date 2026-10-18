"""Windows passkey pairing for the B07 Communications protected ESP (no Just Works fallback).

The installed Bleak 3.0.1 pair() uses CONFIRM_ONLY and can lower requested
protection. This helper explicitly requests PROVIDE_PIN and authenticated
encryption. Windows bond metadata is checked here; active link security is
additionally enforced by ESP authentication-completion notification gating.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys


def validate_pin(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value):
        raise ValueError("passkey must contain exactly six decimal digits")
    return value


def check_bond(pairing):
    # WinRT DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION = 3.
    if not pairing.is_paired or int(pairing.protection_level) != 3:
        raise RuntimeError("authenticated Windows bond required; run laptop.windows_pairing first")


async def prompt_pin():
    """Cancellable hidden Windows console input, with no blocked input thread."""
    import msvcrt
    if not sys.stdin.isatty():
        raise RuntimeError("pairing passkey input requires an interactive Windows console")
    sys.stdout.write("Enter the six-digit ESP serial passkey (hidden): ")
    sys.stdout.flush()
    value = ""
    while True:
        if msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                sys.stdout.write("\n")
                return validate_pin(value)
            if char == "\x03":
                raise KeyboardInterrupt()
            if char == "\b":
                value = value[:-1]
            elif char in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
            elif char in "0123456789" and len(value) < 6:
                value += char
        await asyncio.sleep(0.05)


async def pair_custom(custom, pin_provider):
    from winrt.windows.devices.enumeration import (
        DevicePairingKinds, DevicePairingProtectionLevel, DevicePairingResultStatus)
    loop = asyncio.get_running_loop()
    pending = set()
    errors = []

    def handler(_sender, args):
        if args.pairing_kind != DevicePairingKinds.PROVIDE_PIN:
            return
        deferral = args.get_deferral()
        async def provide():
            try:
                args.accept_with_pin(validate_pin(await pin_provider()))
            except Exception:
                errors.append("passkey input failed")
            finally:
                deferral.complete()
        future = asyncio.run_coroutine_threadsafe(provide(), loop)
        pending.add(future)

    token = custom.add_pairing_requested(handler)
    try:
        result = await asyncio.wait_for(custom.pair_with_protection_level_async(
            DevicePairingKinds.PROVIDE_PIN,
            DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION), 90.0)
        if errors or result.status not in (
            DevicePairingResultStatus.PAIRED, DevicePairingResultStatus.ALREADY_PAIRED):
            raise RuntimeError("authenticated passkey pairing failed (" + result.status.name +
                               "); deliberate bond recovery may be needed")
    finally:
        custom.remove_pairing_requested(token)
        for future in pending:
            if not future.done():
                future.cancel()


async def _device(address):
    from winrt.windows.devices.bluetooth import BluetoothLEDevice
    compact = address.replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{12}", compact):
        raise ValueError("expected Bluetooth MAC address")
    device = await BluetoothLEDevice.from_bluetooth_address_async(int(compact, 16))
    if device is None:
        raise RuntimeError("Windows could not open the discovered BLE device")
    return device


async def _information(device):
    from winrt.windows.devices.enumeration import DeviceInformation
    return await DeviceInformation.create_from_id_async(device.device_information.id)


async def require_authenticated_bond(client):
    device = await _device(client.address)
    try:
        info = await _information(device)
        check_bond(info.pairing)
    finally:
        device.close()


async def pair_address(address, pin_provider):
    device = await _device(address)
    try:
        info = await _information(device)
        if info.pairing.is_paired:
            check_bond(info.pairing)
            return
        await pair_custom(info.pairing.custom, pin_provider)
        info = await _information(device)
        check_bond(info.pairing)
    finally:
        device.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", help="otherwise discover exact B07 Communications advertised service")
    args = parser.parse_args()
    async def run():
        address = args.address
        if address is None:
            from bleak import BleakScanner
            from laptop.bridge import SERVICE_UUID
            found = await BleakScanner.find_device_by_filter(
                lambda _d, a: SERVICE_UUID in [x.lower() for x in a.service_uuids or []], timeout=8)
            if found is None:
                raise RuntimeError("B07 Communications ESP not found")
            address = found.address
        await pair_address(address, prompt_pin)
        print(json.dumps({"authenticated_bond": True, "address": address}))
    asyncio.run(run())


if __name__ == "__main__":
    main()
