using System.Collections.Generic;
using Unity.Collections;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

// Reads the LiDAR depth map (the same one AROcclusionManager uses for occlusion) at the tracked hand's pixels,
// giving the hand's real distance from the camera in meters.
// The depth image covers the same view as the raw camera image, just at lower resolution (256x192 on iPhone).
// MediaPipe sees that camera image rotated by HandTrackingManager's Rotation Degrees, so landmarks are rotated back
// before looking up depth. Turn on Show Debug View to check the red dots sit on your finger in the depth picture.
public class HandDepthEstimator : MonoBehaviour
{
    [SerializeField] private AROcclusionManager occlusionManager;
    [SerializeField] private HandTrackingManager handTrackingManager;

    [Header("Landmark -> depth image mapping")]
    [Tooltip("Match HandTrackingManager's Rotation Degrees (0, 90, 180 or 270).")]
    [SerializeField] private int landmarkRotation = 270;
    [Tooltip("Tick if MediaPipe's landmarks turn out to be in the unrotated image already (dots in the debug view sit rotated away from the hand).")]
    [SerializeField] private bool landmarksAlreadyUnrotated = false;
    [SerializeField] private bool flipX = false;
    [SerializeField] private bool flipY = false;

    [Header("Sampling")]
    [Tooltip("MediaPipe landmarks to sample. Default = the palm (wrist, thumb base, finger knuckles), the same points held items follow, " +
        "so closing the hand doesn't change the depth. The old default 5-8 was the index finger, knuckle to tip.")]
    [SerializeField] private int[] sampleLandmarks = { 0, 1, 5, 9, 13, 17 };
    [Tooltip("Depth pixels around each landmark to sample. 1 = a 3x3 patch.")]
    [SerializeField] private int patchRadius = 1;
    [Tooltip("Skip LiDAR readings ARKit marks as low confidence (common on hand edges).")]
    [SerializeField] private bool skipLowConfidence = true;
    [Tooltip("The hand is the nearest thing around its landmarks, so take a low percentile to ignore background pixels.")]
    [SerializeField, Range(0f, 1f)] private float percentile = 0.25f;
    [SerializeField] private float minDepth = 0.1f;
    [SerializeField] private float maxDepth = 1.5f;
    [Tooltip("0 = no smoothing, closer to 1 = smoother but laggier.")]
    [SerializeField, Range(0f, 0.95f)] private float smoothing = 0.6f;
    [Tooltip("Seconds a reading stays usable after the last valid sample.")]
    [SerializeField] private float maxAge = 0.3f;

    [Header("Debug")]
    [Tooltip("Draws the depth map as MediaPipe sees it (near = bright) with red dots where depth is sampled, plus the reading.")]
    [SerializeField] private bool showDebugView = false;
    [SerializeField] private float debugViewHeight = 300f;

    private readonly List<Vector2> landmarks = new List<Vector2>();
    private readonly List<float> samples = new List<float>();
    private float smoothedDepth;
    private float lastValidTime = float.NegativeInfinity;
    private Texture2D debugTexture;
    private Color32[] debugPixels;

    public bool HasDepth => Time.time - lastValidTime <= maxAge;
    public float Depth => smoothedDepth; // meters along the camera's forward axis, same as WorldToScreenPoint().z

    // MediaPipe normalized landmark (x right, y down, in the image it was given) -> normalized raw camera/depth image coords.
    // MediaPipe rotates clockwise, so this undoes that rotation.
    private Vector2 LandmarkToRaw(Vector2 p)
    {
        float u = flipX ? 1f - p.x : p.x;
        float v = flipY ? 1f - p.y : p.y;
        if (landmarksAlreadyUnrotated) return new Vector2(u, v);
        switch (((landmarkRotation % 360) + 360) % 360)
        {
            case 90: return new Vector2(v, 1f - u);
            case 180: return new Vector2(1f - u, 1f - v);
            case 270: return new Vector2(1f - v, u);
            default: return new Vector2(u, v);
        }
    }

