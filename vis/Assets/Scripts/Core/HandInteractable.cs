using UnityEngine;
using UnityEngine.Events;

public class HandInteractable : MonoBehaviour
{
    [Tooltip("Screen-space pixel radius, not meters - HandTrackingManager compares screen positions " +
        "since this project uses image tracking (no plane geometry to raycast a world position against).")]
    [SerializeField] private float radius = 150f;
    [Tooltip("Where the hand has to be to touch this, e.g. an empty child on the knife handle. Empty = this object's pivot. " +
        "A grabbed item is held by this point.")]
    [SerializeField] private Transform reachPoint;
    public UnityEvent onHandEnter;
    public UnityEvent onHandExit;

    private bool isTouched;
    private float closestHandDistance = float.PositiveInfinity;

    public float Radius => radius;
    public Vector3 ReachPosition => reachPoint != null ? reachPoint.position : transform.position;
    public float ClosestHandDistance => closestHandDistance;
    public bool IsTouched => isTouched;

    public void SetTouched(bool touched)
    {
        if (touched == isTouched) return;
        isTouched = touched;
        if (touched) onHandEnter?.Invoke();
        else onHandExit?.Invoke();
    }

    public void SetClosestHandDistance(float distance)
    {
        closestHandDistance = distance;
    }
}
