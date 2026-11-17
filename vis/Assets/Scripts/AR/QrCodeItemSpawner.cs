using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

// Spawns each marker's prefab the first time the marker is seen. It follows the marker for a moment while ARKit
// settles the marker's pose, then it is anchored in the world: from then on it stays put even if the marker moves,
// is covered or is lost. Grabbable children (knife, pot, ingredients) can still be picked up and moved as before.
// A station whose footprint on the table overlaps an anchored one's isn't anchored until its marker is moved clear.
public class QrCodeItemSpawner : MonoBehaviour
{
    [System.Serializable]
    public class ImagePrefabPair
    {
        public string imageName; // must exactly match the entry's Name in your Reference Image Library
        public GameObject prefab;
        public Vector3 localOffset;
        public Vector3 localScale = Vector3.one;
    }

    [SerializeField] private ARTrackedImageManager trackedImageManager;
    [SerializeField] private List<ImagePrefabPair> imagePrefabs;

    [Header("Anchoring")]
    [Tooltip("Seconds the marker must hold still (while tracked) before its object is anchored. " +
        "ARKit's first pose for a marker is often a little off, so the object follows it until it settles.")]
    [SerializeField] private float settleTime = 0.5f;
    [Tooltip("Anchor anyway after this many seconds of tracking, even if the marker never held still.")]
    [SerializeField] private float maxSettleTime = 3f;
    [Tooltip("How far (m) and how much (degrees) the marker may wobble and still count as holding still.")]
    [SerializeField] private float stillDistance = 0.01f;
    [SerializeField] private float stillAngle = 3f;
    [Tooltip("Pin each object with an ARKit anchor, so it stays put when ARKit corrects its map. Needs an AR Anchor " +
        "Manager on the XR Origin; without one (or unticked) the object just keeps its position in the AR session.")]
    [SerializeField] private bool useArAnchor = true;
    [Tooltip("Switch image tracking off once every marker in the list above is anchored. Saves battery and heat; " +
        "anchored objects don't need it. Untick if some markers in the list aren't in this scene.")]
    [SerializeField] private bool stopTrackingWhenAllAnchored = true;

    [Header("Overlap check")]
    [Tooltip("How far (m) two stations' footprints may run into each other before it counts as an overlap. " +
        "Covers ARKit's marker wobble of a centimetre or so. 0 = any touch counts.")]
    [SerializeField] private float overlapAllowance = 0.01f;

    private class Spawned
    {
        public GameObject obj;
        public bool anchored;
        public Vector3 stillPos;      // marker pose at the start of the current still stretch
        public Quaternion stillRot;
        public float stillFor;        // seconds the marker has stayed near stillPos/stillRot
        public float trackedFor;      // seconds tracked in total, for maxSettleTime
        public bool hasFootprint;
        public Rect footprint;        // its meshes' outline on the table, in its marker's frame (x, z), measured once
    }

    private readonly Dictionary<string, Spawned> spawned = new Dictionary<string, Spawned>();
    private bool canAnchor;

    // Every anchored marker's pose. Dropped items fall to the nearest one's height (FallToSurface).
    public static readonly List<Transform> Anchors = new List<Transform>();
    // Stations waiting because they overlap an anchored one: image name -> name of the one it overlaps.
    public static readonly Dictionary<string, string> Blocked = new Dictionary<string, string>();

    void Awake()
    {
        Anchors.Clear(); // static, so a reloaded scene starts empty
        Blocked.Clear();
        canAnchor = useArAnchor && FindAnyObjectByType<ARAnchorManager>() != null;
        if (useArAnchor && !canAnchor)
            Debug.LogWarning("[QR] No AR Anchor Manager in the scene; objects are anchored by position only.");
    }

    // The AR session outlives scene loads, and ARKit reports each marker only once per session, so after a round the
    // markers would never be seen again. A reset makes this scene scan them afresh.
    void Start()
    {
        var session = FindAnyObjectByType<ARSession>();
        if (session != null) session.Reset();
    }

    void OnEnable()
    {
        trackedImageManager.trackablesChanged.AddListener(OnTrackablesChanged);
    }

    void OnDisable()
    {
        trackedImageManager.trackablesChanged.RemoveListener(OnTrackablesChanged);
    }

    private ImagePrefabPair GetPairFor(string imageName)
    {
        foreach (var pair in imagePrefabs)
        {
            if (pair.imageName == imageName)
                return pair;
        }
        return null;
    }