    void Update()
    {
        if (occlusionManager == null || handTrackingManager == null) return;
        if (!handTrackingManager.TryGetPrimaryHandLandmarks(landmarks)) return;
        if (!occlusionManager.TryAcquireEnvironmentDepthCpuImage(out XRCpuImage depthImage)) return;

        XRCpuImage confImage = default;
        bool acquiredConf = skipLowConfidence && occlusionManager.TryAcquireEnvironmentDepthConfidenceCpuImage(out confImage);
        bool hasConf = acquiredConf;
        try
        {
            if (depthImage.format != XRCpuImage.Format.DepthFloat32) return;
            var depthPlane = depthImage.GetPlane(0);
            NativeArray<float> depth = depthPlane.data.Reinterpret<float>(1);
            int depthRow = depthPlane.rowStride / sizeof(float);
            NativeArray<byte> conf = default;
            int confRow = 0;
            if (hasConf)
            {
                var confPlane = confImage.GetPlane(0);
                conf = confPlane.data;
                confRow = confPlane.rowStride;
                hasConf = confImage.width == depthImage.width && confImage.height == depthImage.height;
            }

            int w = depthImage.width, h = depthImage.height;
            if (showDebugView) DrawDebugDepth(depth, depthRow, w, h);

            samples.Clear();
            foreach (int index in sampleLandmarks)
            {
                if (index < 0 || index >= landmarks.Count) continue;
                Vector2 raw = LandmarkToRaw(landmarks[index]);
                int cx = Mathf.RoundToInt(raw.x * (w - 1));
                int cy = Mathf.RoundToInt(raw.y * (h - 1)); // row 0 is the top
                for (int y = cy - patchRadius; y <= cy + patchRadius; y++)
                for (int x = cx - patchRadius; x <= cx + patchRadius; x++)
                {
                    if (x < 0 || y < 0 || x >= w || y >= h) continue;
                    if (hasConf && conf[y * confRow + x] == 0) continue; // ARKit: 0 low, 1 medium, 2 high
                    float d = depth[y * depthRow + x];
                    if (d >= minDepth && d <= maxDepth) samples.Add(d);
                }
            }
            if (showDebugView) debugTexture.Apply();
            if (samples.Count == 0) return;

            samples.Sort();
            float reading = samples[Mathf.Clamp(Mathf.FloorToInt(samples.Count * percentile), 0, samples.Count - 1)];
            smoothedDepth = HasDepth ? Mathf.Lerp(reading, smoothedDepth, smoothing) : reading;
            lastValidTime = Time.time;
        }
        finally
        {
            depthImage.Dispose();
            if (acquiredConf) confImage.Dispose();
        }
    }

    // Renders the depth map in MediaPipe's (rotated) frame, so it lines up with how the hand appears to MediaPipe,
    // and marks each sampled landmark in red. If the dots sit on the finger, the mapping is right.
    private void DrawDebugDepth(NativeArray<float> depth, int depthRow, int w, int h)
    {
        bool swap = !landmarksAlreadyUnrotated && (((landmarkRotation % 360) + 360) % 360 == 90 || ((landmarkRotation % 360) + 360) % 360 == 270);
        int tw = swap ? h : w, th = swap ? w : h;
        if (debugTexture == null || debugTexture.width != tw || debugTexture.height != th)
        {
            debugTexture = new Texture2D(tw, th, TextureFormat.RGBA32, false) { filterMode = FilterMode.Point };
            debugPixels = new Color32[tw * th];
        }

        for (int ty = 0; ty < th; ty++)
        for (int tx = 0; tx < tw; tx++)
        {
            Vector2 raw = LandmarkToRaw(new Vector2((tx + 0.5f) / tw, (ty + 0.5f) / th));
            int x = Mathf.Clamp((int)(raw.x * w), 0, w - 1), y = Mathf.Clamp((int)(raw.y * h), 0, h - 1);
            float d = depth[y * depthRow + x];
            byte g = (byte)(255f * (1f - Mathf.Clamp01((d - minDepth) / (maxDepth - minDepth))));
            debugPixels[(th - 1 - ty) * tw + tx] = new Color32(g, g, g, 255); // Texture2D row 0 is the bottom
        }

        foreach (int index in sampleLandmarks)
        {
            if (index < 0 || index >= landmarks.Count) continue;
            int tx = Mathf.Clamp((int)(landmarks[index].x * tw), 0, tw - 1);
            int ty = Mathf.Clamp((int)(landmarks[index].y * th), 0, th - 1);
            for (int oy = -2; oy <= 2; oy++)
            for (int ox = -2; ox <= 2; ox++)
            {
                int px = tx + ox, py = ty + oy;
                if (px < 0 || py < 0 || px >= tw || py >= th) continue;
                debugPixels[(th - 1 - py) * tw + px] = new Color32(255, 0, 0, 255);
            }
        }
        debugTexture.SetPixels32(debugPixels);
    }

    void OnGUI()
    {
        if (!showDebugView || debugTexture == null) return;
        float width = debugViewHeight * debugTexture.width / debugTexture.height;
        var rect = new UnityEngine.Rect(Screen.width - width - 10, 10, width, debugViewHeight);
        GUI.DrawTexture(rect, debugTexture);
        GUI.Label(new UnityEngine.Rect(rect.x, rect.yMax + 5, width, 30),
            HasDepth ? $"Hand depth: {smoothedDepth:F2} m" : "Hand depth: no reading");
    }
}
