# Native iOS Communications integration

`CommsNative` is the checked-in Swift package for the iPhone result client. It provides `CommsCore`, `CommsTransport` and `CommsBridge`; `NativePreview` uses the same modules for source-level checks. The exported Unity Xcode project and its generated app inputs are **not included here**. To build the deployed Unity visualizer, obtain the team's matching export and integrate this package in that project; these commands alone do not produce the Unity app.

## Test the source on a Mac

Use a Mac with Xcode, iOS support and SwiftPM network access. From the repository root:

```sh
swift test --package-path ios-visualizer/CommsNative
```

For the optional simulator preview, install XcodeGen and generate disposable test certificates before running its transport suite:

```sh
python3 ios-visualizer/NativePreview/tools/generate_test_pki.py
xcodegen generate --spec ios-visualizer/NativePreview/project.yml
xcodebuild -project ios-visualizer/NativePreview/CommsNativePreview.xcodeproj \
  -scheme CommsNativeTransport -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -jobs 4 -parallel-testing-enabled NO test
xcodebuild -project ios-visualizer/NativePreview/CommsNativePreview.xcodeproj \
  -scheme CommsNativePreview -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
  -jobs 4 test
```

Choose a simulator installed with that Mac's Xcode if the example device name differs. The generated PKI is isolated test material under ignored `.comms-local/CommsFixturePKI`; it is not the production CA. The preview tests exercise the native source against local peers. They do not establish a Unity build, physical BLE/Ultra96/iPhone delivery, signing, or ARKit behavior. This workflow requires Xcode and XcodeGen and has not been verified on Windows.

**Status of the protocol version 3 changes (2026-10-14): written, not compiled.** Result validation (version 3, the label and confidence rules), `GestureResult.confidence`, the display text, the per-result queue, `CommsNextResult` and their tests were written without a Swift toolchain. Run `swift test` first and fix any build error before integrating. Only two parts were checked, on Windows: the Python half of `PythonBoardTests` ran against the real Python board service, and a Python copy of the package's JSON parser ran the `ProtocolTests` vectors.

## Integrate with the team's Unity export

In the separate exported Unity Xcode project, link the `CommsNative` package product into the Unity framework and build/sign with the team's own Apple development settings. For the game's own export (the current plan), the game's C# calls the C functions below directly; no generated code is patched. Only the old Week 7 export relied on patched native bridge calls in its generated receiver. The exported project, generated receiver hook, Unity assets, and signing configuration must be supplied separately. The source handoff cannot verify that an arbitrary export contains those hooks or produce a deployable app by itself.

New integrations should call `CommsStart`, `CommsCopyDisplay` and `CommsStop`, and take game input from `CommsNextResult` (next section). The deployed export calls legacy `Week7*` C bridge symbols; this package keeps wrappers for that ABI so the existing export can link without a generated-code change. The wrappers cover `Week7Start`, `Week7CopyDisplay` and `Week7Stop` only; there is no `Week7*` form of `CommsNextResult`. Preserve the wrappers while that export is in service. Do not substitute the optional preview app for Unity device acceptance.

## C functions for Unity

The C functions are exported by `Sources/CommsBridge/DisplayMailbox.swift`. They exist only in the iOS build, so the C# declarations (derived from the C signatures, not yet compiled in Unity) go inside a platform guard:

```csharp
#if UNITY_IOS && !UNITY_EDITOR
[DllImport("__Internal")] static extern void CommsStart();
[DllImport("__Internal")] static extern int  CommsCopyDisplay(byte[] buffer, int capacity);
[DllImport("__Internal")] static extern void CommsStop();
[DllImport("__Internal")] static extern int  CommsNextResult(byte[] buffer, int capacity);
#endif
```

- `CommsStart`: call once at start-up. It places the **Communications Connect** button in the top-right corner and does not connect by itself; a person taps it, enters the jump-host and board credentials and imports the CA.
- `CommsCopyDisplay`: call once per frame on the main thread with a 2048-byte buffer. When the text changed and fits, it writes the UTF-8 text and a terminating 0 and returns the byte count (without the 0); otherwise, or when the buffer is null or too small, it returns 0 and writes nothing, and the caller keeps the text it has. It never returns a negative number. The four lines are:

  ```text
  Communications
  <status>
  <gesture>  |  <result_id>  |  confidence <received confidence>     (No live result when none is current)
  Received: <N>
  ```

  The label is the received text; the confidence is the received number in Swift's text form of a `Double` (a received `1` shows as `1.0`). The version-2 source showed a fixed `DUMMY inference` header and `confidence 1.0`. Line 3 holds only the latest result and the next result overwrites it, so results that arrive between two polls can be missed; it is a status display, not game input. `Subscribed` means only that the subscription handshake succeeded.
