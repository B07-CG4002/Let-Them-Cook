using UnityEngine;

// Marks a chopping board and settles dropped ingredients flush onto its top surface.
// The top surface is read from the board's mesh, so no hand-tuned heights are needed.
// Holds one ingredient at a time: a whole ingredient, its pieces while it's being chopped, or a chopped bunch.
public class ChoppingBoard : MonoBehaviour
{
    [Tooltip("Mesh whose top face is the cutting surface. Empty = this object's MeshFilter, else the first one in its children.")]
    [SerializeField] private MeshFilter surfaceMesh;
    [Tooltip("How far inside the board's edge (m) the landing point must be. Negative lets items land slightly past the edge.")]
    [SerializeField] private float edgeMargin = 0.01f;
    [Tooltip("Gap left between the item's bottom and the board (m). A tiny gap avoids flicker where the meshes meet.")]
    [SerializeField] private float restGap = 0.0005f;

    public enum LayFlatDirection { LongSide, ShortSide }
    [Tooltip("Which way long ingredients (with Lay Flat On Board) point once dropped: along the board's long side or its " +
        "short side. Pick the one that runs across the knife's blade; if chops cut strips, switch it.")]
    [SerializeField] private LayFlatDirection layFlatAlong = LayFlatDirection.LongSide;

    void Awake()
    {
        if (surfaceMesh == null) surfaceMesh = GetComponent<MeshFilter>();
        if (surfaceMesh == null) surfaceMesh = GetComponentInChildren<MeshFilter>();
    }

    // Works for a single ingredient or a chopped bunch (pieces as children): all child renderers/meshes count.
    // Looks from `viewerPos` through the item's visual center (i.e. where it appears on screen) to the board's top plane.
    // If that lands on a free board, moves the item there so its lowest vertex touches the top and returns Placed.
    // Over this board but it already holds an ingredient: returns Occupied and leaves the item untouched.
    // An item with LayFlatOnBoard is also turned to lie flat along the board's chosen side before it's settled.
    // Uses no depth: the held item's distance from the camera is the fixed-depth approximation, so only its
    // on-screen position is trusted.
    public enum PlaceResult { Missed, Placed, Occupied }

    public PlaceResult Place(Transform item, Vector3 viewerPos)
    {
        if (!TryGetBounds(item, out Bounds itemBounds) || !TryGetSurface(out Vector3 top, out Vector3 up)) return PlaceResult.Missed;

        Vector3 center = itemBounds.center;
        var ray = new Ray(viewerPos, center - viewerPos);
        if (!new UnityEngine.Plane(up, top).Raycast(ray, out float enter)) return PlaceResult.Missed;

        Vector3 landing = ray.GetPoint(enter);
        if (!IsOverSurface(landing)) return PlaceResult.Missed;
        if (IsOccupied()) return PlaceResult.Occupied;

        // Long ingredients: turn it to lie flat along the board first, then measure it again in its new pose.
        var layFlat = item.GetComponent<LayFlatOnBoard>();
        if (layFlat != null)
        {
            layFlat.Apply(Vector3.ProjectOnPlane(BoardSideDirection(), up), center);
            TryGetBounds(item, out itemBounds);
            center = itemBounds.center;
        }

        float bottom = LowestPointAlong(item, itemBounds, up); // measured before moving; the sideways move doesn't change it
        item.position += Vector3.ProjectOnPlane(landing - center, up);   // slide over the landing point
        item.position += up * (Vector3.Dot(top, up) + restGap - bottom); // then lower/raise until flush
        return PlaceResult.Placed;
    }

    // True while any ingredient or piece is resting on this board. Grabbing an item clears its Board, so a
    // held item never counts. Only called on a drop, never per frame.
    public bool IsOccupied()
    {
        foreach (var state in FindObjectsByType<IngredientState>(FindObjectsInactive.Exclude))
            if (state.OnChoppingBoard && state.Board == this) return true;
        return false;
    }

    // World-space box around the item and all its children (a chopped bunch has no renderer on its root).
    private static bool TryGetBounds(Transform item, out Bounds bounds)
    {
        bounds = default;
        bool any = false;
        foreach (var r in item.GetComponentsInChildren<Renderer>())
        {
            if (!any) { bounds = r.bounds; any = true; }
            else bounds.Encapsulate(r.bounds);
        }
        return any;
    }

