using UnityEngine;
using UnityEngine.XR.ARFoundation;

// While its Grabbable is held, turns the object about the vertical so one side faces the camera (e.g. the pot with
// its handles left and right). Only the vertical axis turns, so the object stays upright. Put it next to Grabbable.
// Either end of Facing Axis may face the camera, whichever needs the smaller turn (fine for symmetric things like the pot).
[RequireComponent(typeof(Grabbable))]
public class FaceCameraWhileHeld : MonoBehaviour
{
    [Tooltip("The side that should face you, in the object's own space. Default = its forward (blue Z) axis. " +
             "Ignored on the pot: it uses PotPour's Pour Axis, so it carries the same way round as it pours.")]
    [SerializeField] private Vector3 facingAxis = Vector3.forward;
    [Tooltip("How fast it turns to face you, in degrees per second.")]
    [SerializeField] private float turnSpeed = 360f;

    private Grabbable grabbable;
    private PotPour pour; // optional: don't fight the pour's own tilt
    private Camera arCamera;

    void Awake()
    {
        grabbable = GetComponent<Grabbable>();
        pour = GetComponent<PotPour>();
    }

    void Start()
    {
        // Prefab spawned from a QR marker can't reference the scene camera, so find the AR one (as ChopProgressBar does).
        var camManager = FindAnyObjectByType<ARCameraManager>();
        if (camManager != null) arCamera = camManager.GetComponent<Camera>();
    }

    void Update()
    {
        if (!grabbable.isHeld || arCamera == null || (pour != null && pour.IsPouring)) return;

        Vector3 toCamera = Vector3.ProjectOnPlane(arCamera.transform.position - transform.position, Vector3.up);
        Vector3 local = pour != null ? pour.PourAxis : facingAxis;
        Vector3 axis = Vector3.ProjectOnPlane(transform.TransformDirection(local), Vector3.up);
        if (toCamera.sqrMagnitude < 1e-4f || axis.sqrMagnitude < 1e-6f) return;
        if (Vector3.Dot(axis, toCamera) < 0f) axis = -axis;

        float angle = Vector3.SignedAngle(axis, toCamera, Vector3.up);
        float step = Mathf.Clamp(angle, -turnSpeed * Time.deltaTime, turnSpeed * Time.deltaTime);
        transform.rotation = Quaternion.AngleAxis(step, Vector3.up) * transform.rotation;
    }
}
