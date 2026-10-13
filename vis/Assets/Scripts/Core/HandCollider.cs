using System.Collections.Generic;
using UnityEngine;

// Gives the real hand a physical presence: finger points placed at the LiDAR hand depth push HandPushable objects,
// which then spring back to where they were (see HandPushable). Nothing uses Unity's physics simulation, so objects
// never drift out of grab range. Grab/proximity is unaffected and still 2D screen space.
// Put this on any scene object (e.g. next to GestureButtonController) and fill in the references.
public class HandCollider : MonoBehaviour
{
    [SerializeField] private Camera arCamera;
    [SerializeField] private HandTrackingManager handTrackingManager;
    [SerializeField] private HandDepthEstimator handDepth;
    [Tooltip("Collisions are switched off while something is held, so a held knife can't shove the carrot away mid-chop.")]
    [SerializeField] private GestureButtonController gestureController;

    [Tooltip("Master switch, e.g. to compare performance with collisions off.")]
    [SerializeField] private bool collisionsEnabled = true;

    [Tooltip("MediaPipe landmarks that collide: the 5 fingertips plus the middle-finger knuckle (palm).")]
    [SerializeField] private int[] collidingLandmarks = { 4, 8, 12, 16, 20, 9 };
    [Tooltip("Radius of each finger point, in meters.")]
    [SerializeField] private float fingerRadius = 0.015f;
    [Tooltip("LiDAR sees the front of the hand. Each point extends this far behind that (meters), since a hand has thickness and the depth reading is noisy.")]
    [SerializeField] private float handThickness = 0.04f;

    [Header("Debug")]
    [Tooltip("Small spheres at each colliding point, so you can see where the game thinks your hand is in 3D.")]
    [SerializeField] private bool showHandPoints = false;
    [SerializeField] private float debugPointSize = 0.012f;

    private readonly List<Vector2> landmarks = new List<Vector2>();
    private readonly List<Vector3> handPoints = new List<Vector3>(); // world-space front of each point
    private CapsuleCollider probe; // shape used for the overlap tests; never simulated
    private readonly List<Transform> debugPoints = new List<Transform>();

    void Awake()
    {
        // ComputePenetration needs a real Collider for the hand's shape. Its own transform is ignored (the pose is
        // passed in per test), so park it far away as a trigger where it can't touch anything.
        var go = new GameObject("Hand Collision Probe");
        go.transform.SetParent(transform, false);
        go.transform.position = new Vector3(0f, -10000f, 0f);
        probe = go.AddComponent<CapsuleCollider>();
        probe.isTrigger = true;
        probe.direction = 2; // along local Z = the camera's forward axis
    }

    void LateUpdate()
    {
        // LateUpdate: runs after grab/drop/board placement moved things this frame, so HandPushable sees final positions.
        bool active = collisionsEnabled && ComputeHandPoints();
        UpdateDebugPoints(active);

        float dt = Time.deltaTime;
        var pushables = HandPushable.All;
        for (int i = pushables.Count - 1; i >= 0; i--)
        {
            var p = pushables[i];
            p.Step(active ? GetPush(p) : Vector3.zero, dt);
        }
    }

    private bool ComputeHandPoints()
    {
        handPoints.Clear();
        if (gestureController != null && gestureController.CurrentlyHeldType != null) return false;
        if (handDepth == null || !handDepth.HasDepth) return false; // no real depth, no collisions (never guess)
        if (!handTrackingManager.TryGetPrimaryHandLandmarks(landmarks)) return false;

        float depth = handDepth.Depth;
        foreach (int i in collidingLandmarks)
        {
            if (i < 0 || i >= landmarks.Count) continue;
            // Same normalized -> pixel mapping as HandTrackingManager's fingertip (MediaPipe is y-down).
            var screen = new Vector3(landmarks[i].x * arCamera.pixelWidth, (1f - landmarks[i].y) * arCamera.pixelHeight, depth);
            handPoints.Add(arCamera.ScreenToWorldPoint(screen));
        }
        return handPoints.Count > 0;
    }

    // Sum of how far every hand point has sunk into this object's colliders, as the world-space move that separates them.
    private Vector3 GetPush(HandPushable pushable)
    {
        Vector3 total = Vector3.zero;
        Quaternion probeRot = arCamera.transform.rotation;
        Vector3 back = arCamera.transform.forward * (handThickness * 0.5f);

        probe.radius = fingerRadius;
        probe.height = handThickness + 2f * fingerRadius;

        foreach (var col in pushable.Colliders)
        {
            if (col == null || !col.enabled) continue;
            Bounds b = col.bounds;
            b.Expand(handThickness + 2f * fingerRadius);
            foreach (var front in handPoints)
            {
                if (!b.Contains(front)) continue; // cheap reject before the exact test
                Vector3 center = front + back;
                if (Physics.ComputePenetration(col, col.transform.position, col.transform.rotation,
                                               probe, center, probeRot, out Vector3 dir, out float dist))
                    total += dir * dist;
            }
        }
        return total;
    }

    private void UpdateDebugPoints(bool active)
    {
        if (!showHandPoints)
        {
            foreach (var t in debugPoints) if (t != null) t.gameObject.SetActive(false);
            return;
        }
        while (debugPoints.Count < handPoints.Count)
        {
            var s = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            Destroy(s.GetComponent<Collider>()); // visual only
            s.name = "Hand Point (debug)";
            s.transform.localScale = Vector3.one * debugPointSize;
            debugPoints.Add(s.transform);
        }
        for (int i = 0; i < debugPoints.Count; i++)
        {
            bool show = active && i < handPoints.Count;
            debugPoints[i].gameObject.SetActive(show);
            if (show) debugPoints[i].position = handPoints[i];
        }
    }
}
