using System.Collections;
using UnityEngine;
using UnityEngine.Events;

// Pours a cooked pot onto a clean plate: the pot tilts toward the plate, a particle stream in the dish's colour
// arcs from the pot's lip to the plate, the pot's surface sinks while the food grows on the plate, then the pot
// tilts back and is emptied. Sits next to Pot on the pot object; GestureButtonController.OnPourPressed starts it.
// Held over the bin instead, the pot pours out whatever is in it (a wrong mix, burnt food) the same way, with no plate.
// This only reads Pot's state and calls Clear() at the end.
[RequireComponent(typeof(Pot))]
public class PotPour : MonoBehaviour
{
    [Header("Finding the plate (when the pot is held)")]
    [Tooltip("A clean plate (or the bin, for pouring out) counts if it appears within this fraction of the screen height of the pot's outline on screen.")]
    [SerializeField, Range(0.05f, 1f)] private float plateSearchRadius = 0.4f;

    [Header("Tilt")]
    [SerializeField] private float tiltAngle = 70f;
    [SerializeField] private float tiltTime = 0.25f;
    public enum PourDirection { TowardCamera, Sideways, TowardPlate }
    [Tooltip("Which way the pot tips. Toward Camera: toward you, so you see into the pot and the soup pours out at you. " +
             "Sideways: sideways as you see it, toward the plate's side, so the stream arcs in profile. " +
             "Toward Plate: straight at the plate, even if that's away from you. The stream lands on the plate either way.")]
    [SerializeField] private PourDirection pourDirection = PourDirection.TowardCamera;
    [Tooltip("The pot's pouring direction in its own space: the side between the handles. Default = its red X axis " +
             "(handles on Z, as on our pot); use (0,0,1) if the handles sit on X instead. The pot turns so it pours over this side, not a handle.")]
    [SerializeField] private Vector3 pourAxis = Vector3.right;

    [Header("Stream")]
    [Tooltip("Particle system for the stream (child of the pot, Play On Awake off, Simulation Space World, Shape Cone). " +
             "Empty = the child named PourStream. With none, the pour still happens, just without the stream.")]
    [SerializeField] private ParticleSystem stream;
    [Tooltip("Where the stream leaves the pot. Empty = worked out from the pot's size, on the rim facing the plate.")]
    [SerializeField] private Transform lip;
    [Tooltip("Seconds the stream pours for (the plate fills over this time).")]
    [SerializeField] private float pourTime = 1.2f;
    [Tooltip("Seconds a drop takes from the lip to the plate. The stream is aimed so it lands on the plate after this long.")]
    [SerializeField] private float fallTime = 0.3f;
    [Tooltip("1 = real gravity. Lower makes a straighter stream.")]
    [SerializeField] private float gravityScale = 1f;
    [Tooltip("On: the stream is aimed so it lands on the plate. Off: it just spills off the rim and falls, in front of " +
             "the pot (always visible, but it may not hit the plate exactly). The plate fills either way.")]
    [SerializeField] private bool aimAtPlate = false;
    [Tooltip("With Aim At Plate off: how fast the soup leaves the rim, outward (m/s). Higher = it arcs further out before falling.")]
    [SerializeField] private float spillSpeed = 0.3f;

    [Header("Pot surface")]
    [Tooltip("The pot's soup surface. Empty = the child named SoupSurface (the same one Pot uses).")]
    [SerializeField] private Transform soupSurface;
    [Tooltip("How far down the pot (0 = stays, 1 = to the bottom) the surface sinks while pouring.")]
    [SerializeField, Range(0f, 1f)] private float surfaceSink = 0.8f;

    [Header("Events (sounds)")]
    public UnityEvent onPourStart;
    public UnityEvent onPourEnd;

    public bool IsPouring { get; private set; }
    public Vector3 PourAxis => pourAxis; // FaceCameraWhileHeld turns this side toward you, so carrying and pouring agree

    private Pot pot;
    private Material surfaceMaterial;

    void Awake()
    {
        pot = GetComponent<Pot>();
        if (stream == null)
        {
            var t = FindChild(transform, "PourStream");
            if (t != null) stream = t.GetComponent<ParticleSystem>();
        }
        if (stream != null)
        {
            stream.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
            var main = stream.main;
            main.simulationSpace = ParticleSystemSimulationSpace.World; // drops keep falling where they left, not with the pot
        }
        else Debug.LogWarning($"[Pour] {name} has no PourStream particle system; pouring works without the stream.");

        if (soupSurface == null) soupSurface = FindChild(transform, "SoupSurface");
    }

