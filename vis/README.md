# vis: OvercookAR visualizer

The phone side of OvercookAR, our CG4002 AR co-op cooking game. It is an iOS app built in Unity that shows the kitchen in AR through a phone headset, tracks the player's hands with the camera, and reacts to gestures (grab, chop, drop, and so on).

## What it uses

- **Unity 6000.5.10f1** (Unity 6.5). Open it with this exact version so Unity doesn't upgrade the project.
- **AR Foundation + ARKit XR Plugin** for image tracking. Each kitchen station sits on its own printed QR/image marker.
- **MediaPipe Unity Plugin** (homuler, `HandLandmarker` Task API) for on-device hand tracking.
- **EzySlice** (pure C#) for slicing ingredients when chopping.
- **Xcode** to build and install on an iPhone. ARKit only runs on a real device, not in the Editor's Play mode.

## Folder layout

Only these three Unity folders are in git:

- `Assets/` holds scripts, scenes, models, materials and prefabs. Our scripts are in `Assets/Scripts/` (AR, Core, Kitchen, Network, UI).
- `Packages/` holds `manifest.json`, which lists the packages Unity downloads.
- `ProjectSettings/` holds player, XR and build settings.

`Library/`, `Temp/`, `Logs/`, `UserSettings/` and the Xcode build folder are ignored (see `.gitignore`). Unity recreates them.

## Opening the project

1. Install Unity **6000.5.10f1** through Unity Hub, with the **iOS Build Support** module.
2. Clone the repo somewhere outside iCloud Drive (iCloud offloading breaks Unity imports).
3. In Unity Hub, choose **Add > Add project from disk** and select the `vis` folder.
4. The first open takes a while because Unity rebuilds `Library/`.
5. Open the main scene in `Assets/Scenes/`.

## Building to an iPhone

1. **File > Build Profiles**, pick **iOS**, then **Build**. Pick an output folder such as `vis/Builds/` (it is ignored by git).
2. Open the generated `.xcodeproj` in Xcode, set your own signing team, plug in the phone and press Run.
3. If you get odd duplicate-assembly or "already registered" errors, quit Xcode, delete `Library/`, the build folder and `~/Library/Developer/Xcode/DerivedData`, then build again.

## Talking to the rest of the system

The phone runs a TLS server on port 5005 and receives newline-separated lines (gestures and game events) from the comms side. See `Assets/Scripts/Network/TlsDataReceiver.cs`.
