using System.Collections.Generic;
using UnityEngine;

// An object the real hand can bump. It's pushed out of the hand, up to maxPush, then springs back to its home spot
// with a little wobble, so it never ends up out of grab range. Driven by HandCollider; nothing here runs on its own.
// Needs a collider on itself or its children (a BoxCollider is added automatically if there's none).
// Grab, drop-onto-board and QR tracking can move it freely: any move it didn't make itself becomes the new home.
public class HandPushable : MonoBehaviour
{
    // Registered on enable, so HandCollider never has to search the scene each frame.
    public static readonly List<HandPushable> All = new List<HandPushable>();

    [Tooltip("Furthest the hand can push it from home, in meters.")]
    [SerializeField] private float maxPush = 0.04f;
    [Tooltip("How strongly it's pulled back home. Higher = snappier.")]
    [SerializeField] private float springStiffness = 60f;
    [Tooltip("How quickly the bounce dies out. Lower = more wobble.")]
    [SerializeField] private float springDamping = 8f;
    [Tooltip("Only push sideways (along the table), so a hand from above doesn't sink it into the board.")]
    [SerializeField] private bool horizontalOnly = true;
    [Tooltip("Tilt at full push, in degrees, for the wobble.")]
    [SerializeField] private float maxTilt = 15f;

    public Collider[] Colliders { get; private set; }

    private Vector3 homeLocalPos;
    private Quaternion homeLocalRot;
    private Vector3 offset;          // world-space displacement from home
    private Vector3 velocity;
    private Quaternion tilt = Quaternion.identity; // world-space wobble applied on top of home
    private Vector3 lastLocalPos;
    private Quaternion lastLocalRot;
    private bool initialized;

    void OnEnable() { All.Add(this); RefreshColliders(); }
    void OnDisable() { All.Remove(this); }

    // Call if colliders are added or removed at runtime.
    public void RefreshColliders()
    {
        Colliders = GetComponentsInChildren<Collider>();
        if (Colliders.Length == 0 && GetComponent<MeshFilter>() != null)
            Colliders = new Collider[] { gameObject.AddComponent<BoxCollider>() }; // auto-fits the mesh
        if (Colliders.Length == 0)
            Debug.LogWarning($"[HandPushable] {name} has no collider, so the hand can't touch it.");
    }

    public void Step(Vector3 push, float dt)
    {
        if (!initialized || MovedByOthers()) Rebase();

        if (horizontalOnly) push.y = 0f;

        if (push.sqrMagnitude > 1e-10f)
        {
            // Touching: sits against the hand (pushed out of it), up to maxPush.
            offset = Vector3.ClampMagnitude(offset + push, maxPush);
            velocity = Vector3.zero;
        }
        else if (offset.sqrMagnitude > 1e-10f || velocity.sqrMagnitude > 1e-10f)
        {
            // Free: damped spring back home. Small substeps keep it stable if a frame is slow.
            int steps = Mathf.Clamp(Mathf.CeilToInt(dt / 0.01f), 1, 8);
            float h = dt / steps;
            for (int s = 0; s < steps; s++)
            {
                velocity += (-springStiffness * offset - springDamping * velocity) * h;
                offset += velocity * h;
            }
            if (offset.sqrMagnitude < 1e-8f && velocity.sqrMagnitude < 1e-6f) { offset = Vector3.zero; velocity = Vector3.zero; }
        }
        else return; // at rest, nothing to write

        Apply();
    }

    private bool MovedByOthers()
    {
        // Local space, so QR marker tracking (which moves the parent) doesn't count as a move.
        return (transform.localPosition - lastLocalPos).sqrMagnitude > 1e-8f
            || Quaternion.Angle(transform.localRotation, lastLocalRot) > 0.01f;
    }

    // Home = wherever it is now (minus our own wobble). Covers the first frame and any move made by grab/drop/board snap.
    private void Rebase()
    {
        homeLocalPos = transform.localPosition;
        homeLocalRot = LocalRotation(Quaternion.Inverse(tilt) * transform.rotation);
        if (tilt != Quaternion.identity) transform.rotation = Quaternion.Inverse(tilt) * transform.rotation; // don't carry the lean into the hand
        offset = Vector3.zero;
        velocity = Vector3.zero;
        tilt = Quaternion.identity;
        initialized = true;
        Remember();
    }

    private void Apply()
    {
        Transform parent = transform.parent;
        Vector3 homeWorld = parent != null ? parent.TransformPoint(homeLocalPos) : homeLocalPos;
        Quaternion homeWorldRot = parent != null ? parent.rotation * homeLocalRot : homeLocalRot;

        // Lean away from the push, more the further it's pushed.
        Vector3 axis = Vector3.Cross(Vector3.up, offset);
        float angle = maxPush > 0f ? maxTilt * Mathf.Clamp01(offset.magnitude / maxPush) : 0f;
        tilt = axis.sqrMagnitude > 1e-10f ? Quaternion.AngleAxis(angle, axis.normalized) : Quaternion.identity;

        transform.SetPositionAndRotation(homeWorld + offset, tilt * homeWorldRot);
        Remember();
    }

    private void Remember()
    {
        lastLocalPos = transform.localPosition;
        lastLocalRot = transform.localRotation;
    }

    private Quaternion LocalRotation(Quaternion world) => transform.parent != null ? Quaternion.Inverse(transform.parent.rotation) * world : world;
}