    void Start()
    {
        // In Start: Pot makes its own copy of the surface material in Awake; this reads that same copy.
        var r = soupSurface != null ? soupSurface.GetComponent<Renderer>() : null;
        if (r != null) surfaceMaterial = r.material;
    }

    // The clean plate nearest the pot on screen, within plateSearchRadius of the pot's outline (not its centre, so a
    // big pot held close to the camera doesn't need the plate right under it). Only called on a Pour gesture.
    public Plate FindPlate(Camera cam)
    {
        if (!ScreenRect(cam, out Rect potRect)) return null;

        Plate best = null;
        float bestDist = plateSearchRadius * Screen.height;
        foreach (var plate in FindObjectsByType<Plate>(FindObjectsInactive.Exclude))
        {
            if (!plate.CanTakeFood) continue;
            Vector3 s = cam.WorldToScreenPoint(plate.PourPoint);
            if (s.z <= 0f) continue;
            Vector2 p = s;
            Vector2 nearest = new Vector2(Mathf.Clamp(p.x, potRect.xMin, potRect.xMax), Mathf.Clamp(p.y, potRect.yMin, potRect.yMax));
            float d = Vector2.Distance(p, nearest); // 0 if the plate appears inside the pot's outline
            if (d < bestDist) { bestDist = d; best = plate; }
        }
        if (best == null) Debug.Log($"[Pour] No clean plate within {plateSearchRadius:P0} of the screen height of the pot's outline.");
        return best;
    }

    // The bin nearest the pot on screen, within plateSearchRadius of the pot's outline, or null. Only called on a Pour gesture.
    public Bin FindBin(Camera cam)
    {
        if (!ScreenRect(cam, out Rect potRect)) return null;

        Bin best = null;
        float bestDist = plateSearchRadius * Screen.height;
        foreach (var bin in Bin.All)
        {
            Vector3 s = cam.WorldToScreenPoint(bin.MouthPosition);
            if (s.z <= 0f) continue;
            Vector2 p = s;
            Vector2 nearest = new Vector2(Mathf.Clamp(p.x, potRect.xMin, potRect.xMax), Mathf.Clamp(p.y, potRect.yMin, potRect.yMax));
            float d = Vector2.Distance(p, nearest);
            if (d < bestDist) { bestDist = d; best = bin; }
        }
        return best;
    }

