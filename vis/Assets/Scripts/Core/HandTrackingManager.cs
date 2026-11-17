using System.IO;
using System.Collections.Generic;
using Unity.Collections;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;
using Mediapipe;
using Mediapipe.Tasks.Core;
using Mediapipe.Tasks.Vision.HandLandmarker;
using Mediapipe.Tasks.Vision.Core;

public class HandTrackingManager : MonoBehaviour
{
    [SerializeField] private ARCameraManager arCameraManager;

    [Header("Camera Image Correction")]
    [SerializeField] private bool mirrorImage = false;

    [Tooltip("Passed to MediaPipe's ImageProcessingOptions - it rotates internally instead of a manual CPU loop.")]
    [SerializeField] private int rotationDegrees = 0; // set in the Inspector: 270 on our headset

    [Tooltip("ARKit crops the 4:3 camera image to fill the wider screen, so the top and bottom of the image are never shown. " +
        "Ticked, landmarks are mapped through that crop so they sit on the hand. Untick to compare with the old mapping.")]
    [SerializeField] private bool correctCameraCrop = true;

    [Tooltip("Landmarks that count for touching an item (grab, drop into pot, menu hover). An item is touched when ANY of them " +
        "is within its radius. Default: wrist + the base of each finger (the palm). Add 8 to also count the index fingertip.")]
    [SerializeField] private int[] touchLandmarks = { 0, 1, 5, 9, 13, 17 };
    [Tooltip("Debug: draw a green dot where each item's reach point is (the spot the hand must be near). Untick to hide.")]
    [SerializeField] private bool showReachPoints = true;

    [Header("Two hands")]
    [Tooltip("Ticked: left and right hand are tracked separately, so each can hold and act on its own. " +
        "Unticked: only the first hand MediaPipe finds is used (counted as Right), like before. The demo fallback.")]
    [SerializeField] private bool twoHands = true;
    [Tooltip("MediaPipe names hands as if the image were a mirrored selfie. The back camera isn't mirrored, so its " +
        "Left/Right usually come out swapped. Check with one hand: your right hand's dots should be red, the left's blue.")]
    [SerializeField] private bool swapHandedness = true;
    [Tooltip("Ticked: a hand keeps its Left/Right label for as long as it stays tracked, found by where it moved to. " +
        "MediaPipe's own label is only used when a hand first appears, since it guesses badly for a fist.")]
    [SerializeField] private bool stickyHands = true;
    [Tooltip("Furthest a hand's centre may move between two MediaPipe results and still be the same hand " +
        "(fraction of the camera image, 0-1).")]
    [SerializeField] private float maxHandJump = 0.2f;
    [Tooltip("A hand missing for longer than this (seconds) is forgotten; when it comes back it's labelled afresh.")]
    [SerializeField] private float forgetHandAfter = 0.5f;

    [Header("Colour backup (gloves)")]
    [Tooltip("Finds each glove's coloured patch. Used for any hand MediaPipe doesn't see this frame. Empty = found at start; none = no backup.")]
    [SerializeField] private ColorMarkerTracker colorTracker;
    [Tooltip("Untick to switch MediaPipe off completely (much less heat) and track the gloves by colour only.")]
    [SerializeField] private bool runMediaPipe = true;

    private HandLandmarker handLandmarker;
    private Texture2D cameraTexture;
    private List<HandInteractable> interactables = new List<HandInteractable>();

    // One hand's latest MediaPipe result, normalized to the camera image (x right, y down, 0-1).
    private class HandData
    {
        public readonly List<Vector2> landmarks = new List<Vector2>(21);
        public readonly List<Vector2> touchPoints = new List<Vector2>(); // this hand's touchLandmarks
        public Vector2 fingertip;
        public Vector2 center;
    }

    private HandData[] latestHands = new HandData[2]; // indexed by Hand; null = that hand isn't seen
    private HandData[] colorHands = new HandData[2];  // same, from the colour patches; main thread only
    private readonly List<Vector2> colorTips = new List<Vector2>();
    // Where each hand was last seen (MediaPipe callback thread only), for Sticky Hands.
    private readonly Vector2[] lastCenter = new Vector2[2];
    private readonly long[] lastSeenMs = { long.MinValue, long.MinValue };
    private int lastDetectedHandCount = 0;
    private float imageAspect = 0f; // camera image width / height, 0 until the first frame
    private readonly object landmarkLock = new object();

