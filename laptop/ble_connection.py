"""Force real Windows GATT discovery after bonding instead of cached MTU 23."""
from bleak import BleakClient


def make_ble_client(*args, **kwargs):
    options = dict(kwargs.pop("winrt", {}))
    options["use_cached_services"] = False
    return BleakClient(*args, winrt=options, **kwargs)
