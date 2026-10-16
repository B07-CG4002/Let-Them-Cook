"""Generic result rules (gesture label, confidence) and physical scaling of the 11 channels."""
import asyncio
from fractions import Fraction

import pytest

from common.sensor import CHANNEL_COUNT, CHANNELS, SCALES, physical_values
from common.wire import ProtocolError, encode_frame, read_frame
from ultra96.protocol import (DUMMY_GESTURES, MAX_GESTURE_LENGTH, valid_confidence, valid_gesture,
                              validate_message)


def result(**change):
    message = dict(v=3, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1, boot_id=7,
                   seq=0, request_id=None, result_id="1:7:0", gesture="CHOP", confidence=0.5)
    message.update(change)
    return message


# --- Gesture label: 1..32 characters from [A-Za-z0-9_-] --------------------------------------

MODEL_CLASSES = ("PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT")


@pytest.mark.parametrize("label", MODEL_CLASSES + DUMMY_GESTURES + (
    "A" * 32, "a", "Z", "0", "9", "-", "_", "a-b_C9", "fire-ext_2", "--__", "0123456789"))
def test_labels_matching_the_rule_are_accepted(label):
    assert valid_gesture(label)
    assert validate_message(result(gesture=label))["gesture"] == label


@pytest.mark.parametrize("label", [
    "", "A" * 33, " CHOP", "CHOP ", "FIRE EXT", "CHOP\n", "CHOP\t", "CHOP!", "CH.OP", "CH:OP",
    "É", "ÉCLAIR", "ß", "ı", "K", "ſ",  # non-ASCII letters
    "１", "٣", "²",  # fullwidth, Arabic-Indic and superscript digits
    "ＣHOP",  # fullwidth letter
])
def test_labels_outside_the_rule_are_rejected(label):
    assert not valid_gesture(label)
    with pytest.raises(ProtocolError, match="gesture"):
        validate_message(result(gesture=label))


@pytest.mark.parametrize("label", [None, 5, 5.0, True, b"CHOP", ["CHOP"], {"CHOP": 1}])
def test_non_string_labels_are_rejected(label):
    assert not valid_gesture(label)
    with pytest.raises(ProtocolError):
        validate_message(result(gesture=label))


def test_label_length_limit_is_32():
    assert MAX_GESTURE_LENGTH == 32
    assert valid_gesture("x" * 32) and not valid_gesture("x" * 33)


# --- Confidence: a finite JSON number (not a boolean) in [0, 1] -------------------------------

@pytest.mark.parametrize("confidence", [0, 1, 0.0, 1.0, 0.5, -0.0, 5e-324, 1 - 2 ** -53, 0.875])
def test_confidence_in_the_unit_interval_is_accepted(confidence):
    assert valid_confidence(confidence)
    assert validate_message(result(confidence=confidence))["confidence"] == confidence


@pytest.mark.parametrize("confidence", [
    1.0000001, 1 + 2 ** -52, -1e-300, -1, 2, True, False, "0.5", "1", None, [0.5],
    float("nan"), float("inf"), float("-inf"), 10 ** 400, -10 ** 400, 2 ** 64, Fraction(1, 2),
])
def test_confidence_outside_the_rule_is_rejected_without_overflow(confidence):
    assert not valid_confidence(confidence)
    with pytest.raises(ProtocolError, match="confidence|non-finite"):
        validate_message(result(confidence=confidence))


def test_negative_zero_and_integer_bounds_survive_strict_json():
    async def round_trip(message):
        reader = asyncio.StreamReader()
        reader.feed_data(encode_frame(message))
        reader.feed_eof()
        return await read_frame(reader)

    for confidence in (-0.0, 0, 1):
        decoded = validate_message(asyncio.run(round_trip(result(confidence=confidence))))
        assert decoded["confidence"] == confidence
        assert type(decoded["confidence"]) is type(confidence)


# --- physical_values: int16 / scale per channel ----------------------------------------------

def test_channel_names_and_scales_are_the_agreed_layout():
    assert CHANNEL_COUNT == 11
    assert CHANNELS == ("ax", "ay", "az", "gx", "gy", "gz", "yaw", "pitch", "roll", "flex1", "flex2")
    assert SCALES == (10000, 10000, 10000, 100, 100, 100, 100, 100, 100, 10000, 10000)


def test_physical_values_divides_each_channel_by_its_own_scale():
    raw = [10000, -10000, 5000, 100, -100, 25050, 18000, -9000, -18000, 0, 10000]
    values = physical_values(raw)
    assert values == (1.0, -1.0, 0.5, 1.0, -1.0, 250.5, 180.0, -90.0, -180.0, 0.0, 1.0)
    assert type(values) is tuple and all(type(value) is float for value in values)
    assert physical_values(tuple(raw)) == values
    for index, scale in enumerate(SCALES):
        unit = [0] * 11
        unit[index] = 1
        expected = [0.0] * 11
        expected[index] = 1 / scale
        assert physical_values(unit) == tuple(expected), CHANNELS[index]
    assert physical_values([32767] * 11) == tuple(32767 / scale for scale in SCALES)
    assert physical_values([-32768] * 11)[0] == -3.2768
    assert physical_values([-32768] * 11)[3] == -327.68


@pytest.mark.parametrize("raw", [
    [0] * 10, [0] * 12, [], [32768] + [0] * 10, [-32769] + [0] * 10, [1.0] + [0] * 10,
    [True] + [0] * 10, ["1"] + [0] * 10, [None] + [0] * 10, None, "0" * 11, b"\x00" * 11,
    (value for value in [0] * 11),
])
def test_physical_values_rejects_anything_but_eleven_int16(raw):
    with pytest.raises(ValueError):
        physical_values(raw)