    // Wrist + the four knuckles at the base of the fingers. These barely move when the hand closes into a fist,
    // unlike the index fingertip (8), which curls into the palm.
    private static readonly int[] HandCenterLandmarks = { 0, 5, 9, 13, 17 };
    private static readonly int[] FingertipLandmarks = { 8, 4, 12, 16, 20 }; // index first: it is "the" fingertip

    void Start()
    {
        if (colorTracker == null)
            colorTracker = FindAnyObjectByType<ColorMarkerTracker>();
        if (runMediaPipe)
            InitializeHandLandmarker();
    }

    void OnEnable()
    {
        if (arCameraManager != null)
            arCameraManager.frameReceived += OnCameraFrameReceived;
    }

    void OnDisable()
    {
        if (arCameraManager != null)
            arCameraManager.frameReceived -= OnCameraFrameReceived;
    }

    private void InitializeHandLandmarker()
    {
        string modelPath = Path.Combine(Application.streamingAssetsPath, "hand_landmarker.task");
        var baseOptions = new BaseOptions(modelAssetPath: modelPath);
        var options = new HandLandmarkerOptions(
            baseOptions: baseOptions,
            runningMode: RunningMode.LIVE_STREAM,
            numHands: 2,
            resultCallback: OnHandLandmarkerResult
        );
        handLandmarker = HandLandmarker.CreateFromOptions(options);
        Debug.Log("HandLandmarker initialized successfully.");
    }

    private void OnCameraFrameReceived(ARCameraFrameEventArgs eventArgs)
    {
        if (!arCameraManager.TryAcquireLatestCpuImage(out XRCpuImage cpuImage))
            return;

        var conversionParams = new XRCpuImage.ConversionParams
        {
            inputRect = new RectInt(0, 0, cpuImage.width, cpuImage.height),
            outputDimensions = new Vector2Int(cpuImage.width, cpuImage.height),
            outputFormat = TextureFormat.RGBA32,
            transformation = mirrorImage ? XRCpuImage.Transformation.MirrorY : XRCpuImage.Transformation.None
        };

        int size = cpuImage.GetConvertedDataSize(conversionParams);
        var buffer = new NativeArray<byte>(size, Allocator.Temp);
        cpuImage.Convert(conversionParams, buffer);
        cpuImage.Dispose();

        int width = conversionParams.outputDimensions.x;
        int height = conversionParams.outputDimensions.y;
        imageAspect = (float)width / height;

        if (cameraTexture == null || cameraTexture.width != width || cameraTexture.height != height)
            cameraTexture = new Texture2D(width, height, conversionParams.outputFormat, false);

        if (colorTracker != null)
        {
            colorTracker.ProcessFrame(buffer, width, height);
            UpdateColorHands();
        }
        if (!runMediaPipe)
        {
            buffer.Dispose();
            lock (landmarkLock)
            {
                latestHands = new HandData[2];
                lastDetectedHandCount = 0;
            }
            return;
        }
        if (handLandmarker == null)
            InitializeHandLandmarker(); // ticked while running

        cameraTexture.LoadRawTextureData(buffer);
        cameraTexture.Apply();
        buffer.Dispose();

        var image = new Image(ImageFormat.Types.Format.Srgba, cameraTexture);
        long timestamp = System.DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

        var imageProcessingOptions = new ImageProcessingOptions(rotationDegrees: rotationDegrees); // MediaPipe rotates the image itself, no CPU loop
        handLandmarker.DetectAsync(image, timestamp, imageProcessingOptions);
    }

    private void OnHandLandmarkerResult(HandLandmarkerResult result, Image image, long timestamp)
    {
        int handCount = result.handLandmarks?.Count ?? 0;
        var found = new List<HandData>();
        var labels = new List<Hand>(); // MediaPipe's guess for each found hand

        for (int i = 0; i < handCount; i++)
        {
            var hand = result.handLandmarks[i];
            if (hand.landmarks == null || hand.landmarks.Count < 21) continue;

            var data = new HandData();
            foreach (var landmark in hand.landmarks)
                data.landmarks.Add(new Vector2(landmark.x, landmark.y));
            data.fingertip = data.landmarks[8];

            Vector2 sum = Vector2.zero;
            foreach (int c in HandCenterLandmarks)
                sum += data.landmarks[c];
            data.center = sum / HandCenterLandmarks.Length;

            foreach (int t in touchLandmarks)
                if (t >= 0 && t < data.landmarks.Count)
                    data.touchPoints.Add(data.landmarks[t]);

            bool left = HandednessName(result, i) == "Left";
            if (swapHandedness) left = !left;
            found.Add(data);
            labels.Add(left ? Hand.Left : Hand.Right);
        }

        var hands = new HandData[2];
        if (!twoHands)
        {
            if (found.Count > 0) hands[(int)Hand.Right] = found[0]; // one hand only: the first one found counts
        }
        else
        {
            AssignHands(found, labels, timestamp, hands);
        }

        lock (landmarkLock)
        {
            latestHands = hands;
            lastDetectedHandCount = handCount;
        }
    }

