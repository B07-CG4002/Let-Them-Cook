using System.Collections.Generic;
using UnityEngine;

// Fake fall: a dropped item that didn't land on a board, pot or station speeds up downward like gravity and stops on
// the table, i.e. at the height of the nearest anchored QR marker. No rigidbody, no rolling; it only runs while falling
// and then removes itself. Grabbing the item mid-fall cancels it.
public class FallToSurface : MonoBehaviour
{
    private float targetY;
    private float gravity;
    private float bounce;
    private float velocity;

    // Starts a fall for `item`, or does nothing if no anchored marker is within `maxReach` (m, sideways): then the item
    // stays where it was let go, as before. Returns true if it will fall.
    public static bool Begin(Transform item, IReadOnlyList<Transform> surfaces, float maxReach, float gravity, float bounce)
    {
        Transform nearest = null;
        float best = maxReach * maxReach;
        Vector3 p = item.position;
        foreach (var s in surfaces)
        {
            if (s == null) continue;
            Vector3 d = s.position - p;
            float flat = d.x * d.x + d.z * d.z;
            if (flat <= best) { best = flat; nearest = s; }
        }
        if (nearest == null || !TryGetBottom(item, out float bottom)) return false;

        // Pivot height at which the item's lowest point touches the marker's surface.
        float target = nearest.position.y + (p.y - bottom);
        if (p.y <= target) return false; // already at or below the table

        var fall = item.GetComponent<FallToSurface>();
        if (fall == null) fall = item.gameObject.AddComponent<FallToSurface>();
        fall.targetY = target;
        fall.gravity = gravity;
        fall.bounce = bounce;
        fall.velocity = 0f;
        return true;
    }

    // Stops a fall in progress (the item was grabbed).
    public static void Cancel(Transform item)
    {
        var fall = item.GetComponent<FallToSurface>();
        if (fall != null) Destroy(fall);
    }

    void Update()
    {
        velocity -= gravity * Time.deltaTime;
        Vector3 pos = transform.position;
        pos.y += velocity * Time.deltaTime;

        if (pos.y <= targetY)
        {
            pos.y = targetY;
            velocity = -velocity * bounce; // small hop back up
            if (velocity < 0.05f)          // too small to see: landed
            {
                transform.position = pos;
                Destroy(this);
                return;
            }
        }
        transform.position = pos;
    }

    // Lowest point of the item's visible meshes (particles and hidden renderers, e.g. the pot's fire and empty
    // soup surface, don't count).
    private static bool TryGetBottom(Transform item, out float bottom)
    {
        bottom = float.PositiveInfinity;
        foreach (var r in item.GetComponentsInChildren<Renderer>())
        {
            if (!r.enabled || r is ParticleSystemRenderer) continue;
            bottom = Mathf.Min(bottom, r.bounds.min.y);
        }
        return !float.IsPositiveInfinity(bottom);
    }
}
