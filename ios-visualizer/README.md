# iOS communications source

This directory provides the native Communications Swift package and a separate preview/test host. It is a **source handoff**. The team's exported Unity Xcode project, generated IL2CPP files, app data, signing configuration and prebuilt iPhone app are outside this repository handoff. This checkout alone cannot rebuild or reinstall the deployed Unity visualizer.

| Path | Purpose |
| --- | --- |
| [`CommsNative/`](CommsNative/) | Swift package with `CommsCore` (framing, schemas and display state), `CommsTransport` (phone-owned SSH/TLS subscriber) and `CommsBridge` (native UI/C bridge) |
| [`NativePreview/`](NativePreview/) | Optional iOS preview, UI tests and loopback transport tests against the same source modules |
| [`NATIVE-INTEGRATION.md`](NATIVE-INTEGRATION.md) | macOS source tests, preview steps, and requirements for integrating the package into the team's Unity export |

The deployed phone app opens its **own** SSH route to Ultra96 and subscribes through verified TLS to the board's result service. It does not receive results from the Windows laptop. The screen shows `Subscribed`, the newest simulated gesture/result identity, and a `Received` count when the connection is active. The already deployed Unity app labels its setup button **Week 7 Connect**; the source-only package labels a future integration **Communications Connect**. See [native integration](NATIVE-INTEGRATION.md#configure-and-observe-the-installed-app) for setup and phone observations; a laptop ingestion ACK alone does not prove phone receipt.

The source package uses `Comms*` names and defaults to session `ltc-comms`. New integrations use `CommsStart`, `CommsCopyDisplay` and `CommsStop`. The app installed for Week 7 uses session `week7-demo` and its Unity export calls `Week7*` C bridge symbols; wrapper symbols remain for that ABI, but the current server default is `ltc-comms`, so that app needs a server started with its session. Firmware emits packet magic `LC`, and the laptop accepts only `LC`. TLS identity `ultra96.week7.internal` remains pinned until certificate and app trust migration.
