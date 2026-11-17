using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

// The bin. Put it on the bin model's object in the bin's QR prefab. Food is thrown away by pouring it in:
// holding the pot over the bin and doing Pour empties the pot into it (a wrong mix, burnt food, a dish nobody ordered),
// with the same tilt and stream as pouring at the sink. Holding a plate with food on it over the bin and doing Pour
// tips the food off (Accepts Plates). An ingredient dropped over the bin or thrown at it goes in. The bin never fills up. PotPour.FindBin and GestureButtonController.OnPourPressed use it.
public class Bin : MonoBehaviour
{
    [Tooltip("Where the pour stream lands: the bin's opening. Empty = the top centre of the bin's model.")]
    [SerializeField] private Transform mouth;
    [Tooltip("A held plate counts as over the bin if it appears within this many screen pixels of the mouth, " +
        "on top of anywhere over the bin's own model. (A held pot uses the PotPour's Plate Search Radius, like the sink.)")]
    [SerializeField] private float snapRadius = 150f;

    [Header("Ingredients")]
    [Tooltip("An ingredient (whole, half-chopped or chopped) dropped over the bin or thrown at it goes in. Untick to refuse them.")]
    [SerializeField] private bool acceptsIngredients = true;

    [Header("Plates")]
    [Tooltip("Pour with a plated dish over the bin tips the food off and leaves the plate clean in your hand. " +
        "Untick so only the pot can be poured into the bin.")]
    [SerializeField] private bool acceptsPlates = true;
    [Tooltip("How far the plate tips toward the bin (degrees).")]
    [SerializeField] private float plateTiltAngle = 60f;
    [Tooltip("Seconds for the whole tip: over, food slides off, back.")]
    [SerializeField] private float plateTipTime = 0.8f;

    [Header("Events (sounds)")]
    public UnityEvent onFoodBinned;

    public bool AcceptsIngredients => acceptsIngredients;

    // True while a plate is being tipped, so the held plate stays still (like during a pot pour).
    public static bool Busy { get; private set; }

    // Every enabled bin; read only on a Pour, so nothing searches the scene.
    public static readonly List<Bin> All = new List<Bin>();

    void OnEnable() => All.Add(this);
    void OnDisable() => All.Remove(this);

    public Vector3 MouthPosition
    {
        get
        {
            if (mouth != null) return mouth.position;
            bool any = false;
            Bounds b = new Bounds(transform.position, Vector3.zero);
            foreach (var r in GetComponentsInChildren<MeshRenderer>())
            {
                if (!any) { b = r.bounds; any = true; }
                else b.Encapsulate(r.bounds);
            }
            return any ? new Vector3(b.center.x, b.max.y, b.center.z) : transform.position;
        }
    }

    // The bin `worldPos` appears over on screen (within snapRadius of its mouth, or over its model), or null.
    public static Bin FindFor(Vector3 worldPos, Camera cam, Transform exclude = null)
    {
        Vector3 p = cam.WorldToScreenPoint(worldPos);
        if (p.z <= 0f) return null;

        Bin best = null;
        float bestDist = float.PositiveInfinity;
        foreach (var bin in All)
        {
            Vector3 m = cam.WorldToScreenPoint(bin.MouthPosition);
            float d = m.z > 0f ? Vector2.Distance(p, m) : float.PositiveInfinity;
            bool over = d <= bin.snapRadius || ScreenBounds.Covers(bin.transform, worldPos, cam, exclude);
            if (over && d < bestDist) { best = bin; bestDist = d; }
        }
        return best;
    }

    // Called by PotPour when a pot has been poured out into this bin.
    public void Binned(string what)
    {
        Debug.Log($"[Bin] {what} thrown away.");
        onFoodBinned?.Invoke();
    }

    // Throws an ingredient away: it's removed at once. Callers check AcceptsIngredients first.
    public void TakeIngredient(Transform item)
    {
        string what = item.name;
        item.gameObject.SetActive(false); // out of the hand-tracking lists at once; Destroy waits for end of frame
        Destroy(item.gameObject);
        Binned(what);
    }

    // Tips the food off a held plate into the bin. Returns false (and why) if it can't; nothing changes then.
    public bool TryTipPlate(Plate plate, out string why)
    {
        why = null;
        if (!acceptsPlates) { why = "the bin only takes what's in the pot"; return false; }
        if (Busy) { why = "already tipping a plate"; return false; }
        if (plate == null || plate.State != Plate.PlateState.Full) { why = "there's no food on the plate"; return false; }
        StartCoroutine(TipPlate(plate));
        return true;
    }

    private IEnumerator TipPlate(Plate plate)
    {
        Busy = true;
        string dish = plate.Dish;
        Transform t = plate.transform;
        Quaternion startRot = t.rotation;
        Vector3 toBin = Vector3.ProjectOnPlane(MouthPosition - t.position, Vector3.up);
        if (toBin.sqrMagnitude < 1e-4f) toBin = Vector3.ProjectOnPlane(t.right, Vector3.up); // right over the mouth: any side
        Quaternion tipRot = Quaternion.AngleAxis(plateTiltAngle, Vector3.Cross(Vector3.up, toBin.normalized)) * startRot;

        // Tip over in the first third, food shrinks away in the middle third, back upright in the last.
        float third = Mathf.Max(plateTipTime, 0.05f) / 3f;
        for (float e = 0f; e < third * 3f; e += Time.deltaTime)
        {
            if (plate == null) break;
            float tilt = e < third ? e / third : e < 2f * third ? 1f : 1f - (e - 2f * third) / third;
            t.rotation = Quaternion.Slerp(startRot, tipRot, Mathf.SmoothStep(0f, 1f, tilt));
            if (e >= third) plate.SetFill(1f - (e - third) / third);
            yield return null;
        }
        if (plate != null)
        {
            t.rotation = startRot;
            plate.MakeClean();
        }
        Busy = false;
        Binned(dish ?? "The plated dish");
    }
}