    // Puts each found hand in the Left or Right slot of `hands`.
    private void AssignHands(List<HandData> found, List<Hand> labels, long nowMs, HandData[] hands)
    {
        var used = new bool[found.Count];

        // Sticky Hands: a hand seen recently takes the nearest found hand within Max Hand Jump, closest pair first,
        // so a fist (which MediaPipe can't tell left from right) keeps the label it had while open.
        if (stickyHands)
        {
            long forgetMs = (long)(forgetHandAfter * 1000f);
            while (true)
            {
                int bestSlot = -1, bestFound = -1;
                float bestDist = maxHandJump;
                for (int slot = 0; slot < 2; slot++)
                {
                    if (hands[slot] != null || nowMs - lastSeenMs[slot] > forgetMs) continue;
                    for (int f = 0; f < found.Count; f++)
                    {
                        if (used[f]) continue;
                        float d = Vector2.Distance(lastCenter[slot], found[f].center);
                        if (d <= bestDist) { bestDist = d; bestSlot = slot; bestFound = f; }
                    }
                }
                if (bestSlot < 0) break;
                hands[bestSlot] = found[bestFound];
                used[bestFound] = true;
            }
        }

        // New hands: MediaPipe's label. Two with the same label (it sometimes does that): the second gets the free one.
        for (int f = 0; f < found.Count; f++)
        {
            if (used[f]) continue;
            Hand which = labels[f];
            if (hands[(int)which] != null) which = which.Other();
            if (hands[(int)which] != null) continue;
            hands[(int)which] = found[f];
        }

        for (int slot = 0; slot < 2; slot++)
        {
            if (hands[slot] == null) continue;
            lastCenter[slot] = hands[slot].center;
            lastSeenMs[slot] = nowMs;
        }
    }

    // Turns each seen colour patch into a HandData: every landmark at the palm patch, except the fingertip landmarks
    // (8, 4, 12, 16, 20), which get the fingertip dots found. So depth, touch and HandCollider work unchanged.
    private void UpdateColorHands()
    {
        var hands = new HandData[2];
        foreach (Hand patch in HandExtensions.Both)
        {
            if (!colorTracker.TryGetHand(patch, out Vector2 center, colorTips)) continue;
            Hand which = twoHands ? patch : Hand.Right; // one hand mode: whichever patch is seen counts as Right
            if (hands[(int)which] != null) continue;

            var data = new HandData();
            for (int i = 0; i < 21; i++) data.landmarks.Add(center);
            for (int t = 0; t < colorTips.Count && t < FingertipLandmarks.Length; t++)
                data.landmarks[FingertipLandmarks[t]] = colorTips[t];
            data.center = center;
            data.fingertip = colorTips.Count > 0 ? colorTips[0] : center;
            data.touchPoints.Add(center); // palm only: fingertip dots don't grab
            hands[(int)which] = data;
        }
        colorHands = hands;
    }

    // MediaPipe's hand if it sees it, else the colour patch's.
    private HandData Get(Hand hand)
    {
        lock (landmarkLock)
        {
            var data = latestHands[(int)hand];
            if (data != null) return data;
        }
        return colorHands[(int)hand];
    }

    private bool FromColor(Hand hand)
    {
        lock (landmarkLock)
        {
            if (latestHands[(int)hand] != null) return false;
        }
        return colorHands[(int)hand] != null;
    }

    // For the label: "no", "yes" (MediaPipe) or "colour".
    private string Source(Hand hand) => Get(hand) == null ? "no" : FromColor(hand) ? "colour" : "yes";