    private void OnTrackablesChanged(ARTrackablesChangedEventArgs<ARTrackedImage> args)
    {
        foreach (var trackedImage in args.added)
        {
            string imageName = trackedImage.referenceImage.name;
            var pair = GetPairFor(imageName);
            if (pair == null || spawned.ContainsKey(imageName))
                continue;

            var obj = Instantiate(pair.prefab, trackedImage.transform.position, trackedImage.transform.rotation * pair.prefab.transform.rotation);
            // Child of the marker until anchored, so it follows the marker while ARKit settles its pose.
            obj.transform.SetParent(trackedImage.transform, worldPositionStays: true);
            obj.transform.localPosition = pair.localOffset;
            obj.transform.localScale = pair.localScale;
            spawned[imageName] = new Spawned
            {
                obj = obj,
                stillPos = trackedImage.transform.position,
                stillRot = trackedImage.transform.rotation
            };
        }

        // Fires every frame for every tracked marker; anchored ones cost one dictionary lookup.
        foreach (var trackedImage in args.updated)
        {
            if (!spawned.TryGetValue(trackedImage.referenceImage.name, out var s) || s.anchored) continue;
            if (trackedImage.trackingState != TrackingState.Tracking) continue; // out of view: wait, don't count

            Transform t = trackedImage.transform;
            if (Vector3.Distance(t.position, s.stillPos) > stillDistance || Quaternion.Angle(t.rotation, s.stillRot) > stillAngle)
            {
                s.stillPos = t.position;
                s.stillRot = t.rotation;
                s.stillFor = 0f;
            }
            else s.stillFor += Time.deltaTime;
            s.trackedFor += Time.deltaTime;

            if (s.stillFor < settleTime && s.trackedFor < maxSettleTime) continue;
            string imageName = trackedImage.referenceImage.name;
            if (Overlapping(s, out string other))
            {
                Blocked[imageName] = other;
                s.trackedFor = 0f; // once clear, settle again before anchoring
                continue;
            }
            Blocked.Remove(imageName);
            Anchor(trackedImage, s);
        }

        foreach (var removed in args.removed)
        {
            // An anchored object no longer depends on its marker. One that hadn't settled is still the marker's
            // child and goes with it, so forget it and spawn it again when the marker is next seen.
            string imageName = removed.Value.referenceImage.name;
            if (spawned.TryGetValue(imageName, out var s) && !s.anchored)
            {
                Destroy(s.obj);
                spawned.Remove(imageName);
                Blocked.Remove(imageName);
            }
        }
    }

    // Moves the object from the marker onto a fixed point at the marker's current pose.
    private void Anchor(ARTrackedImage trackedImage, Spawned s)
    {
        string imageName = trackedImage.referenceImage.name;
        var anchor = new GameObject($"Anchor {imageName}");
        // Same parent as the markers (the XR Origin's Trackables), so it stays in AR session space.
        anchor.transform.SetParent(trackedImage.transform.parent, worldPositionStays: false);
        anchor.transform.SetPositionAndRotation(trackedImage.transform.position, trackedImage.transform.rotation);
        if (canAnchor) anchor.AddComponent<ARAnchor>();

        s.obj.transform.SetParent(anchor.transform, worldPositionStays: true);
        s.anchored = true;
        Anchors.Add(anchor.transform);
        Debug.Log($"[QR] {imageName} anchored after {s.trackedFor:F1}s (still for {s.stillFor:F1}s)");

        if (stopTrackingWhenAllAnchored && AllAnchored())
        {
            trackedImageManager.enabled = false;
            Debug.Log("[QR] Every marker anchored; image tracking switched off.");
        }
    }

    // Kitchen reset for a new round without rescanning: every loose item (ingredients, plates, dirty plate stacks) is
    // destroyed, and each anchored station is replaced by a fresh copy of its prefab on the same anchor.
    public void ResetStations()
    {
        foreach (var g in FindObjectsByType<Grabbable>(FindObjectsInactive.Include))
            Destroy(g.gameObject);
        foreach (var pair in spawned)
        {
            var s = pair.Value;
            var p = GetPairFor(pair.Key);
            if (!s.anchored || p == null) continue;
            Transform anchor = s.obj.transform.parent;
            Destroy(s.obj);
            s.obj = Instantiate(p.prefab, anchor);
            s.obj.transform.localPosition = p.localOffset;
            s.obj.transform.localRotation = p.prefab.transform.rotation;
            s.obj.transform.localScale = p.localScale;
        }
        Debug.Log("[QR] Kitchen reset");
    }

