using System;
using UnityEngine;

// A thrown item flying in a small arc from where it was to its landing spot. No rigidbody, like FallToSurface: it
// only runs while flying and then removes itself, calling `onLanded`. Grabbing the item mid-air cancels it.
// With a hit radius above 0 it sweeps a small sphere along each frame's step, and stops at the first collider in its
// way (calling `onBlocked`) instead of passing through. Ignored: the item itself, anything under `ignoreRoots` (the
// target station, and the stove/sink a target pot stands on), items in a hand, and triggers.
public class ThrowFlight : MonoBehaviour
{
    private Vector3 from, to;
    private float duration, arcHeight, t;
    private float hitRadius;
    private Transform[] ignoreRoots;
    private Action<Transform> onLanded;
    private Action<Transform, RaycastHit> onBlocked;
    private readonly RaycastHit[] hits = new RaycastHit[8];

    public static void Begin(Transform item, Vector3 to, float duration, float arcHeight, float hitRadius,
        Transform[] ignoreRoots, Action<Transform> onLanded, Action<Transform, RaycastHit> onBlocked)
    {
        var flight = item.GetComponent<ThrowFlight>();
        if (flight == null) flight = item.gameObject.AddComponent<ThrowFlight>();
        flight.from = item.position;
        flight.to = to;
        flight.duration = Mathf.Max(duration, 0.01f);
        flight.arcHeight = arcHeight;
        flight.hitRadius = hitRadius;
        flight.ignoreRoots = ignoreRoots;
        flight.t = 0f;
        flight.onLanded = onLanded;
        flight.onBlocked = onBlocked;
    }

    // Stops a flight in progress (the item was caught); it doesn't land.
    public static void Cancel(Transform item)
    {
        var flight = item.GetComponent<ThrowFlight>();
        if (flight != null) Destroy(flight);
    }

    void Update()
    {
        t += Time.deltaTime / duration;
        float k = Mathf.Clamp01(t);
        // Straight line plus a hump that is highest halfway.
        Vector3 next = Vector3.Lerp(from, to, k) + Vector3.up * (arcHeight * 4f * k * (1f - k));

        if (hitRadius > 0f && TryHit(transform.position, next, out RaycastHit hit))
        {
            Vector3 step = next - transform.position;
            transform.position += step.normalized * hit.distance; // stops just touching the thing it hit
            var blocked = onBlocked;
            Destroy(this);
            blocked?.Invoke(transform, hit);
            return;
        }

        transform.position = next;
        if (k < 1f) return;

        var landed = onLanded;
        Destroy(this);
        landed?.Invoke(transform);
    }

    // Nearest collider the sphere meets going from `a` to `b` that isn't ignored.
    private bool TryHit(Vector3 a, Vector3 b, out RaycastHit nearest)
    {
        nearest = default;
        Vector3 step = b - a;
        float length = step.magnitude;
        if (length < 1e-5f) return false;

        int count = Physics.SphereCastNonAlloc(a, hitRadius, step / length, hits, length, ~0, QueryTriggerInteraction.Ignore);
        bool found = false;
        for (int i = 0; i < count; i++)
        {
            var h = hits[i];
            if (h.distance <= 0f) continue; // already touching at the start (e.g. thrown from inside a crate)
            Transform other = h.collider.transform;
            if (other.IsChildOf(transform)) continue;
            if (IsIgnored(other)) continue;
            var g = h.collider.GetComponentInParent<Grabbable>();
            if (g != null && g.isHeld) continue;
            if (!found || h.distance < nearest.distance) { nearest = h; found = true; }
        }
        return found;
    }

    private bool IsIgnored(Transform other)
    {
        if (ignoreRoots == null) return false;
        foreach (var root in ignoreRoots)
            if (root != null && other.IsChildOf(root)) return true;
        return false;
    }
}