    // "Left" or "Right" as MediaPipe sees it (before Swap Handedness), or null if it gave none.
    private static string HandednessName(HandLandmarkerResult result, int index)
    {
        if (result.handedness == null || index >= result.handedness.Count) return null;
        var categories = result.handedness[index].categories;
        return categories != null && categories.Count > 0 ? categories[0].categoryName : null;
    }

    public bool TwoHands => twoHands;

    public bool IsTracked(Hand hand) => Get(hand) != null;

    // Copies that hand's 21 landmarks (MediaPipe normalized: x right, y down, 0-1) into `result`.
    public bool TryGetHandLandmarks(Hand hand, List<Vector2> result)
    {
        result.Clear();
        var data = Get(hand);
        if (data == null) return false;
        result.AddRange(data.landmarks);
        return true;
    }

    // MediaPipe landmark (normalized to the whole camera image) -> normalized position in the AR view (x right, y down, 0-1).
    // The view has the phone screen's shape (ARKit fills it, cropping the image), even though it renders into a half-width RenderTexture.
    // HandDepthEstimator keeps using the raw image landmarks, since the depth image is not cropped.
    public Vector2 ToViewport(Vector2 p)
    {
        if (!correctCameraCrop || imageAspect <= 0f) return p;
        float viewAspect = (float)Screen.width / Screen.height;
        if (viewAspect > imageAspect)
            p.y = 0.5f + (p.y - 0.5f) * (viewAspect / imageAspect); // top and bottom of the image cropped
        else
            p.x = 0.5f + (p.x - 0.5f) * (imageAspect / viewAspect); // sides cropped
        return p;
    }

    // Centre of that hand in screen pixels (bottom-left origin, like WorldToScreenPoint).
    // Held items and crate spawns use it, since a grab gesture closes the hand and the fingertip no longer marks where the hand is.
    public bool TryGetHandCenterScreenPos(Hand hand, out Vector2 screenPos)
    {
        var data = Get(hand);
        screenPos = data != null ? ToScreen(data.center) : Vector2.zero;
        return data != null;
    }

    // MediaPipe landmark -> AR camera pixels (bottom-left origin).
    private Vector2 ToScreen(Vector2 landmark)
    {
        var p = ToViewport(landmark);
        Camera arCamera = arCameraManager.GetComponent<Camera>();
        return new Vector2(p.x * arCamera.pixelWidth, (1f - p.y) * arCamera.pixelHeight);
    }

    // Uses the AR camera's pixel size so landmark positions match WorldToScreenPoint.
    void Update()
    {
        if (arCameraManager == null)
            return;

        // Re-scan every frame instead of once in Start(), since interactables
        // (like QR-spawned Knife/Carrot) can appear after this script starts.
        interactables.Clear();
        foreach (var interactable in FindObjectsByType<HandInteractable>(FindObjectsInactive.Exclude))
            interactables.Add(interactable);

        if (interactables.Count == 0)
            return;

        var hands = new[] { Get(Hand.Left), Get(Hand.Right) }; // HandData is replaced, never changed, so safe to keep

        Camera arCamera = arCameraManager.GetComponent<Camera>();
        var leftPoints = TouchScreenPoints(hands[(int)Hand.Left]);
        var rightPoints = TouchScreenPoints(hands[(int)Hand.Right]);

        foreach (var interactable in interactables)
        {
            if (interactable.IsButton)
            {
                // A button is touched anywhere over its own rectangle, not within a circle.
                if (interactable.TryGetScreenRect(arCamera, out UnityEngine.Rect rect))
                    interactable.SetHandDistances(NearestDistance(leftPoints, rect), NearestDistance(rightPoints, rect));
                else
                    interactable.SetHandDistances(float.PositiveInfinity, float.PositiveInfinity);
                continue;
            }
            Vector3 objectScreenPos = arCamera.WorldToScreenPoint(interactable.ReachPosition);
            interactable.SetHandDistances(NearestDistance(leftPoints, objectScreenPos), NearestDistance(rightPoints, objectScreenPos));
        }
    }

    private List<Vector2> TouchScreenPoints(HandData hand)
    {
        var points = new List<Vector2>();
        if (hand != null)
            foreach (var raw in hand.touchPoints) points.Add(ToScreen(raw));
        return points;
    }

