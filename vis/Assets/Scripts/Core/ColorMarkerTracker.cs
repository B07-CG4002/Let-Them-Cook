using System.Collections.Generic;
using Unity.Collections;
using UnityEngine;

// Backup hand tracker for when MediaPipe can't see a hand (e.g. gloves). Each glove has a brightly coloured patch
// on the back of the hand, a different colour per hand, so left and right are always told apart. The patch is used
// as that hand's palm centre. Optional small dots of a third colour on the fingertips become that hand's fingertips
// (for HandCollider). HandTrackingManager hands it every camera frame and uses it for any hand MediaPipe misses.
// Positions are in the same normalized camera-image coordinates as MediaPipe's landmarks (x right, y down, 0-1).
public class ColorMarkerTracker : MonoBehaviour
{
    [Header("Patch colours (hue in degrees: 0 red, 60 yellow, 120 green, 180 cyan, 240 blue, 300 magenta)")]
    [Tooltip("Patch on the RIGHT glove.")]
    [SerializeField, Range(0f, 360f)] private float rightHue = 120f;
    [Tooltip("Patch on the LEFT glove.")]
    [SerializeField, Range(0f, 360f)] private float leftHue = 300f;
    [Tooltip("Degrees either side of each hue that still count.")]
    [SerializeField, Range(1f, 90f)] private float hueRange = 25f;
    [Tooltip("0-1. Raise it if dull or grey things are picked up (the beige glove itself must stay black in the debug view); " +
        "lower it if a patch is missed in dim light.")]
    [SerializeField, Range(0f, 1f)] private float minSaturation = 0.45f;
    [Tooltip("0-1 brightness. Raise it if dark things are picked up; lower it if a patch is missed in dim light.")]
    [SerializeField, Range(0f, 1f)] private float minValue = 0.25f;

    [Header("Fingertip dots (optional)")]
    [Tooltip("Look for small dots of a third colour on the fingertips. Untick if the gloves have none.")]
    [SerializeField] private bool trackFingertips = true;
    [SerializeField, Range(0f, 360f)] private float tipHue = 220f;
    [SerializeField, Range(1f, 90f)] private float tipHueRange = 20f;
    [Tooltip("How far from a palm patch its fingertip dots can be, as a fraction of the image. Each dot goes to the nearest patch.")]
    [SerializeField, Range(0.05f, 0.5f)] private float tipSearchRadius = 0.25f;
    [Tooltip("Fewest matching (checked) pixels that count as one dot.")]
    [SerializeField] private int minTipPixels = 3;

    [Header("Search")]
    [Tooltip("Check every Nth pixel in each direction. Higher = cheaper (less heat) but small patches may be missed.")]
    [SerializeField, Range(2, 16)] private int pixelStep = 6;
    [Tooltip("Fewest matching (checked) pixels that count as a patch. Lower it if a far-away patch is missed.")]
    [SerializeField] private int minPixels = 20;
    [Tooltip("Size of the area averaged around a patch, as a fraction of the image. A bit bigger than the patch looks.")]
    [SerializeField, Range(0.02f, 0.3f)] private float clusterRadius = 0.08f;
    [Tooltip("Seconds a hand stays usable after its patch was last seen.")]
    [SerializeField] private float maxAge = 0.2f;
    [Tooltip("0 = no smoothing, closer to 1 = smoother but laggier.")]
    [SerializeField, Range(0f, 0.95f)] private float smoothing = 0.4f;
    [Tooltip("Only if the dot sits mirrored left-right from the patch.")]
    [SerializeField] private bool flipX = false;
    [Tooltip("Only if the dot sits mirrored up-down from the patch.")]
    [SerializeField] private bool flipY = false;

    [Header("Debug")]
    [Tooltip("Shows what counts as each colour (green = right patch, magenta = left patch, blue = fingertip dot), " +
        "with each found palm in red and fingertips in yellow.")]
    [SerializeField] private bool showDebugView = false;
    [SerializeField] private float debugViewHeight = 240f;

    private const byte None = 0, LeftPatch = 1, RightPatch = 2, Tip = 3;
    private const int Cell = 8;

    private class HandState
    {
        public Vector2 rawCenter;           // sampled-grid normalized (as stored in the buffer)
        public Vector2 center;              // MediaPipe normalized
        public float lastFound = float.NegativeInfinity;
        public bool foundThisFrame;
        public readonly List<Vector2> rawTips = new List<Vector2>();
        public readonly List<Vector2> tips = new List<Vector2>(); // MediaPipe normalized
    }

