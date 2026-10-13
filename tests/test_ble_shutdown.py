"""Native CCCD acknowledgement and portable fallback; no hardware access."""
import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from laptop.ble_shutdown import disable_notifications_remotely


@pytest.mark.parametrize("platform,native", [("linux", True), ("darwin", True), ("win32", False)])
def test_other_backends_or_missing_native_capability_preserve_normal_stop(platform, native):
    async def unexpected(_value):
        raise AssertionError("native API must not run")
    characteristic = SimpleNamespace(obj=SimpleNamespace(
        write_client_characteristic_configuration_descriptor_with_result_async=unexpected)) if native else object()
    assert asyncio.run(disable_notifications_remotely(characteristic, platform=platform)) is False


@pytest.mark.parametrize("status", ["SUCCESS", "PROTOCOL_ERROR"])
def test_supported_native_write_requires_explicit_success(monkeypatch, status):
    name = "winrt.windows.devices.bluetooth.genericattributeprofile"
    for prefix in ("winrt", "winrt.windows", "winrt.windows.devices", "winrt.windows.devices.bluetooth"):
        if prefix not in sys.modules:
            module = ModuleType(prefix)
            module.__path__ = []
            monkeypatch.setitem(sys.modules, prefix, module)
    module = ModuleType(name)
    module.GattClientCharacteristicConfigurationDescriptorValue = SimpleNamespace(NONE="NONE")
    module.GattCommunicationStatus = SimpleNamespace(SUCCESS="SUCCESS")
    monkeypatch.setitem(sys.modules, name, module)
    calls = []
    async def write(value):
        calls.append(value)
        return SimpleNamespace(status=status)
    characteristic = SimpleNamespace(obj=SimpleNamespace(
        write_client_characteristic_configuration_descriptor_with_result_async=write))
    if status == "SUCCESS":
        assert asyncio.run(disable_notifications_remotely(characteristic, platform="win32")) is True
    else:
        with pytest.raises(OSError):
            asyncio.run(disable_notifications_remotely(characteristic, platform="win32"))
    assert calls == ["NONE"]
