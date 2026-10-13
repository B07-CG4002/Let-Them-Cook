using UnityEngine;
using UnityEngine.Events;

public class HandInteractable : MonoBehaviour
{
    [Tooltip("Screen-space pixel radius, not meters - HandTrackingManager compares screen positions " +
        "since this project uses image tracking (no plane geometry to raycast a world position against).")]
    [SerializeField] private float radius = 150f;
    public UnityEvent onHandEnter;
    public UnityEvent onHandExit;

    private bool isTouched;
    private float closestHandDistance = float.PositiveInfinity;

    public float Radius => radius;
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