    private readonly HandState[] hands = { new HandState(), new HandState() }; // indexed by Hand
    private byte[] mask;
    private int[] visited, queue;
    private int visitStamp;
    private int[][] cellCounts = new int[2][];
    private int gw, gh, cellsX, cellsY;
    private readonly List<Vector3> tipBlobs = new List<Vector3>(); // x, y = raw centre, z = pixel count
    private Texture2D debugTexture;
    private Color32[] debugPixels;

    public bool IsTracking(Hand hand) => isActiveAndEnabled && Time.time - hands[(int)hand].lastFound <= maxAge;

    // That hand's palm centre and fingertips (MediaPipe normalized). False if its patch isn't seen.
    public bool TryGetHand(Hand hand, out Vector2 center, List<Vector2> tips)
    {
        tips.Clear();
        var h = hands[(int)hand];
        center = h.center;
        if (!IsTracking(hand)) return false;
        tips.AddRange(h.tips);
        return true;
    }

    // rgba: the RGBA32 camera image HandTrackingManager gives MediaPipe (row 0 at the top).
    public void ProcessFrame(NativeArray<byte> rgba, int width, int height)
    {
        if (!isActiveAndEnabled) return;

        gw = width / pixelStep;
        gh = height / pixelStep;
        if (mask == null || mask.Length != gw * gh)
        {
            mask = new byte[gw * gh];
            visited = new int[gw * gh];
            queue = new int[gw * gh];
        }
        cellsX = (gw + Cell - 1) / Cell;
        cellsY = (gh + Cell - 1) / Cell;
        for (int k = 0; k < 2; k++)
        {
            if (cellCounts[k] == null || cellCounts[k].Length != cellsX * cellsY) cellCounts[k] = new int[cellsX * cellsY];
            System.Array.Clear(cellCounts[k], 0, cellCounts[k].Length);
        }

        // Pass 1: classify every Nth pixel by colour, and count patch hits per 8x8 block.
        float minS = minSaturation, minV = minValue * 255f;
        for (int gy = 0; gy < gh; gy++)
        {
            int rowStart = gy * pixelStep * width;
            for (int gx = 0; gx < gw; gx++)
            {
                int i = (rowStart + gx * pixelStep) * 4;
                float hue = Hue(rgba[i], rgba[i + 1], rgba[i + 2], minS, minV);
                byte c = None;
                if (HueNear(hue, rightHue, hueRange)) c = RightPatch;
                else if (HueNear(hue, leftHue, hueRange)) c = LeftPatch;
                else if (trackFingertips && HueNear(hue, tipHue, tipHueRange)) c = Tip;
                mask[gy * gw + gx] = c;
                if (c == LeftPatch || c == RightPatch) cellCounts[c - 1][(gy / Cell) * cellsX + gx / Cell]++;
            }
        }

        // Pass 2: find each hand's patch.
        FindPatch(Hand.Left, LeftPatch);
        FindPatch(Hand.Right, RightPatch);

        // Pass 3: fingertip dots, each given to the nearest found patch.
        foreach (var h in hands) { h.rawTips.Clear(); h.tips.Clear(); }
        if (trackFingertips && (hands[0].foundThisFrame || hands[1].foundThisFrame)) FindFingertips();

        DrawDebug();
    }

    // Starts from last frame's spot if the patch is still there, else from the busiest block, then pulls the
    // centre onto the patch with a few mean-shift steps.
    private void FindPatch(Hand hand, byte cls)
    {
        var h = hands[(int)hand];
        h.foundThisFrame = false;

        Vector2 start;
        if (IsTracking(hand) && CountAround(h.rawCenter, cls) >= minPixels)
        {
            start = h.rawCenter;
        }
        else
        {
            int[] counts = cellCounts[cls - 1];
            int best = 0;
            for (int c = 1; c < counts.Length; c++)
                if (counts[c] > counts[best]) best = c;
            if (counts[best] == 0) return;
            start = new Vector2(((best % cellsX) * Cell + Cell * 0.5f) / gw, ((best / cellsX) * Cell + Cell * 0.5f) / gh);
        }

        Vector2 current = start;
        int count = 0;
        for (int iter = 0; iter < 4; iter++)
            if (!MeanAround(current, cls, out current, out count)) break;
        if (count < minPixels) return;

        h.rawCenter = current;
        Vector2 landmark = ToLandmark(current);
        h.center = IsTracking(hand) ? Vector2.Lerp(landmark, h.center, smoothing) : landmark;
        h.lastFound = Time.time;
        h.foundThisFrame = true;
    }

