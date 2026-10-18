using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;
using UnityEngine.XR.ARSubsystems;

// Spawns each marker's prefab the first time the marker is seen. It follows the marker for a moment while ARKit
// settles the marker's pose, then it is anchored in the world: from then on it stays put even if the marker moves,
// is covered or is lost. Grabbable children (knife, pot, ingredients) can still be picked up and moved as before.
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

    private class Spawned
    {
        public GameObject obj;
        public bool anchored;
        public Vector3 stillPos;      // marker pose at the start of the current still stretch
        public Quaternion stillRot;
        public float stillFor;        // seconds the marker has stayed near stillPos/stillRot
        public float trackedFor;      // seconds tracked in total, for maxSettleTime
    }

    private readonly Dictionary<string, Spawned> spawned = new Dictionary<string, Spawned>();
    private bool canAnchor;

    // Every anchored marker's pose. Dropped items fall to the nearest one's height (FallToSurface).
    public static readonly List<Transform> Anchors = new List<Transform>();

    void Awake()
    {
        Anchors.Clear(); // static, so a reloaded scene starts empty
        canAnchor = useArAnchor && FindAnyObjectByType<ARAnchorManager>() != null;
        if (useArAnchor && !canAnchor)
            Debug.LogWarning("[QR] No AR Anchor Manager in the scene; objects are anchored by position only.");
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

            if (s.stillFor >= settleTime || s.trackedFor >= maxSettleTime)
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

    private bool AllAnchored()
    {
        foreach (var pair in imagePrefabs)
            if (!spawned.TryGetValue(pair.imageName, out var s) || !s.anchored) return false;
        return true;
    }
}
