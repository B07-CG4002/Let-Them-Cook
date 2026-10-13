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

    private HandLandmarker handLandmarker;
    private Texture2D cameraTexture;
    private List<HandInteractable> interactables = new List<HandInteractable>();

    private List<Vector2> latestLandmarkPoints = new List<Vector2>();
    private List<Vector2> latestFingertips = new List<Vector2>();
    private List<Vector2> latestHandCenters = new List<Vector2>();
    private int lastDetectedHandCount = 0;
    private readonly object landmarkLock = new object();

    // Wrist + the four knuckles at the base of the fingers. These barely move when the hand closes into a fist,
    // unlike the index fingertip (8), which curls into the palm.
    private static readonly int[] HandCenterLandmarks = { 0, 5, 9, 13, 17 };

    void Start()
    {
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

        if (cameraTexture == null || cameraTexture.width != width || cameraTexture.height != height)
            cameraTexture = new Texture2D(width, height, conversionParams.outputFormat, false);

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
        var points = new List<Vector2>();
        var fingertips = new List<Vector2>();
        var handCenters = new List<Vector2>();
        int handCount = result.handLandmarks?.Count ?? 0;

        if (result.handLandmarks != null)
        {
            foreach (var hand in result.handLandmarks)
            {
                foreach (var landmark in hand.landmarks)
                    points.Add(new Vector2(landmark.x, landmark.y));

                if (hand.landmarks.Count > 8)
                {
                    var tip = hand.landmarks[8];
                    fingertips.Add(new Vector2(tip.x, tip.y));
                }

                if (hand.landmarks.Count > 17)
                {
                    Vector2 sum = Vector2.zero;
                    foreach (int i in HandCenterLandmarks)
                        sum += new Vector2(hand.landmarks[i].x, hand.landmarks[i].y);
                    handCenters.Add(sum / HandCenterLandmarks.Length);
                }
            }
        }

        lock (landmarkLock)
        {
            latestLandmarkPoints = points;
            latestFingertips = fingertips;
            latestHandCenters = handCenters;
            lastDetectedHandCount = handCount;
        }
    }

    // Copies the first hand's 21 landmarks (MediaPipe normalized: x right, y down, 0-1) into `result`.
    public bool TryGetPrimaryHandLandmarks(List<Vector2> result)
    {
        result.Clear();
        lock (landmarkLock)
        {
            if (latestLandmarkPoints.Count < 21) return false;
            for (int i = 0; i < 21; i++) result.Add(latestLandmarkPoints[i]);
        }
        return true;
    }

    public Vector2 GetPrimaryFingertipScreenPos()
    {
        lock (landmarkLock)
        {
            if (latestFingertips.Count > 0)
            {
                var fp = latestFingertips[0];
                Camera arCamera = arCameraManager.GetComponent<Camera>();
                return new Vector2(fp.x * arCamera.pixelWidth, (1f - fp.y) * arCamera.pixelHeight);
            }
        }
        return Vector2.zero;
    }

    // Centre of the primary hand in screen pixels (bottom-left origin, like WorldToScreenPoint).
    // Used for crate spawns, since a grab gesture closes the hand and the fingertip no longer marks where the hand is.
    public bool TryGetPrimaryHandCenterScreenPos(out Vector2 screenPos)
    {
        lock (landmarkLock)
        {
            if (latestHandCenters.Count > 0)
            {
                var c = latestHandCenters[0];
                Camera arCamera = arCameraManager.GetComponent<Camera>();
                screenPos = new Vector2(c.x * arCamera.pixelWidth, (1f - c.y) * arCamera.pixelHeight);
                return true;
            }
        }
        screenPos = Vector2.zero;
        return false;
    }

    // Uses the AR camera's pixel size so fingertip positions match WorldToScreenPoint.
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

        List<Vector2> fingertips;
        lock (landmarkLock)
        {
            fingertips = new List<Vector2>(latestFingertips);
        }

        Camera arCamera = arCameraManager.GetComponent<Camera>();
        var pixelRect = new UnityEngine.Rect(0, 0, arCamera.pixelWidth, arCamera.pixelHeight);

        var fingertipLocalPoints = new List<Vector2>();
        foreach (var fingertip in fingertips)
        {
            fingertipLocalPoints.Add(new Vector2(
                fingertip.x * pixelRect.width,
                (1f - fingertip.y) * pixelRect.height
            ));
        }

        foreach (var interactable in interactables)
        {
            Vector3 objectScreenPos = arCamera.WorldToScreenPoint(interactable.transform.position);
            float minDist = float.PositiveInfinity;

            if (objectScreenPos.z > 0f)
            {
                foreach (var fingertipLocal in fingertipLocalPoints)
                {
                    float dist = Vector2.Distance(fingertipLocal, objectScreenPos);
                    if (dist < minDist)
                        minDist = dist;
                }
            }

            interactable.SetClosestHandDistance(minDist);
            interactable.SetTouched(minDist <= interactable.Radius);
        }
    }

    void OnGUI()
    {
        List<Vector2> normalizedPoints;
        int handCount;
        lock (landmarkLock)
        {
            normalizedPoints = new List<Vector2>(latestLandmarkPoints);
            handCount = lastDetectedHandCount;
        }

        GUI.Label(new UnityEngine.Rect(10, 10, 400, 30), $"Hands detected: {handCount}");

        GUIStyle dotStyle = new GUIStyle();
        dotStyle.normal.background = Texture2D.whiteTexture;
        GUI.color = UnityEngine.Color.red;

        // Draw the dots where each eye image actually is (HeadsetEyeLayout moves and crops them).
        var layout = HeadsetEyeLayout.Active;
        UnityEngine.Rect[] halves = layout != null
            ? new[] { layout.LeftEyeGuiRect, layout.RightEyeGuiRect }
            : new[] {
                new UnityEngine.Rect(0, 0, Screen.width / 2f, Screen.height),
                new UnityEngine.Rect(Screen.width / 2f, 0, Screen.width / 2f, Screen.height)
            };
        var uv = layout != null ? layout.UvRect : new UnityEngine.Rect(0, 0, 1, 1);

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