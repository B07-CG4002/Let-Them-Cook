"""Public WinRT connection preferences own only their independently opened handles."""
import asyncio
from enum import IntEnum
import importlib.util
from types import SimpleNamespace

import pytest


class Status(IntEnum):
    UNSPECIFIED = 0
    SUCCESS = 1
    DEVICE_NOT_AVAILABLE = 2
    ACCESS_DENIED = 3


PREFERENCE = object()


class Request:
    def __init__(self, status=Status.SUCCESS, *, close_error=False):
        self.status = status
        self.closed = 0
        self.close_error = close_error

    def close(self):
        self.closed += 1
        if self.close_error:
            raise OSError("request close failed")


class Device:
    def __init__(self, status=Status.SUCCESS, intervals=(48, 12), *, request_error=None,
                 close_error=False):
        self.request = Request(status)
        self.intervals = list(intervals)
        self.closed = 0
        self.requested = []
        self.request_error = request_error
        self.close_error = close_error

    def request_preferred_connection_parameters(self, preference):
        self.requested.append(preference)
        if self.request_error:
            raise self.request_error
        return self.request

    def get_connection_parameters(self):
        value = self.intervals.pop(0) if len(self.intervals) > 1 else self.intervals[0]
        return SimpleNamespace(connection_interval=value, connection_latency=2, link_timeout=200)

    def close(self):
        self.closed += 1
        if self.close_error:
            raise OSError("device close failed")


async def configure(device, **kwargs):
    from laptop.ble_parameters import prefer_throughput
    async def opened(address):
        assert address == "AA:BB:CC:DD:EE:FF"
        return device
    return await prefer_throughput(SimpleNamespace(address="AA:BB:CC:DD:EE:FF"),
        source_rate=25, platform="win32", device_factory=opened,
        preferred_parameters=PREFERENCE, **kwargs)


def test_connection_preference_implementation_exists():
    assert importlib.util.find_spec("laptop.ble_parameters") is not None


def test_success_keeps_request_until_close_and_reports_actual_winrt_units():
    async def run():
        device = Device()
        preference = await configure(device)
        evidence = preference.report()
        assert evidence["requested"] is True
        assert evidence["request_status"] == "SUCCESS"
        assert evidence["request_status_code"] == 1
        assert evidence["request_accepted"] is True
        assert evidence["actual_interval_ms"] == 15.0
        assert evidence["actual_latency_events"] == 2
        assert evidence["actual_supervision_timeout_ms"] == 2000
        assert evidence["actual_parameters_changed"] is True
        assert device.requested == [PREFERENCE]
        assert device.closed == device.request.closed == 0
        preference.close()
        preference.close()
        assert device.closed == device.request.closed == 1
        assert preference.report()["released"] is True
    asyncio.run(run())


def test_accepted_request_does_not_claim_negotiated_interval_or_speed():
    async def run():
        device = Device(intervals=(48,))
        preference = await asyncio.wait_for(configure(device, observation_timeout=0.05), 0.5)
        evidence = preference.report()
        assert evidence["request_accepted"] is True
        assert evidence["actual_interval_ms"] == 60.0
        assert evidence["actual_parameters_changed"] is False
        assert evidence["diagnostic"] == "accepted_without_observed_parameter_change"
        assert evidence["observation_elapsed_seconds"] >= 0.05
        preference.close()
    asyncio.run(run())


@pytest.mark.parametrize("status", [Status.DEVICE_NOT_AVAILABLE, Status.ACCESS_DENIED])
def test_denied_and_unavailable_requests_are_reported_and_handles_released(status):
    async def run():
        device = Device(status, intervals=(48,))
        preference = await configure(device)
        report = preference.report()
        assert report["request_status"] == status.name
        assert report["request_accepted"] is False
        assert report["actual_interval_ms"] == 60
        assert report["released"] is True
        assert device.closed == device.request.closed == 1
    asyncio.run(run())


@pytest.mark.parametrize("platform,rate", [("linux", 25), ("darwin", 25), ("win32", 10), ("win32", None)])
def test_unsupported_platform_or_baseline_rate_does_not_open_native_handle(platform, rate):
    from laptop.ble_parameters import prefer_throughput
    async def run():
        async def unexpected(_address):
            raise AssertionError("native device must not be opened")
        preference = await prefer_throughput(SimpleNamespace(address="unused"), source_rate=rate,
                                            platform=platform, device_factory=unexpected)
        assert preference.report()["requested"] is False
        assert preference.report()["diagnostic"]
        preference.close()
    asyncio.run(run())


def test_missing_windows_api_closes_independent_handle_without_touching_client():
    async def run():
        device = Device(request_error=AttributeError("older Windows API"))
        preference = await configure(device)
        assert preference.report()["request_accepted"] is False
        assert "AttributeError" in preference.report()["diagnostic"]
        assert device.closed == 1
        assert device.request.closed == 0
    asyncio.run(run())


def test_cancellation_during_parameter_observation_closes_both_owned_handles():
    async def run():
        device = Device(intervals=(48,))
        task = asyncio.create_task(configure(device))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert device.closed == device.request.closed == 1
    asyncio.run(run())


def test_cleanup_attempts_device_even_if_request_close_fails_and_reports_error():
    async def run():
        device = Device(close_error=True)
        device.request.close_error = True
        preference = await configure(device)
        preference.close()
        preference.close()
        assert device.closed == device.request.closed == 1
        assert preference.report()["cleanup_errors"] == ["request:OSError", "device:OSError"]
    asyncio.run(run())


def test_later_refresh_reports_current_actual_parameters_while_preference_is_alive():
    async def run():
        device = Device(intervals=(48, 12, 24))
        preference = await configure(device)
        assert preference.report()["actual_interval_ms"] == 15
        preference.refresh()
        assert preference.report()["actual_interval_ms"] == 30
        preference.close()
    asyncio.run(run())


def test_device_open_finishing_after_cancellation_cannot_leave_preference_active():
    from laptop.ble_parameters import prefer_throughput
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        device = Device()
        async def delayed_open(_address):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return device
        task = asyncio.create_task(prefer_throughput(SimpleNamespace(address="unused"),
            source_rate=25, platform="win32", device_factory=delayed_open,
            preferred_parameters=PREFERENCE))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert device.closed == 1
        assert device.requested == []
    asyncio.run(run())


def test_actual_parameter_read_failure_is_reported_without_inventing_values():
    async def run():
        device = Device()
        def read_error(): raise OSError("disconnected parameter query")
        device.get_connection_parameters = read_error
        preference = await configure(device)
        report = preference.report()
        assert report["request_accepted"] is True
        assert report["actual_interval_ms"] is None
        assert report["actual_observation_error"] == "OSError"
        assert report["diagnostic"] == "actual_parameters_unavailable"
        preference.close()
        assert device.closed == device.request.closed == 1
    asyncio.run(run())


@pytest.mark.parametrize("timeout", [-1, 2.1, float("inf"), float("nan"), True])
def test_observation_budget_is_validated_before_any_native_action(timeout):
    async def run():
        device = Device()
        with pytest.raises(ValueError, match="observation"):
            await configure(device, observation_timeout=timeout)
        assert device.requested == []
    asyncio.run(run())