    // Whether a station's footprint overlaps an anchored station's footprint. Footprints are measured once in each
    // marker's own frame, so the answer depends only on where the markers are, not on the order they were scanned in,
    // the angle of the AR world's axes, or what has been put on or taken off a station since.
    private bool Overlapping(Spawned station, out string other)
    {
        other = null;
        if (!Footprint(station, out Vector2[] mine)) return false;
        foreach (var pair in spawned)
        {
            if (pair.Value == station || !pair.Value.anchored || !Footprint(pair.Value, out Vector2[] theirs)) continue;
            if (!Intersects(mine, theirs)) continue;
            other = pair.Key;
            return true;
        }
        return false;
    }

    // The station's footprint as 4 corners on the table (world x, z), each side pulled in by half the allowance.
    private bool Footprint(Spawned s, out Vector2[] corners)
    {
        corners = null;
        if (!s.hasFootprint)
        {
            if (!TryMeasureFootprint(s.obj, out s.footprint)) return false;
            s.hasFootprint = true;
        }
        Rect f = s.footprint;
        float inX = Mathf.Min(overlapAllowance * 0.5f, f.width * 0.5f);
        float inZ = Mathf.Min(overlapAllowance * 0.5f, f.height * 0.5f);
        Transform frame = s.obj.transform.parent; // the marker, or the anchor at the marker's pose
        corners = new[]
        {
            Flat(frame.TransformPoint(new Vector3(f.xMin + inX, 0f, f.yMin + inZ))),
            Flat(frame.TransformPoint(new Vector3(f.xMax - inX, 0f, f.yMin + inZ))),
            Flat(frame.TransformPoint(new Vector3(f.xMax - inX, 0f, f.yMax - inZ))),
            Flat(frame.TransformPoint(new Vector3(f.xMin + inX, 0f, f.yMax - inZ)))
        };
        return true;
    }

    private static Vector2 Flat(Vector3 p) => new Vector2(p.x, p.z);

    // Smallest rectangle, along the marker's own x and z (the marker lies flat on the table), holding every visible
    // mesh of the station. Uses each mesh's own box (Renderer.localBounds), not Renderer.bounds: that one is lined up
    // with the AR world's axes, so a station turned at an angle gets a box up to 40% wider than it really is.
    // Hidden renderers and particles (flames, the pour stream) don't count.
    private static bool TryMeasureFootprint(GameObject obj, out Rect footprint)
    {
        footprint = default;
        Transform frame = obj.transform.parent;
        float minX = float.PositiveInfinity, minZ = float.PositiveInfinity;
        float maxX = float.NegativeInfinity, maxZ = float.NegativeInfinity;
        foreach (var r in obj.GetComponentsInChildren<Renderer>())
        {
            if (!r.enabled || r is ParticleSystemRenderer || r is TrailRenderer || r is LineRenderer) continue;
            Bounds b = r.localBounds;
            Matrix4x4 toFrame = frame.worldToLocalMatrix * r.transform.localToWorldMatrix;
            for (int i = 0; i < 8; i++)
            {
                Vector3 corner = b.center + Vector3.Scale(b.extents,
                    new Vector3((i & 1) == 0 ? -1f : 1f, (i & 2) == 0 ? -1f : 1f, (i & 4) == 0 ? -1f : 1f));
                Vector3 p = toFrame.MultiplyPoint3x4(corner);
                minX = Mathf.Min(minX, p.x); maxX = Mathf.Max(maxX, p.x);
                minZ = Mathf.Min(minZ, p.z); maxZ = Mathf.Max(maxZ, p.z);
            }
        }
        if (float.IsPositiveInfinity(minX)) return false;
        footprint = Rect.MinMaxRect(minX, minZ, maxX, maxZ);
        return true;
    }

    // Two 4-cornered outlines overlap unless some edge of one has the other entirely on its far side.
    private static bool Intersects(Vector2[] a, Vector2[] b) => !Separated(a, b) && !Separated(b, a);

    private static bool Separated(Vector2[] a, Vector2[] b)
    {
        for (int i = 0; i < a.Length; i++)
        {
            Vector2 edge = a[(i + 1) % a.Length] - a[i];
            Vector2 axis = new Vector2(-edge.y, edge.x);
            float minA = float.PositiveInfinity, maxA = float.NegativeInfinity;
            float minB = float.PositiveInfinity, maxB = float.NegativeInfinity;
            foreach (var p in a) { float d = Vector2.Dot(p, axis); minA = Mathf.Min(minA, d); maxA = Mathf.Max(maxA, d); }
            foreach (var p in b) { float d = Vector2.Dot(p, axis); minB = Mathf.Min(minB, d); maxB = Mathf.Max(maxB, d); }
            if (maxA < minB || maxB < minA) return true;
        }
        return false;
    }

    private bool AllAnchored()
    {
        foreach (var pair in imagePrefabs)
            if (!spawned.TryGetValue(pair.imageName, out var s) || !s.anchored) return false;
        return true;
    }
}