    // Groups touching fingertip-colour pixels near the found patches into dots; each dot goes to the nearest patch.
    private void FindFingertips()
    {
        // Search the box covering every found patch's radius.
        float minX = 1f, minY = 1f, maxX = 0f, maxY = 0f;
        foreach (var h in hands)
        {
            if (!h.foundThisFrame) continue;
            minX = Mathf.Min(minX, h.rawCenter.x - tipSearchRadius); maxX = Mathf.Max(maxX, h.rawCenter.x + tipSearchRadius);
            minY = Mathf.Min(minY, h.rawCenter.y - tipSearchRadius); maxY = Mathf.Max(maxY, h.rawCenter.y + tipSearchRadius);
        }
        int x0 = Mathf.Max(0, Mathf.FloorToInt(minX * gw)), x1 = Mathf.Min(gw - 1, Mathf.CeilToInt(maxX * gw));
        int y0 = Mathf.Max(0, Mathf.FloorToInt(minY * gh)), y1 = Mathf.Min(gh - 1, Mathf.CeilToInt(maxY * gh));

        visitStamp++;
        tipBlobs.Clear();
        for (int y = y0; y <= y1; y++)
        for (int x = x0; x <= x1; x++)
        {
            int start = y * gw + x;
            if (mask[start] != Tip || visited[start] == visitStamp) continue;

            // Flood fill (4-neighbour) inside the search box.
            int head = 0, tail = 0, count = 0;
            float sx = 0f, sy = 0f;
            queue[tail++] = start;
            visited[start] = visitStamp;
            while (head < tail)
            {
                int idx = queue[head++];
                int px = idx % gw, py = idx / gw;
                sx += px + 0.5f;
                sy += py + 0.5f;
                count++;
                TryVisit(px - 1, py, x0, x1, y0, y1, ref tail);
                TryVisit(px + 1, py, x0, x1, y0, y1, ref tail);
                TryVisit(px, py - 1, x0, x1, y0, y1, ref tail);
                TryVisit(px, py + 1, x0, x1, y0, y1, ref tail);
            }
            if (count >= minTipPixels) tipBlobs.Add(new Vector3(sx / count / gw, sy / count / gh, count));
        }

        // Biggest dots first, so each hand keeps its 5 clearest.
        tipBlobs.Sort((a, b) => b.z.CompareTo(a.z));
        foreach (var blob in tipBlobs)
        {
            var p = new Vector2(blob.x, blob.y);
            HandState nearest = null;
            float nearestDist = tipSearchRadius;
            foreach (var h in hands)
            {
                if (!h.foundThisFrame) continue;
                float d = Vector2.Distance(p, h.rawCenter);
                if (d <= nearestDist) { nearest = h; nearestDist = d; }
            }
            if (nearest == null || nearest.tips.Count >= 5) continue;
            nearest.rawTips.Add(p);
            nearest.tips.Add(ToLandmark(p));
        }
    }

    private void TryVisit(int x, int y, int x0, int x1, int y0, int y1, ref int tail)
    {
        if (x < x0 || x > x1 || y < y0 || y > y1) return;
        int idx = y * gw + x;
        if (mask[idx] != Tip || visited[idx] == visitStamp) return;
        visited[idx] = visitStamp;
        queue[tail++] = idx;
    }

    // The buffer is the exact image MediaPipe gets, and our landmarks come back in its unrotated frame,
    // so this is the identity unless the flips are needed.
    private Vector2 ToLandmark(Vector2 raw) => new Vector2(flipX ? 1f - raw.x : raw.x, flipY ? 1f - raw.y : raw.y);

    private static bool HueNear(float hue, float target, float range)
    {
        if (hue < 0f) return false;
        float diff = Mathf.Abs(hue - target);
        if (diff > 180f) diff = 360f - diff;
        return diff <= range;
    }