    // The pot's meshes' bounds, projected to a screen rectangle. False if the pot is behind the camera.
    private bool ScreenRect(Camera cam, out Rect rect)
    {
        rect = default;
        Bounds b = PotBounds();
        Vector2 min = new Vector2(float.PositiveInfinity, float.PositiveInfinity), max = -min;
        for (int i = 0; i < 8; i++)
        {
            Vector3 corner = b.center + Vector3.Scale(b.extents, new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
            Vector3 sc = cam.WorldToScreenPoint(corner);
            if (sc.z <= 0f) continue; // corner behind the camera: skip it
            min = Vector2.Min(min, sc);
            max = Vector2.Max(max, sc);
        }
        if (float.IsInfinity(min.x)) return false;
        rect = Rect.MinMaxRect(min.x, min.y, max.x, max.y);
        return true;
    }

    // Returns false (and why) if it can't pour now; nothing changes then.
    public bool TryPour(Plate plate, Camera cam, out string why)
    {
        why = null;
        if (IsPouring) { why = "already pouring"; return false; }
        if (plate == null) { why = "no clean plate near the pot"; return false; }
        if (!plate.CanTakeFood) { why = $"the plate isn't clean ({plate.State})"; return false; }
        if (!pot.IsReadyToPlate)
        {
            why = pot.State switch
            {
                Pot.PotState.Empty => "the pot is empty",
                Pot.PotState.Burnt => "it's burnt",
                Pot.PotState.Cooked => "the curry needs its next ingredient first",
                _ => "it isn't cooked yet"
            };
            return false;
        }

        StartCoroutine(Pour(plate, plate.PourPoint, cam));
        return true;
    }

    // Pours everything out into the bin, whatever state it's in (a wrong mix, burnt, even a cooked dish).
    public bool TryPourOut(Bin bin, Camera cam, out string why)
    {
        why = null;
        if (IsPouring) { why = "already pouring"; return false; }
        if (bin == null) { why = "no bin near the pot"; return false; }
        if (pot.State == Pot.PotState.Empty) { why = "the pot is empty"; return false; }

        StartCoroutine(Pour(null, bin.MouthPosition, cam, bin));
        return true;
    }

    // plate null = pouring out into `bin`: nothing fills, the pot is just emptied.
    private IEnumerator Pour(Plate plate, Vector3 target, Camera cam, Bin bin = null)
    {
        IsPouring = true;
        bool pouringOut = plate == null;
        string dish = pot.CurrentRecipe != null ? pot.CurrentRecipe.name : "the mix";
        Color color = surfaceMaterial != null ? surfaceMaterial.color : new Color(1f, 0.6f, 0.15f);
        Debug.Log(pouringOut ? $"[Pour] Pouring {dish} out into the bin" : $"[Pour] Pouring {dish} onto {plate.name}");
        onPourStart?.Invoke();

        // Which way to tip (flat, in the world): toward the camera, sideways on screen toward the plate's side, or at the plate.
        Vector3 toPlate = Vector3.ProjectOnPlane(target - Center(), Vector3.up);
        Vector3 toCamera = Vector3.ProjectOnPlane(cam.transform.position - Center(), Vector3.up);
        Vector3 camRight = Vector3.ProjectOnPlane(cam.transform.right, Vector3.up).normalized;
        Vector3 sideways = Vector3.Dot(toPlate, camRight) < 0f ? -camRight : camRight;
        Vector3 dir = pourDirection switch
        {
            PourDirection.TowardCamera => toCamera.magnitude > 0.02f ? toCamera : sideways, // camera right above the pot: sideways
            PourDirection.TowardPlate => toPlate.magnitude > 0.02f ? toPlate : sideways,
            _ => sideways
        };
        dir.Normalize();

        // Turn the pot about the vertical so its pouring side (between the handles) faces `dir`, then tip it over that side.
        // With a Lip set, that side is the lip's; otherwise whichever end of the pour axis needs the smaller turn.
        Quaternion startRot = transform.rotation;
        Vector3 axis = Vector3.ProjectOnPlane(transform.TransformDirection(PourAxis), Vector3.up);
        if (lip != null && Vector3.Dot(transform.InverseTransformPoint(lip.position), PourAxis) < 0f) axis = -axis;
        else if (lip == null && Vector3.Dot(axis, dir) < 0f) axis = -axis;
        Quaternion facedRot = axis.sqrMagnitude > 1e-6f ? Quaternion.FromToRotation(axis.normalized, dir) * startRot : startRot;
        Quaternion tiltRot = Quaternion.AngleAxis(tiltAngle, Vector3.Cross(Vector3.up, dir)) * facedRot;

        // Lip in the pot's own space, so it turns and tilts with the pot.
        Vector3 lipLocal;
        if (lip != null) lipLocal = transform.InverseTransformPoint(lip.position);
        else
        {
            Vector3 rim = RimPoint(dir); // a round pot: the rim point facing `dir` is the same before and after turning
            transform.rotation = facedRot;
            lipLocal = transform.InverseTransformPoint(rim);
            transform.rotation = startRot;
        }

        // Surface: sink from where it is now toward the bottom of the pot.
        Vector3 surfaceFrom = Vector3.zero, surfaceTo = Vector3.zero;
        if (soupSurface != null)
        {
            surfaceFrom = soupSurface.localPosition;
            Vector3 world = soupSurface.position;
            world.y = Mathf.Lerp(world.y, PotBounds().min.y, surfaceSink);
            surfaceTo = soupSurface.parent != null ? soupSurface.parent.InverseTransformPoint(world) : world;
        }

        // 1. Tilt.
        for (float t = 0f; t < 1f; t += Time.deltaTime / Mathf.Max(tiltTime, 0.01f))
        {
            transform.rotation = Quaternion.Slerp(startRot, tiltRot, Mathf.SmoothStep(0f, 1f, t));
            yield return null;
        }
        transform.rotation = tiltRot;

        // 2. Pour. The plate starts filling once the first drops reach it.
        if (stream != null)
        {
            var main = stream.main;
            main.startColor = color;
            AimStream(transform.TransformPoint(lipLocal), target);
            stream.Play(true);
        }
        if (!pouringOut) plate.BeginFill(dish, color);
        if (stream != null) StartCoroutine(LogStream());
        else Debug.LogWarning("[Pour] No PourStream found, so there is nothing to see. It must be a child of the pot named exactly PourStream.");

        bool aborted = false;
        for (float elapsed = 0f; elapsed < pourTime + fallTime; elapsed += Time.deltaTime)
        {
            if (!pouringOut && (pot.IsBurnt || plate == null)) { aborted = true; break; } // burnt mid-pour: no food for the plate
            transform.rotation = tiltRot; // in case anything else touched it this frame
            if (!pouringOut) target = plate.PourPoint; // the plate may be moving with the hand
            if (stream != null) AimStream(transform.TransformPoint(lipLocal), target);
            if (stream != null && elapsed >= pourTime && stream.isEmitting) stream.Stop(true, ParticleSystemStopBehavior.StopEmitting);

            float pourT = Mathf.Clamp01(elapsed / pourTime);
            if (soupSurface != null) soupSurface.localPosition = Vector3.Lerp(surfaceFrom, surfaceTo, pourT);
            if (!pouringOut) plate.SetFill((elapsed - fallTime) / pourTime);
            yield return null;
        }
        if (stream != null) stream.Stop(true, ParticleSystemStopBehavior.StopEmitting); // drops already in the air finish falling

        // 3. Tilt back, then empty the pot.
        for (float t = 0f; t < 1f; t += Time.deltaTime / Mathf.Max(tiltTime, 0.01f))
        {
            transform.rotation = Quaternion.Slerp(tiltRot, startRot, Mathf.SmoothStep(0f, 1f, t));
            yield return null;
        }
        transform.rotation = startRot;

        if (aborted)
        {
            if (plate != null) plate.MakeClean();
            Debug.Log("[Pour] Stopped: the food burnt while pouring.");
        }
        else
        {
            if (!pouringOut) plate.FinishFill();
            pot.Clear();
            if (pouringOut) Debug.Log("[Pour] Pot emptied.");
            if (pouringOut) bin.Binned(dish);
        }
        IsPouring = false;
        onPourEnd?.Invoke();
    }

    // One diagnostic line a moment into the pour: is the stream running, where is it, how big, and can it render?
    private IEnumerator LogStream()
    {
        yield return new WaitForSeconds(Mathf.Min(0.25f, pourTime * 0.5f));
        if (stream == null) yield break;
        var r = stream.GetComponent<ParticleSystemRenderer>();
        var main = stream.main;
        Debug.Log($"[Pour] Stream check: active={stream.gameObject.activeInHierarchy}, playing={stream.isPlaying}, emitting={stream.isEmitting}, " +
                  $"particles={stream.particleCount}, emissionOn={stream.emission.enabled}, rate={stream.emission.rateOverTime.constant}, " +
                  $"renderer={(r != null && r.enabled)}, material={(r != null && r.sharedMaterial != null ? r.sharedMaterial.name : "none")}, " +
                  $"size={main.startSize.constant:F3}, scaling={main.scalingMode}, lossyScale={stream.transform.lossyScale}, " +
                  $"pos={stream.transform.position}, layer={LayerMask.LayerToName(stream.gameObject.layer)}");
    }

    // Aimed: points the stream so a drop leaving `from` lands on `to` after fallTime seconds, gravity included:
    // v = (to - from - 0.5*g*T*T) / T. The cone shape emits along the particle system's forward (Z) axis.
    private void AimStream(Vector3 from, Vector3 to)
    {
        float T = Mathf.Max(fallTime, 0.05f);
        Vector3 g = Physics.gravity * gravityScale;
        Vector3 v = aimAtPlate
            ? (to - from - 0.5f * g * T * T) / T
            : Vector3.ProjectOnPlane(from - Center(), Vector3.up).normalized * spillSpeed; // straight out over the rim, then gravity

        stream.transform.SetPositionAndRotation(from, Quaternion.LookRotation(v.sqrMagnitude > 1e-6f ? v : Vector3.down));
        var main = stream.main;
        main.startSpeed = v.magnitude;
        main.startLifetime = T;
        main.gravityModifier = gravityScale;
    }

    // Point on the top rim of the pot on the side facing `dir` (horizontal), from the pot's combined mesh bounds.
    private Vector3 RimPoint(Vector3 dir)
    {
        Bounds b = PotBounds();
        float radius = Mathf.Min(b.extents.x, b.extents.z);
        return new Vector3(b.center.x, b.max.y, b.center.z) + dir * radius;
    }

    private Vector3 Center() => PotBounds().center;

    // Pot meshes only: the particle systems (fire, stream) and the bar's canvas are left out.
    private Bounds PotBounds()
    {
        Bounds b = new Bounds(transform.position, Vector3.zero);
        bool any = false;
        foreach (var r in GetComponentsInChildren<MeshRenderer>())
        {
            if (!r.enabled) continue;
            if (!any) { b = r.bounds; any = true; }
            else b.Encapsulate(r.bounds);
        }
        return b;
    }

    private static Transform FindChild(Transform parent, string childName)
    {
        foreach (Transform child in parent.GetComponentsInChildren<Transform>(true))
            if (child.name == childName) return child;
        return null;
    }
}
