# Let-Them-Cook

A cooking AR game inspired by Overcooked.

This repository handoff contains the Communications subsystem: protected dummy-data streams from two FireBeetle32 boards through a Windows relay and Ultra96 to the installed Unity iPhone visualizer.

## Communications

- [flash.py](flash.py): build, upload and pair the LEFT and RIGHT boards; run `python flash.py --help` for options.
- [demo.py](demo.py): open the SSH tunnel, capture both streams and review reports; run `python demo.py --help` or `python demo.py run --help` for options.
- [iOS source and integration boundary](ios-visualizer/README.md) · [native build and preview](ios-visualizer/NATIVE-INTEGRATION.md).

Sensor packets use the `LC` marker, source statistics use `LCS1`, and the session is `ltc-comms`. Both boards, the Ultra96 service and the phone app must run builds of this source: the Week 7 builds (`W7` packets, `W7S1` statistics, session `week7-demo`) are no longer accepted. Laptop, server and phone must use the same session; `demo.py run/live`, `demo.py service --start` and the phone source all default to `ltc-comms`, and `--session-id` or `$env:LTC_COMMS_SESSION` changes it on the laptop side. The TLS name `ultra96.week7.internal` stays because it is written into the server certificate, and the `Week7*` bridge symbols stay for the Week 7 Unity export. The Unity Xcode export and a prebuilt iPhone app are not part of this repository handoff.
