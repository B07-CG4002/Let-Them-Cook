"""Disable a WinRT remote CCCD without discarding its local notification handler."""
import sys


async def disable_notifications_remotely(characteristic, *, platform=None):
    """Return False for other backends; a supported native write must succeed.

    Bleak's normal stop_notify removes its handler immediately after the CCCD
    write. This first phase keeps that handler for a bounded source-counter
    drain. The connection owner must still call stop_notify and disconnect.
    """
    if (sys.platform if platform is None else platform) != "win32":
        return False
    native = getattr(characteristic, "obj", None)
    write = getattr(native, "write_client_characteristic_configuration_descriptor_with_result_async", None)
    if not callable(write):
        return False
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattClientCharacteristicConfigurationDescriptorValue, GattCommunicationStatus)
    result = await write(GattClientCharacteristicConfigurationDescriptorValue.NONE)
    if result.status != GattCommunicationStatus.SUCCESS:
        raise OSError("remote notification disable was not acknowledged")
    return True