    // Hue in degrees (0-360), or -1 if the pixel isn't colourful and bright enough to count.
    private static float Hue(byte r, byte g, byte b, float minS, float minV)
    {
        int max = Mathf.Max(r, Mathf.Max(g, b));
        int min = Mathf.Min(r, Mathf.Min(g, b));
        if (max < minV) return -1f;
        int delta = max - min;
        if (delta == 0 || (float)delta / max < minS) return -1f;

        float hue;
        if (max == r) hue = 60f * ((g - b) / (float)delta);
        else if (max == g) hue = 60f * ((b - r) / (float)delta + 2f);
        else hue = 60f * ((r - g) / (float)delta + 4f);
        if (hue < 0f) hue += 360f;
        return hue;
    }

    private int CountAround(Vector2 p, byte cls)
    {
        MeanAround(p, cls, out _, out int count);
        return count;
    }

    // Average position (normalized) of pixels of class cls within clusterRadius of p.
    private bool MeanAround(Vector2 p, byte cls, out Vector2 mean, out int count)
    {
        int x0 = Mathf.Max(0, Mathf.FloorToInt((p.x - clusterRadius) * gw));
        int x1 = Mathf.Min(gw - 1, Mathf.CeilToInt((p.x + clusterRadius) * gw));
        int y0 = Mathf.Max(0, Mathf.FloorToInt((p.y - clusterRadius) * gh));
        int y1 = Mathf.Min(gh - 1, Mathf.CeilToInt((p.y + clusterRadius) * gh));
        float sx = 0f, sy = 0f;
        count = 0;
        for (int y = y0; y <= y1; y++)
        for (int x = x0; x <= x1; x++)
        {
            if (mask[y * gw + x] != cls) continue;
            sx += x + 0.5f;
            sy += y + 0.5f;
            count++;
        }
        mean = count > 0 ? new Vector2(sx / count / gw, sy / count / gh) : p;
        return count > 0;
    }

    private void DrawDebug()
    {
        if (!showDebugView) return;
        if (debugTexture == null || debugTexture.width != gw || debugTexture.height != gh)
        {
            debugTexture = new Texture2D(gw, gh, TextureFormat.RGBA32, false) { filterMode = FilterMode.Point };
            debugPixels = new Color32[gw * gh];
        }
        for (int y = 0; y < gh; y++)
        for (int x = 0; x < gw; x++)
        {
            byte c = mask[y * gw + x];
            Color32 col = c == RightPatch ? new Color32(0, 255, 0, 255)
                : c == LeftPatch ? new Color32(255, 0, 255, 255)
                : c == Tip ? new Color32(0, 128, 255, 255)
                : new Color32(0, 0, 0, 255);
            debugPixels[(gh - 1 - y) * gw + x] = col; // Texture2D row 0 is the bottom
        }
        foreach (var h in hands)
        {
            if (!h.foundThisFrame) continue;
            DrawSquare(h.rawCenter, new Color32(255, 0, 0, 255));
            foreach (var t in h.rawTips) DrawSquare(t, new Color32(255, 255, 0, 255));
        }
        debugTexture.SetPixels32(debugPixels);
        debugTexture.Apply();
    }

    private void DrawSquare(Vector2 p, Color32 colour)
    {
        int cx = Mathf.Clamp((int)(p.x * gw), 0, gw - 1), cy = Mathf.Clamp((int)(p.y * gh), 0, gh - 1);
        for (int oy = -2; oy <= 2; oy++)
        for (int ox = -2; ox <= 2; ox++)
        {
            int px = cx + ox, py = cy + oy;
            if (px < 0 || py < 0 || px >= gw || py >= gh) continue;
            debugPixels[(gh - 1 - py) * gw + px] = colour;
        }
    }

    private string Status(Hand hand) => IsTracking(hand) ? $"yes ({hands[(int)hand].tips.Count} tips)" : "no";

    void OnGUI()
    {
        if (!showDebugView || debugTexture == null) return;
        float width = debugViewHeight * debugTexture.width / debugTexture.height;
        var rect = new UnityEngine.Rect(10, 50, width, debugViewHeight);
        GUI.DrawTexture(rect, debugTexture);
        GUI.Label(new UnityEngine.Rect(rect.x, rect.yMax + 5, 400, 30), $"Colour  Left: {Status(Hand.Left)}  Right: {Status(Hand.Right)}");
    }
}
