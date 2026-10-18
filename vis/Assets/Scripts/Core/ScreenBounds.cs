using UnityEngine;

// Screen-space hit tests shared by the pot (ingredient drops) and pot stations (pot drops). Only called on a drop.
public static class ScreenBounds
{
    // True if `worldPos` appears on screen inside `root`'s outline (its meshes' bounds, projected).
    // Renderers under `exclude` don't count (a carried pot that is still a child of the stove's prefab).
    public static bool Covers(Transform root, Vector3 worldPos, Camera cam, Transform exclude = null)
    {
        Vector3 p = cam.WorldToScreenPoint(worldPos);
        if (p.z <= 0f) return false;

        bool any = false;
        Bounds b = default;
        foreach (var r in root.GetComponentsInChildren<Renderer>())
        {
            if (exclude != null && r.transform.IsChildOf(exclude)) continue;
            if (!any) { b = r.bounds; any = true; }
            else b.Encapsulate(r.bounds);
        }
        if (!any) return false;

        Vector2 min = new Vector2(float.PositiveInfinity, float.PositiveInfinity), max = -min;
        for (int i = 0; i < 8; i++)
        {
            Vector3 corner = b.center + Vector3.Scale(b.extents, new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
            Vector3 s = cam.WorldToScreenPoint(corner);
            if (s.z <= 0f) return false; // partly behind the camera
            min = Vector2.Min(min, s);
            max = Vector2.Max(max, s);
        }
        return p.x >= min.x && p.x <= max.x && p.y >= min.y && p.y <= max.y;
    }
}
