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

## Integrate with the team's Unity export

In the separate exported Unity Xcode project, link the `CommsNative` package product into the Unity framework, retain the reviewed native C bridge calls from the generated receiver, and build/sign with the team's own Apple development settings. The exported project, generated receiver hook, Unity assets, and signing configuration must be supplied separately. The source handoff cannot verify that an arbitrary export contains those hooks or produce a deployable app by itself.

New integrations should call `CommsStart`, `CommsCopyDisplay` and `CommsStop`. The deployed export calls legacy `Week7*` C bridge symbols; this package keeps wrappers for that ABI so the existing export can link without a generated-code change. Preserve the wrappers while that export is in service. Do not substitute the optional preview app for Unity device acceptance.

## Configure and observe the installed app

The installed Unity app needs the verified **public** Communications CA, the institutional VPN route if required, and authorized SSH credentials for the campus jump host and Ultra96. No CA/server private key belongs in the app. In its setup view, import the trusted CA, enter the host credentials, and tap **Week 7 Connect** in the already deployed app. The source-only `CommsNative` UI uses **Communications Connect** for a future build. Wait for **Subscribed** before starting the Windows capture. Keep the app foregrounded; after locking or backgrounding, reconnect manually and enter credentials again. Its `Received` count is a phone observation and should be compared with the same run's Ultra96 accepted-result evidence.

The phone owns its SSH connection to Ultra96 TCP 22 and a nested stream to board loopback `127.0.0.1:9999`. TLS inside that route verifies the CA and `ultra96.week7.internal`. New source sends `SUBSCRIBE` for `ltc-comms`, requires `SUBSCRIBED`, and validates framed results. The installed app and original Ultra96 service use `week7-demo`, so they continue to work together; deploying a rebuilt app with this source requires the server session to be updated at the same time. The original BLE packet magic `W7` is accepted by the updated laptop while new firmware emits `LC`. Exported `Week7*` bridge wrappers and the pinned TLS service name remain until their dependent Unity export and certificate trust are migrated.

New app builds write public settings and the verified public CA under `Application Support/Comms` (`comms-ca.pem`). They can read the corresponding files from the old `Application Support/Week7` directory when no new file exists; saving settings writes only to the new directory. Passwords remain in memory only.

See the [repository README](../README.md#communications) for launcher entry points and the original deployment's session setting. The [sensor codec](../common/sensor.py), [BLE controls](../common/control.py) and [network protocol](../ultra96/protocol.py) define the packet and result formats.
