"""Connection-scoped Windows 11 throughput preference and actual BLE parameters.

The public BluetoothLEDevice handle is opened independently of Bleak and pairing.
Retain the returned resource for the BLE connection lifetime, then close it.
SUCCESS means Windows accepted the preference; it does not establish negotiated
parameters, sustainable throughput, or the capacity of concurrent connections.

Microsoft API/unit references:
https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.bluetoothledevice.requestpreferredconnectionparameters
https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.bluetoothleconnectionparameters

ThroughputOptimized trades power and available concurrent links for throughput.
It is requested only above the existing 10 Hz baseline and restored by disposing
this helper's request and device handles. Other client-owned handles are untouched.
"""
from __future__ import annotations

import asyncio
import math
import sys
import time


_STATUSES = {0: "UNSPECIFIED", 1: "SUCCESS", 2: "DEVICE_NOT_AVAILABLE", 3: "ACCESS_DENIED"}


class ConnectionPreference:
    """Own the preference request and separate WinRT device; close is idempotent."""
    def __init__(self, source_rate):
        self._device = self._request = None
        self._baseline = None
        self._current = None
        self._report = {
            "source_rate_hz": source_rate,
            "preference": "throughput_optimized" if source_rate is not None and source_rate > 10 else "default",
            "requested": False,
            "request_status": "NOT_REQUESTED",
            "request_status_code": None,
            "request_accepted": False,
            "actual_interval_ms": None,
            "actual_latency_events": None,
            "actual_supervision_timeout_ms": None,
            "actual_observed_at_monotonic": None,
            "actual_observation_error": None,
            "actual_parameters_changed": False,
            "observation_elapsed_seconds": 0.0,
            "diagnostic": None,
            "cleanup_errors": [],
            "released": False,
        }

    def report(self):
        result = dict(self._report)
        result["cleanup_errors"] = list(result["cleanup_errors"])
        return result

    def _read_actual(self):
        if self._device is None:
            return
        try:
            parameters = self._device.get_connection_parameters()
            interval = int(parameters.connection_interval)
            latency = int(parameters.connection_latency)
            timeout = int(parameters.link_timeout)
            if not 6 <= interval <= 3200 or not 0 <= latency <= 499 or not 10 <= timeout <= 3200:
                raise ValueError("connection parameters outside Bluetooth ranges")
            self._current = (interval, latency, timeout)
            self._report.update(
                actual_interval_ms=interval * 1.25,
                actual_latency_events=latency,
                actual_supervision_timeout_ms=timeout * 10,
                actual_observed_at_monotonic=time.monotonic(),
                actual_observation_error=None,
                actual_parameters_changed=self._baseline is not None and self._current != self._baseline)
        except Exception as exc:
            self._report["actual_observation_error"] = type(exc).__name__

    def refresh(self):
        """Snapshot actual current parameters; this never assumes a target interval."""
        if self._report["released"]:
            return self.report()
        if self._request is not None:
            try:
                status = int(self._request.status)
                self._report.update(request_status_code=status,
                                    request_status=_STATUSES.get(status, f"UNKNOWN_{status}"),
                                    request_accepted=status == 1)
            except Exception as exc:
                self._report.update(request_status="UNAVAILABLE", request_accepted=False,
                                    diagnostic=f"request_status_unavailable:{type(exc).__name__}")
        self._read_actual()
        return self.report()

    def close(self):
        """Release both owned handles even if one close fails; retain error evidence."""
        if self._report["released"]:
            return
        self._report["released"] = True
        request, device = self._request, self._device
        self._request = self._device = None
        for name, resource in (("request", request), ("device", device)):
            if resource is not None:
                try:
                    resource.close()
                except Exception as exc:
                    self._report["cleanup_errors"].append(f"{name}:{type(exc).__name__}")


async def prefer_throughput(client, *, source_rate=None, observation_timeout=2.0,
                            device_factory=None, preferred_parameters=None, platform=None):
    """Request a scoped preference and observe parameters for at most two seconds.

Call after connecting, before source activation. The BLE connection owner should
supervise this await using its existing native-operation timeout; parameter
observation itself is capped at two seconds. Cancellation releases acquired
handles, including an open operation that returns after swallowing cancellation.
Dependencies are injectable for portable tests; WinRT imports stay inside here.
Unsupported OS/API and system denial are diagnostic no-ops, not capture proof.
"""
    if source_rate is not None and (type(source_rate) is not int or not 1 <= source_rate <= 200):
        raise ValueError("source rate must be integer 1..200 Hz")
    if (isinstance(observation_timeout, bool) or not isinstance(observation_timeout, (int, float))
            or not math.isfinite(observation_timeout) or not 0 <= observation_timeout <= 2):
        raise ValueError("parameter observation timeout must be finite in 0..2 seconds")
    result = ConnectionPreference(source_rate)
    platform = sys.platform if platform is None else platform
    if platform != "win32":
        result._report["diagnostic"] = "unsupported_platform"
        return result
    if source_rate is None or source_rate <= 10:
        result._report["diagnostic"] = "baseline_rate_no_preference_requested"
        return result
    try:
        if preferred_parameters is None:
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters
            preferred_parameters = BluetoothLEPreferredConnectionParameters.throughput_optimized
        if device_factory is None:
            from laptop.windows_pairing import _device
            device_factory = _device
        result._device = await device_factory(client.address)
        if asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        if result._device is None:
            raise RuntimeError("Windows BLE device unavailable")
        result._read_actual()
        result._baseline = result._current
        result._report["requested"] = True
        result._request = result._device.request_preferred_connection_parameters(preferred_parameters)
        if result._request is None:
            raise RuntimeError("Windows BLE preference request unavailable")
        started = asyncio.get_running_loop().time()
        deadline = started + observation_timeout
        while True:
            report = result.refresh()
            now = asyncio.get_running_loop().time()
            if report["request_status_code"] in (2, 3):
                result._report["diagnostic"] = "preference_request_denied_or_unavailable"
                result.close()
                break
            if report["actual_parameters_changed"]:
                result._report["diagnostic"] = "actual_parameter_change_observed"
                break
            if report["actual_observation_error"] is not None:
                result._report["diagnostic"] = "actual_parameters_unavailable"
                break
            if now >= deadline:
                result._report["diagnostic"] = (
                    "accepted_without_observed_parameter_change" if report["request_accepted"]
                    else "preference_status_unresolved")
                break
            await asyncio.sleep(min(0.05, deadline - now))
        result._report["observation_elapsed_seconds"] = asyncio.get_running_loop().time() - started
        return result
    except asyncio.CancelledError:
        result.close()
        raise
    except Exception as exc:
        result._report.update(request_accepted=False,
                              diagnostic=f"preference_unavailable:{type(exc).__name__}")
        result.close()
        return result
