# iOS communications source

This directory provides the native Communications Swift package and a separate preview/test host. It is a **source handoff**. The team's exported Unity Xcode project, generated IL2CPP files, app data, signing configuration and prebuilt iPhone app are outside this repository handoff. This checkout alone cannot rebuild or reinstall the deployed Unity visualizer.

| Path | Purpose |
| --- | --- |
| [`CommsNative/`](CommsNative/) | Swift package with `CommsCore` (framing, result validation, display state and the per-result queue), `CommsTransport` (phone-owned SSH/TLS subscriber) and `CommsBridge` (native UI/C bridge) |
| [`NativePreview/`](NativePreview/) | Optional iOS preview, UI tests and loopback transport tests against the same source modules |
| [`NATIVE-INTEGRATION.md`](NATIVE-INTEGRATION.md) | macOS source tests, preview steps, the C functions for Unity, and requirements for integrating the package into the team's Unity export |

The phone app opens its **own** SSH route to Ultra96 and subscribes through verified TLS to the board's result service. It does not receive results from the Windows laptop. The source accepts only protocol version 3 results: a label (`gesture`) of 1 to 32 characters from `A–Z a–z 0–9 _ -` and a `confidence` that is a finite number from 0 to 1. A result that fails these checks ends the session. When the connection is active, the screen shows `Subscribed`, the newest result's label, `result_id` and received confidence, and a `Received` count. The labels are whatever the board's model sends; the board's built-in dummy model sends `REST`, `FIST`, `OPEN` or `POINT` with confidence 1.0. The Unity app installed for Week 7 labels its setup button **Week 7 Connect** and accepts only older results with those four dummy labels and confidence 1, so it cannot read results from a version-3 service (a version-3 result ends its session); a build from this source labels the button **Communications Connect**. See [native integration](NATIVE-INTEGRATION.md#configure-and-observe-the-installed-app) for setup and phone observations; a laptop ingestion ACK alone does not prove phone receipt.

The Swift changes for version 3 (result validation, the `confidence` field, the display text and `CommsNextResult`) were written on 2026-10-14 without a Swift toolchain: **written, not compiled**. Run `swift test` on a Mac before integrating ([native integration](NATIVE-INTEGRATION.md#test-the-source-on-a-mac)).

The source package uses `Comms*` names and defaults to session `ltc-comms`. New integrations use `CommsStart`, `CommsCopyDisplay` and `CommsStop`, and take game input from `CommsNextResult`:

| C function | Contract |
| --- | --- |
| `void CommsStart(void)` | Installs the connect button and the foreground/background handling. Does not connect by itself. Call once |
| `int32_t CommsCopyDisplay(char* buffer, int32_t capacity)` | Writes the four-line status text (`Communications`, the status, `<gesture>  \|  <result_id>  \|  confidence <value>` or `No live result`, `Received: <N>`) with a terminating 0 and returns its byte count, only when the text changed and fits; otherwise returns 0 and writes nothing. Line 3 holds only the latest result, so it is a status display, not game input |
| `void CommsStop(void)` | Ends the session and clears the passwords. Call only when the app stops using Communications |
| `int32_t CommsNextResult(char* buffer, int32_t capacity)` | **Written, not compiled (2026-10-14).** Takes out the oldest waiting result as one compact JSON object, `{"result_id":"…","gesture":"…","confidence":…,"device_id":…}`, with a terminating 0, and returns its byte count. Returns 0 when nothing waits. If the buffer is null or smaller than the bytes plus the 0, it writes nothing, keeps the result and returns minus (bytes + 1). Never blocks; call it on the main thread each frame, in a loop while it returns a positive number. Results wait in arrival order after de-duplication by `result_id`; each can be taken out at most once, and not every result is guaranteed to arrive. Provisional limits: at most 32 waiting (when full, the oldest is dropped) and 2 s after arrival (then dropped); both drops are counted. The queue is emptied on a new Connect, Disconnect, the app becoming inactive and `CommsStop`, and kept across automatic reconnects |

Details and the C# declarations are in [native integration](NATIVE-INTEGRATION.md#c-functions-for-unity).

The app installed for Week 7 uses session `week7-demo` and its Unity export calls `Week7*` C bridge symbols; wrappers remain for `Week7Start`, `Week7CopyDisplay` and `Week7Stop` only (there is none for `CommsNextResult`). Firmware emits version-3 packets (marker `LC`, 38 bytes), and the laptop accepts only those. TLS identity `ultra96.week7.internal` remains pinned until certificate and app trust migration.
