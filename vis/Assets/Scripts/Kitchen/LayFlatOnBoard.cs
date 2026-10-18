using UnityEngine;

// Add to long ingredients (Carrot, cucumber...), not round ones (tomato). When dropped onto a chopping board,
// the ingredient is turned so its long side lies flat on the board, along the direction the board picks
// (ChoppingBoard's Lay Flat Along setting). The knife sits at a fixed angle to the board in the same prefab, so
// that direction also runs across the blade and each chop cuts a round. ChoppingBoard.TryPlace calls this.
// Chopped pieces are new objects without this component, so a dropped bunch keeps its rotation.
public class LayFlatOnBoard : MonoBehaviour
{
    public enum Axis { Auto, X, Y, Z }

    [Tooltip("The ingredient's long axis, in its mesh's own axes. Auto = the mesh's longest side.")]
    [SerializeField] private Axis longAxis = Axis.Auto;

    // Rotates the item about `pivot` so its long axis points along `direction` (a direction on the board's surface).
    public void Apply(Vector3 direction, Vector3 pivot)
    {
        var mf = GetComponentInChildren<MeshFilter>();
        if (mf == null || mf.sharedMesh == null || direction.sqrMagnitude < 1e-6f) return;

        Vector3 along = mf.transform.TransformDirection(LocalLongAxis(mf)).normalized;
        Vector3 target = direction.normalized;
        if (Vector3.Dot(target, along) < 0f) target = -target; // smallest turn, so the tip stays roughly where it was

        Quaternion turn = Quaternion.FromToRotation(along, target);
        transform.position = pivot + turn * (transform.position - pivot);
        transform.rotation = turn * transform.rotation;
    }

    private Vector3 LocalLongAxis(MeshFilter mf)
    {
        if (longAxis == Axis.X) return Vector3.right;
        if (longAxis == Axis.Y) return Vector3.up;
        if (longAxis == Axis.Z) return Vector3.forward;

        Vector3 size = Vector3.Scale(mf.sharedMesh.bounds.size, mf.transform.lossyScale);
        size = new Vector3(Mathf.Abs(size.x), Mathf.Abs(size.y), Mathf.Abs(size.z));
        if (size.x >= size.y && size.x >= size.z) return Vector3.right;
        return size.y >= size.z ? Vector3.up : Vector3.forward;
    }
}
