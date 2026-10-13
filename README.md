# Let-Them-Cook

A cooking AR game inspired by Overcooked.

This repository handoff contains the Communications subsystem: protected dummy-data streams from two FireBeetle32 boards through a Windows relay and Ultra96 to the installed Unity iPhone visualizer.

## Communications

- [flash.py](flash.py): build, upload and pair the LEFT and RIGHT boards; run `python flash.py --help` for options.
- [demo.py](demo.py): open the SSH tunnel, capture both streams and review reports; run `python demo.py --help` or `python demo.py run --help` for options.
- [iOS source and integration boundary](ios-visualizer/README.md) · [native build and preview](ios-visualizer/NATIVE-INTEGRATION.md).

New source defaults use `LC` sensor packets, `LCS1` source statistics and the `ltc-comms` session. The original deployment still uses `W7`, `W7S1` and `week7-demo`; the laptop decoders accept either packet marker. When using the original server and installed phone, pass `--session-id week7-demo` to `demo.py run/live` or set `$env:LTC_COMMS_SESSION = 'week7-demo'` in PowerShell. Laptop, server and phone must use the same session. The TLS name `ultra96.week7.internal` and the deployed iPhone bridge's `Week7*` symbols remain compatibility identifiers until certificates and the Unity export are replaced. The Unity Xcode export and a prebuilt iPhone app are not part of this repository handoff.
