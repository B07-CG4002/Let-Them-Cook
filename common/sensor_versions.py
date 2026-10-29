"""The sensor packet versions Communications carries, and the codec module of each.

3: 38 bytes, 11 values, LCS3 (common.sensor; the default everywhere).
4: the three-flex gloves, 40 bytes, 12 values, LCS4 (common.sensor_v4).
5: the three-flex gloves with the wider gyro range, the same 40 bytes and 12
   values as 4 but gyro at 10 counts per deg/s instead of 100, LCS5
   (common.sensor_v5).
The three codec modules share one interface (VERSION, CHANNELS, SCALES,
PACKET_SIZE, SENSOR_MTU_FLOOR, MAX_PACKETS_PER_NOTIFICATION, SensorPacket,
encode_packet, decode_packet, split_notification, physical_values,
DUMMY_FIXTURES). Each accepts its own version only, so a packet is never read
with another version's scales; physical units are restored once, on the
Ultra96 (ultra96.server), with the codec of the service's version.
"""
from common import sensor, sensor_v4, sensor_v5

CODECS = {codec.VERSION: codec for codec in (sensor, sensor_v4, sensor_v5)}
VERSIONS = tuple(sorted(CODECS))
DEFAULT = sensor.VERSION
# Twelve channels (ax..flex3): the three-flex gloves. The hand12 worker and the
# HAND_EVENT path need one of these; their channel names are identical.
TWELVE_CHANNEL = (sensor_v4.VERSION, sensor_v5.VERSION)
assert all(CODECS[version].CHANNELS == sensor_v4.CHANNELS for version in TWELVE_CHANNEL)


def check(version):
    """Return version if Communications carries it; ValueError otherwise."""
    if type(version) is not int or version not in CODECS:
        raise ValueError("sensor_version must be 3, 4 or 5")
    return version


def codec(version):
    """The codec module of a sensor packet version (common.sensor, sensor_v4 or sensor_v5)."""
    return CODECS[check(version)]


def source_stats_magic(version):
    """The source-statistics marker a board of this version reports: LCS3, LCS4 or LCS5."""
    return b"LCS" + str(check(version)).encode()


def describe(version):
    """A short description for help texts and printouts, e.g. 'v5 (40 bytes, 12 values, gyro x10, LCS5)'."""
    selected = codec(version)
    return "v{} ({} bytes, {} values, gyro x{}, {})".format(
        version, selected.PACKET_SIZE, len(selected.CHANNELS), selected.SCALES[3],
        source_stats_magic(version).decode())
