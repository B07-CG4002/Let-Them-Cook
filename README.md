# Let-Them-Cook

A cooking AR game inspired by Overcooked.

This repository handoff contains the Communications subsystem: protected dummy-data streams from two FireBeetle32 boards through a Windows relay to the Ultra96 service, which passes each new sample to a model (a built-in dummy model by default) and sends the results to an iPhone app that links this source's `CommsNative` package.

## Communications

- [flash.py](flash.py): build, upload and pair the LEFT and RIGHT boards; run `python flash.py --help` for options.
- [demo.py](demo.py): run a whole capture in one command (`python demo.py start`: the SSH tunnel, both boards, a live view and one `run.log`; `q` stops it), or open the SSH tunnel, capture both streams and review reports step by step; run `python demo.py --help`, `python demo.py start --help` or `python demo.py run --help` for options.
- [tools/check_board.py](tools/check_board.py): check one flashed board against a service on this laptop, with no SSH, Ultra96 or phone; run `python -m tools.check_board left` or `right`.
- [tools/deploy_service.py](tools/deploy_service.py): deploy this source's board service, and optionally a renewed server certificate, to the project's Ultra96 in one step, with rollback; run `python -m tools.deploy_service --dry-run` first. It needs `paramiko` from `requirements-dev.txt`.
- [iOS source and integration boundary](ios-visualizer/README.md) · [native build and preview](ios-visualizer/NATIVE-INTEGRATION.md).

Sensor packets use the `LC` marker, source statistics use `LCS3`, and the session is `ltc-comms`. Both boards, the Ultra96 service and the phone app must run builds of this source. A board on older firmware (version 2 with `LCS1` statistics, or Week 7 with `W7` packets and `W7S1` statistics) is refused by the laptop until it is flashed again, and the app installed for Week 7 subscribes to `week7-demo`, which a server started from this source does not serve. Laptop, server and phone must use the same session: `demo.py start/run/live`, `demo.py service --start`, `tools.deploy_service` and the phone source all default to `ltc-comms`; `--session-id` changes it for the laptop commands, and `$env:LTC_COMMS_SESSION` for the `demo.py` commands only (`tools.deploy_service` does not read it). Captures recorded with Week 7 firmware hold `W7` packets, so `demo.py report` no longer passes them; review those from commit `880abba`. Captures recorded with version-2 firmware are refused too; review those from commit `5a004c6`. The TLS name `ultra96.week7.internal` stays because it is written into the server certificate, and the `Week7*` bridge symbols stay for the Week 7 Unity export. The Unity Xcode export and a prebuilt iPhone app are not part of this repository handoff.
