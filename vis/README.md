# Let Them Cook: Visualizer (Unity AR app)

The phone side of **Let Them Cook**, an AR cooking game made for NUS CG4002 (Computer Engineering Capstone). The player wears a phone in a headset and two gesture gloves. The phone shows a virtual kitchen pinned to QR codes on a real table. The gloves say **what** each hand is doing (chop, pour, grab...), and the phone's camera and LiDAR say **where** each hand is. The game fuses the two.

This folder is the Unity project: `Assets/`, `Packages/` and `ProjectSettings/`.

---

## How it fits together

```
Gloves (IMU, 50 Hz each) ──► Ultra96 (FPGA gesture classifier) ──JSON over NUS VPN──► Phone
                                                                                        │
  CommsNative (Swift, in the Xcode build) ─► CommsBridge ─► GestureButtonController ◄───┤
                                                   (game logic for each hand)          │
  AR camera ─► HandTrackingManager + ColorMarkerTracker  (where is each hand on screen) │
  LiDAR     ─► HandDepthEstimator                         (how far away is each hand)  │
  QR codes  ─► QrCodeItemSpawner ─► kitchen stations (board, pot, stove, sink, plates, serve window, bin)
```

- **Gestures:** each glove streams IMU data to the Ultra96, which classifies it and sends `{gesture, confidence, device_id}` to the phone. `CommsBridge` maps the label to a game action (Grab, Drop, Chop, Pour, Wash, Throw) and the `device_id` to a hand (1 = left, 2 = right).
- **Hand position:** each glove has a coloured patch (green = right, magenta = left). `ColorMarkerTracker` finds the patches in the camera image every frame, so left and right never swap.
- **Hand depth:** LiDAR depth at each hand lets held items follow the hand in 3D and lets the real hand nudge virtual ingredients.
- **Two hands in parallel:** each hand holds and acts on its own item (`GestureButtonController` keeps one slot per hand).
- **Late gestures:** a gesture arrives a moment after it's made. `HandHistory` keeps the last 0.5 s of each hand, so a Grab/Drop/Chop/Pour that finds nothing *now* uses where the hand was when the gesture was made.

## Gameplay

Take ingredients from crates, chop them on a board (real mesh slicing), put them in the pot, add water at the sink, cook on the stove, pour onto a plate and serve it at the window to fill an order. Dirty plates come back and are washed with both hands. Burnt food costs points. Ingredients can be thrown into the pot, onto a board or into the bin.

Modes: **Play** (timed round with countdown, score, stars and best score), **Unlimited** (no clock, nothing burns), **Tutorial** (How to play card). Modes can be switched in game without rescanning the QR codes. Menu buttons are pressed by holding a hand over them.

## Requirements

- Unity 6 (6000.5) with iOS build support. The project uses URP, AR Foundation + ARKit (image tracking, occlusion), TextMeshPro and [EzySlice](https://github.com/DavidArayan/ezy-slice).
- An iPhone with LiDAR (tested on iPhone 14 Pro), iOS 15 or later.
- Xcode, to build and sign the iOS app.
- For glove input: the `CommsNative` Swift package (`ios-visualizer/CommsNative` in this repo) and the NUS VPN.

## Building

1. Open this folder in Unity and switch the platform to iOS.
2. **Player Settings > iOS > Scripting Define Symbols:** add `COMMS_NATIVE;COMMS_NEXT_RESULT` for glove input. Add `COMMS_HAND_EVENTS` as well to read hand events instead of plain results. With no defines, the app builds and runs without comms (use the on-screen test buttons).
3. Build to an Xcode project. In Xcode, add the `CommsNative` package to the **UnityFramework** target, set the minimum iOS version to 15.0, then build to the phone. Quit any other Xcode project that has CommsNative open first.
4. Print the QR codes listed in the AR Reference Image Library and place them on a table.

## Running a round

1. Connect the phone to the NUS VPN and start the Ultra96 service and both gloves.
2. Open the app and tap **Communications Connect** (top right). The connection screen comes from CommsNative, not Unity:
   1. Import the CA certificate file for the Ultra96 server (the comms side provides it).
   2. Enter your jump-host login and the Ultra96 board login.
   3. Wait for the status to show **Subscribed**. The Received count goes up as gesture results arrive.

   No logins are stored in this repo. Ask the comms side for the CA file and board account.
3. Choose a mode, then look at each QR code until its station appears. The round starts once all stations are anchored (the warning pill shows scan progress).
4. Without gloves, the hidden on-screen test buttons call the same actions for either hand.

## Project layout (`Assets/Scripts/`)

| Folder | What's in it |
|---|---|
| `AR/` | `QrCodeItemSpawner` (spawns and anchors stations on QR codes, overlap check), `HeadsetEyeLayout` (two eye views for the headset), `BarrelDistortionFeature` (lens distortion render pass), `FaceCameraWhileHeld` |
| `Network/` | `CommsBridge` (the only script that talks to CommsNative; survives scene loads) |
| `Core/` | `GestureButtonController` (game actions per hand), `HandTrackingManager`, `ColorMarkerTracker`, `HandDepthEstimator`, `HandInteractable`, `HandCollider` + `HandPushable` (hand nudges), `HandHistory` (late gestures), `Hand`, `Grabbable`, `ThrowFlight`, `FallToSurface`, `ScreenBounds`, `ScoreManager`, `RoundResults`, `FrameRateCap` |
| `Kitchen/` | Stations and rules: `Pot`, `PotPour`, `PotStation`, `ChoppingBoard`, `ChoppedIngredient`, `ChopProgressBar`, `IngredientCrate`, `IngredientState`, `LayFlatOnBoard`, `Sink`, `Plate`, `PlatePile`, `DishRack`, `DirtyPlateStack`, `ServeStation`, `Bin`, `OrderManager`, `RoundTimer`, `RoundEnd` |
| `UI/` | HUD (orders, score, timer, warning and gesture pills, banners, throw crosshair), hover-to-select (`DwellSelect`, `SelectableMenuItem`), `SceneLoader`, `EndScreen`, `PauseWhileActive`, hover feedback |
| `Editor/` | `HudPrefabBuilder`, `EndScreenBuilder`: Editor-only tools that built the HUD and end screen prefabs (Tools > OvercookAR) |

Scenes: **Menu** (CommsBridge lives here and is kept across scenes), the **game** scene, and **End** (results).

## Notes and known limitations

- Hand tracking in the final build uses the colour gloves only. The MediaPipe hand-landmark code in `HandTrackingManager` is still present but switched off (*Run MediaPipe* unticked). Other scripts still read MediaPipe's 21-point hand shape, which the colour tracker fills in.
- Most settings are set in the Inspector, not in code.
- Going from the Menu to the game, or playing again from the End screen, resets ARKit, so the QR codes have to be scanned again. Switching modes inside the game doesn't.
- Colour tracking needs the patches visible and reasonable light. Other strongly green or magenta objects in view can confuse it.
- Glove input works only in iOS builds with CommsNative. In the Editor and other platforms, use the test buttons.
- One camera image is shown to both eyes (mono, not stereo), cropped to fit the headset lenses.
- Falls, throws and hand pushes are scripted, not Unity physics.
