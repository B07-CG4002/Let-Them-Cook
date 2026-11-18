using UnityEngine;
using UnityEngine.Events;

public class HandInteractable : MonoBehaviour
{
    [Tooltip("Not used on buttons: a button (any UI object, i.e. one with a RectTransform) is touched anywhere over its own " +
        "rectangle instead. Screen-space pixel radius, not meters - HandTrackingManager compares screen positions " +
        "since this project uses image tracking (no plane geometry to raycast a world position against).")]
    [SerializeField] private float radius = 150f;
    [Tooltip("Where the hand has to be to touch this, e.g. an empty child on the knife handle. Empty = this object's pivot. " +
        "A grabbed item is held by this point.")]
    [SerializeField] private Transform reachPoint;
    // Fire when the first hand arrives and when the last one leaves (hover feedback doesn't care which hand).
    public UnityEvent onHandEnter;
    public UnityEvent onHandExit;

    private bool isTouched;
    private readonly float[] handDistance = { float.PositiveInfinity, float.PositiveInfinity }; // indexed by Hand
    private static readonly Vector3[] corners = new Vector3[4];

    public float Radius => radius;
    public Vector3 ReachPosition => reachPoint != null ? reachPoint.position : transform.position;
    public float ClosestHandDistance => Mathf.Min(handDistance[0], handDistance[1]);
    public bool IsTouched => isTouched; // by either hand

    public float DistanceTo(Hand hand) => handDistance[(int)hand];
    public bool IsTouchedBy(Hand hand) => handDistance[(int)hand] <= TouchRadius;

    // On a button the distance is to the button's edge (0 = over it), so it's touched only when over the button.
    public bool IsButton => transform is RectTransform; // any UI object (menu/end-card buttons); kitchen items never are
    private float TouchRadius => IsButton ? 0f : radius;

    // The button's rectangle in screen pixels (same space as WorldToScreenPoint). False = behind the camera.
    public bool TryGetScreenRect(Camera cam, out Rect rect)
    {
        rect = default;
        if (!(transform is RectTransform rt)) return false;
        rt.GetWorldCorners(corners);
        Vector2 min = new Vector2(float.PositiveInfinity, float.PositiveInfinity), max = -min;
        foreach (var corner in corners)
        {
            Vector3 sp = cam.WorldToScreenPoint(corner);
            if (sp.z <= 0f) return false;
            min = Vector2.Min(min, sp);
            max = Vector2.Max(max, sp);
        }
        rect = Rect.MinMaxRect(min.x, min.y, max.x, max.y);
        return true;
    }

    // Screen-pixel distance from each hand's nearest palm point; infinity = that hand isn't tracked.
    public void SetHandDistances(float left, float right)
    {
        handDistance[(int)Hand.Left] = left;
        handDistance[(int)Hand.Right] = right;

        bool touched = ClosestHandDistance <= TouchRadius;
        if (touched == isTouched) return;
        isTouched = touched;
        if (touched) onHandEnter?.Invoke();
        else onHandExit?.Invoke();
    }
}