- `CommsStop`: call only when the app stops using Communications. It ends the session, clears the passwords and removes the button. Do not call it on a scene change or when going to the background.
- `CommsNextResult` (**written, not compiled, 2026-10-14**): the game input. It takes out the oldest waiting result as one compact UTF-8 JSON object with a terminating 0, keys in this order, for example `{"result_id":"1:305419896:42","gesture":"CHOP","confidence":0.93,"device_id":1}`, and returns the number of bytes written (without the 0). It returns 0 when nothing waits. When the buffer is null or smaller than the bytes plus the 0, it writes nothing, leaves the result queued and returns minus (bytes + 1), the capacity needed. It never blocks. Call it on the main thread each frame, in a loop while it returns a positive number; a 512-byte buffer holds any valid result, because labels are at most 32 characters.

What `CommsNextResult` promises: results wait in arrival order, de-duplicated by `result_id`, and each result that is not dropped can be taken out at most once. It does not promise that every result arrives: results lost before the phone (board drops, reconnects, no subscriber) never reach it. Provisional limits, revisited once the model's result rate is known: at most 32 results wait, and when the queue is full the oldest is dropped; a result still waiting 2 s after it reached the phone is dropped, also while only `CommsCopyDisplay` is polled. The display snapshot counts both drops (`pendingDropped`, `pendingExpired`, with `pendingCount` for the results waiting); a new Connect resets them. The queue is emptied, without counting, when the session stops (Disconnect, the app becoming inactive, `CommsStop`) and when a new Connect starts. An automatic reconnect keeps it, and so does a session ended by a verification error; its entries still expire after 2 s. A `result_id` starting with `cmd:` comes only from command tests on the laptop.

Rules for the caller: one object polls `CommsCopyDisplay` (each call reports only the change since the previous one), the same object calls `CommsNextResult`, and that object survives scene changes. Use `result_id`, not the label text, to tell results apart. Which game action a label maps to, cooldowns and repeats are the game's decision.

## Configure and observe the installed app

The installed Unity app needs the verified **public** Communications CA, the institutional VPN route if required, and authorized SSH credentials for the campus jump host and Ultra96. No CA/server private key belongs in the app. In its setup view, import the trusted CA, enter the host credentials, and tap **Communications Connect** in an app built from this source (the app installed for Week 7 shows **Week 7 Connect**). Wait for **Subscribed** before starting the Windows capture. Keep the app foregrounded; after locking or backgrounding, reconnect manually and enter credentials again. Its `Received` count is a phone observation and should be compared with the same run's Ultra96 accepted-result evidence (the laptop's `Phone expected increase`, which holds only while the board runs its built-in dummy model).

The phone owns its SSH connection to Ultra96 TCP 22 and a nested stream to board loopback `127.0.0.1:9999`. TLS inside that route verifies the CA and `ultra96.week7.internal`. New source sends `SUBSCRIBE` (version 1) for `ltc-comms`, requires `SUBSCRIBED`, and accepts only version-3 `GESTURE_RESULT` frames with exactly 10 keys, a label of 1 to 32 characters from `A–Z a–z 0–9 _ -` and a confidence that is a finite number from 0 to 1; a result that fails validation ends the session and clears the passwords. The app installed for Week 7 uses `week7-demo` and accepts only older results with the four dummy labels and confidence 1, so it cannot read results from a version-3 service (a version-3 result ends its session); rebuild it from this source, which uses `ltc-comms` like the server. Firmware emits version-3 BLE packets (marker `LC`, 38 bytes), and the laptop accepts only those, so both boards must run current firmware. Exported `Week7*` bridge wrappers and the pinned TLS service name remain until their dependent Unity export and certificate trust are migrated.

New app builds write public settings and the verified public CA under `Application Support/Comms` (`comms-ca.pem`). They can read the corresponding files from the old `Application Support/Week7` directory when no new file exists; saving settings writes only to the new directory. Passwords remain in memory only.

See the [repository README](../README.md#communications) for launcher entry points and the session default. The [sensor codec](../common/sensor.py), [BLE controls](../common/control.py) and [network protocol](../ultra96/protocol.py) define the packet and result formats.