    // Top-center point of the cutting surface and its upward normal, in world space.
    private bool TryGetSurface(out Vector3 top, out Vector3 up)
    {
        top = up = Vector3.zero;
        if (surfaceMesh == null || surfaceMesh.sharedMesh == null) return false;

        Transform t = surfaceMesh.transform;
        Bounds lb = surfaceMesh.sharedMesh.bounds;
        int axis = UpAxis(t, out float sign);
        Vector3 localTop = lb.center;
        localTop[axis] = sign > 0 ? lb.max[axis] : lb.min[axis];
        top = t.TransformPoint(localTop);
        up = (t.TransformDirection(LocalAxis(axis)) * sign).normalized;
        return true;
    }

    // World direction of the board's long or short side (layFlatAlong), from the board mesh's own axes.
    private Vector3 BoardSideDirection()
    {
        if (surfaceMesh == null || surfaceMesh.sharedMesh == null)
            return Vector3.zero;

        Transform t = surfaceMesh.transform;
        Vector3 size = Vector3.Scale(surfaceMesh.sharedMesh.bounds.size, t.lossyScale);
        int upAxis = UpAxis(t, out _);
        int a = upAxis == 0 ? 1 : 0, b = upAxis == 2 ? 1 : 2; // the two axes across the surface
        bool aLonger = Mathf.Abs(size[a]) >= Mathf.Abs(size[b]);
        int pick = (layFlatAlong == LayFlatDirection.LongSide) == aLonger ? a : b;
        return t.TransformDirection(LocalAxis(pick));
    }

    private bool IsOverSurface(Vector3 worldPoint)
    {
        if (surfaceMesh == null || surfaceMesh.sharedMesh == null) return false;

        Transform t = surfaceMesh.transform;
        Bounds lb = surfaceMesh.sharedMesh.bounds;
        Vector3 local = t.InverseTransformPoint(worldPoint);
        int upAxis = UpAxis(t, out _);
        for (int i = 0; i < 3; i++)
        {
            if (i == upAxis) continue;
            float margin = edgeMargin / Mathf.Max(Mathf.Abs(t.lossyScale[i]), 1e-6f); // meters -> mesh units
            if (local[i] < lb.min[i] + margin || local[i] > lb.max[i] - margin) return false;
        }
        return true;
    }

    // The board mesh's local axis that points most upward in the world is its thickness axis.
    // Handles models imported Z-up (e.g. from Blender) without extra setup. ARKit's world Y is gravity-aligned.
    private static int UpAxis(Transform t, out float sign)
    {
        int axis = 1; sign = 1f; float best = -1f;
        for (int i = 0; i < 3; i++)
        {
            float d = Vector3.Dot(t.TransformDirection(LocalAxis(i)).normalized, Vector3.up);
            if (Mathf.Abs(d) > best) { best = Mathf.Abs(d); axis = i; sign = Mathf.Sign(d); }
        }
        return axis;
    }

    private static Vector3 LocalAxis(int i) => i == 0 ? Vector3.right : i == 1 ? Vector3.up : Vector3.forward;

    // Lowest point of the item's actual meshes (its own and its children's) along `up`. Carrot meshes and
    // EzySlice pieces are Read/Write enabled; if any mesh isn't, falls back to the box, which can leave a small
    // gap if the item is rotated.
    private static float LowestPointAlong(Transform item, Bounds b, Vector3 up)
    {
        float min = float.PositiveInfinity;
        bool allReadable = true;
        foreach (var mf in item.GetComponentsInChildren<MeshFilter>())
        {
            if (mf.sharedMesh == null) continue;
            if (!mf.sharedMesh.isReadable) { allReadable = false; break; }
            Matrix4x4 m = mf.transform.localToWorldMatrix;
            foreach (var v in mf.sharedMesh.vertices)
                min = Mathf.Min(min, Vector3.Dot(m.MultiplyPoint3x4(v), up));
        }
        if (allReadable && !float.IsPositiveInfinity(min)) return min;

        min = float.PositiveInfinity;
        for (int i = 0; i < 8; i++)
        {
            Vector3 corner = b.center + Vector3.Scale(b.extents,
                new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
            min = Mathf.Min(min, Vector3.Dot(corner, up));
        }
        return min;
    }
}