    // Nearest palm landmark counts, so the item is touched if any of them is within its radius. Infinity = no hand.
    private static float NearestDistance(List<Vector2> touchScreenPoints, Vector3 objectScreenPos)
    {
        float minDist = float.PositiveInfinity;
        if (objectScreenPos.z <= 0f) return minDist;
        foreach (var touchPoint in touchScreenPoints)
        {
            float dist = Vector2.Distance(touchPoint, objectScreenPos);
            if (dist < minDist)
                minDist = dist;
        }
        return minDist;
    }

    // Same, but to the edge of a button's on-screen rectangle (0 = the palm point is inside it).
    private static float NearestDistance(List<Vector2> touchScreenPoints, UnityEngine.Rect rect)
    {
        float minDist = float.PositiveInfinity;
        foreach (var p in touchScreenPoints)
        {
            float dx = Mathf.Max(rect.xMin - p.x, 0f, p.x - rect.xMax);
            float dy = Mathf.Max(rect.yMin - p.y, 0f, p.y - rect.yMax);
            minDist = Mathf.Min(minDist, Mathf.Sqrt(dx * dx + dy * dy));
        }
        return minDist;
    }

    void OnGUI()
    {
        int handCount;
        lock (landmarkLock)
        {
            handCount = lastDetectedHandCount;
        }
        var hands = new[] { Get(Hand.Left), Get(Hand.Right) };

        string which = twoHands
            ? $"  (Left: {Source(Hand.Left)}, Right: {Source(Hand.Right)})"
            : $"  (one hand mode: {Source(Hand.Right)})";
        GUI.Label(new UnityEngine.Rect(10, 10, 600, 30), $"Hands detected: {handCount}{which}");

        GUIStyle dotStyle = new GUIStyle();
        dotStyle.normal.background = Texture2D.whiteTexture;

        // Draw the dots where each eye image actually is (HeadsetEyeLayout moves and crops them).
        var layout = HeadsetEyeLayout.Active;
        UnityEngine.Rect[] halves = layout != null
            ? new[] { layout.LeftEyeGuiRect, layout.RightEyeGuiRect }
            : new[] {
                new UnityEngine.Rect(0, 0, Screen.width / 2f, Screen.height),
                new UnityEngine.Rect(Screen.width / 2f, 0, Screen.width / 2f, Screen.height)
            };
        var uv = layout != null ? layout.UvRect : new UnityEngine.Rect(0, 0, 1, 1);

        // Right hand red (as before), left hand blue.
        foreach (Hand hand in new[] { Hand.Right, Hand.Left })
        {
            var data = hands[(int)hand];
            if (data == null) continue;
            var normalizedPoints = new List<Vector2>(data.landmarks.Count);
            foreach (var p in data.landmarks) normalizedPoints.Add(ToViewport(p));
            GUI.color = hand == Hand.Right ? UnityEngine.Color.red : UnityEngine.Color.orange;
            DrawDots(normalizedPoints, halves, uv, dotStyle);
        }

        if (showReachPoints && arCameraManager != null)
        {
            // Reach points in the same normalized form as landmarks (x right, y down, 0-1).
            Camera arCamera = arCameraManager.GetComponent<Camera>();
            var reachPoints = new List<Vector2>();
            foreach (var interactable in interactables)
            {
                if (interactable == null) continue;
                Vector3 sp = arCamera.WorldToScreenPoint(interactable.ReachPosition);
                if (sp.z > 0f) reachPoints.Add(new Vector2(sp.x / arCamera.pixelWidth, 1f - sp.y / arCamera.pixelHeight));
            }
            GUI.color = UnityEngine.Color.green;
            DrawDots(reachPoints, halves, uv, dotStyle);
        }
    }

    private static void DrawDots(List<Vector2> normalizedPoints, UnityEngine.Rect[] halves, UnityEngine.Rect uv, GUIStyle dotStyle)
    {
        foreach (var half in halves)
        {
            foreach (var point in normalizedPoints)
            {
                // point.y counts from the top, uv.y from the bottom.
                float u = (point.x - uv.x) / uv.width;
                float v = (point.y - (1f - uv.y - uv.height)) / uv.height;
                if (u < 0f || u > 1f || v < 0f || v > 1f)
                    continue; // cropped out of view

                float screenX = half.x + u * half.width;
                float screenY = half.y + v * half.height;
                GUI.Box(new UnityEngine.Rect(screenX - 5, screenY - 5, 10, 10), GUIContent.none, dotStyle);
            }
        }
    }

    void OnDestroy()
    {
        handLandmarker?.Close();
    }
}
