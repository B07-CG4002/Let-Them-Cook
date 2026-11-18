using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

// Washing up at the sink. Put it on the sink model's object (the one with the PotStation, Kind = Sink).
// Dirty plates queue here, first in first washed; only the front one shows (on Plate Seat), the rest wait hidden.
// Washing is a two-hand gesture: BOTH gloves must send Wash (each within Keep Washing For of the other; with hand events on,
// the other hand's state must be WASH right now), with both hands
// at the sink and dirty plates in it (in one-hand mode, that one hand is enough). Then it scrubs: each gesture keeps the scrubbing going
// for Keep Washing For seconds, and Wash Time of scrubbing in total cleans the front plate, which goes back to the Dish Rack.
// Stop scrubbing and the plate keeps its progress (and its half-faded dirt). GestureButtonController calls FindFor/AddDirty on a drop and TryWash on Wash.
public class Sink : MonoBehaviour
{
    [Header("Dirty plates")]
    [Tooltip("Where the front dirty plate shows in the basin; the washing hand must be near it, and dirty plates are dropped near it. " +
        "Empty = this object's position.")]
    [SerializeField] private Transform plateSeat;
    [Tooltip("Dirty plates dropped within this many screen pixels of the seat go in, on top of anywhere over the sink's own model.")]
    [SerializeField] private float snapRadius = 150f;

    [Header("Washing")]
    [Tooltip("Seconds of scrubbing to clean one plate.")]
    [SerializeField] private float washTime = 3f;
    [Tooltip("Each Wash gesture keeps the scrubbing going this long (s). A bit longer than the gap between the glove's " +
        "WASH results, so a steady wash doesn't stutter. For tapping the test button, 3 makes one tap one plate.")]
    [SerializeField] private float keepWashingFor = 0.5f;
    [Tooltip("A washing hand counts as at the sink if its palm centre is over the sink model on screen, " +
        "or within this many screen pixels of the plate seat. No depth is used.")]
    [SerializeField] private float handRadius = 200f;
    [Tooltip("Optional bar over the sink (a ChopProgressBar on a World Space Canvas, like the board's).")]
    [SerializeField] private ChopProgressBar bar;
    [SerializeField] private Color washingColor = new Color(0.35f, 0.7f, 1f);
    [Tooltip("Bubbles particle system, played while scrubbing and stopped (left to fade) when it stops. " +
        "Set Looping on and Play On Awake off on the particle system.")]
    [SerializeField] private ParticleSystem bubbles;

    [Header("Clean plates")]
    [Tooltip("Each washed plate is added to this rack. Empty = the DishRack in the scene, found on the first wash " +
        "(for when the sink and the rack are separate QR prefabs and can't reference each other).")]
    [SerializeField] private DishRack dishRack;

    [Header("Events (sounds)")]
    public UnityEvent onPlateWashed;

    public Vector3 SeatPosition => plateSeat != null ? plateSeat.position : transform.position;
    public int DirtyCount => dirty.Count;
    public float Progress => progress / washTime; // 0..1 on the plate being washed
    public bool IsWashing => DirtyCount > 0 && Time.time < washingUntil;

    // Every enabled sink; read only on a drop or a Wash, so nothing searches the scene.
    public static readonly List<Sink> All = new List<Sink>();

    private readonly Queue<Plate> dirty = new Queue<Plate>();
    private float progress;     // seconds scrubbed on the current plate
    private float washingUntil; // scrubbing runs until this time
    private readonly float[] lastWash = { float.NegativeInfinity, float.NegativeInfinity }; // per Hand: when its glove last sent Wash

    private PotStation potStation; // the sink's PotStation on the same object: a pot in the sink hides the dirty plate

    void Awake() => potStation = GetComponent<PotStation>();

    void OnEnable()
    {
        All.Add(this);
        if (potStation != null)
        {
            potStation.onPotPlaced.AddListener(OnPotMoved);
            potStation.onPotRemoved.AddListener(OnPotMoved);
        }
    }

    void OnDisable()
    {
        All.Remove(this);
        if (potStation != null)
        {
            potStation.onPotPlaced.RemoveListener(OnPotMoved);
            potStation.onPotRemoved.RemoveListener(OnPotMoved);
        }
    }

    private void OnPotMoved(Pot pot) => ShowFront();

    // The sink the dirty stack is being dropped at, or null. Same test as the serve window.
    public static Sink FindFor(Transform item, Camera cam)
    {
        Sink best = null;
        float bestDist = float.PositiveInfinity;
        Vector3 itemPos = item.position;
        Vector3 itemScreen = cam.WorldToScreenPoint(itemPos);
        if (itemScreen.z <= 0f) return null;

        foreach (var sink in All)
        {
            Vector3 seatScreen = cam.WorldToScreenPoint(sink.SeatPosition);
            float d = seatScreen.z > 0f ? Vector2.Distance(itemScreen, seatScreen) : float.PositiveInfinity;
            bool over = d <= sink.snapRadius || ScreenBounds.Covers(sink.transform, itemPos, cam, item);
            if (over && d < bestDist) { best = sink; bestDist = d; }
        }
        return best;
    }

    // The plates join the back of the queue. Only the front one shows; it can't be grabbed (its HandInteractable stays off).
    public void AddDirty(IEnumerable<Plate> plates)
    {
        foreach (var plate in plates)
        {
            if (plate.TryGetComponent(out HandInteractable touch)) touch.enabled = false;
            plate.gameObject.SetActive(false);
            dirty.Enqueue(plate);
        }
        ShowFront();
        Debug.Log($"[Sink] {DirtyCount} dirty plate(s) waiting");
    }

    // Called on a Wash gesture. False (and why) = nothing happens.
    // twoHands = Two Hands mode: both hands at the sink and both gloves washing. Otherwise only `hand`.
    public bool TryWash(Hand hand, bool twoHands, HandTrackingManager hands, Camera cam, out string why)
    {
        why = null;
        if (DirtyCount == 0) why = "no dirty plates in the sink";
        else if (HandProblem(hand, hands, cam) is string far) why = $"the {hand} hand isn't at the sink ({far})";
        else if (twoHands && HandProblem(hand.Other(), hands, cam) is string otherFar)
            why = $"washing needs both hands, the {hand.Other()} hand isn't at the sink ({otherFar})";
        if (why != null) return false;

        lastWash[(int)hand] = Time.time;
        bool otherWashing = CommsBridge.HandStatesOn
            ? CommsBridge.HandIsDoing(hand.Other(), "WASH")
            : Time.time - lastWash[(int)hand.Other()] <= keepWashingFor;
        if (twoHands && !otherWashing)
        {
            why = $"waiting for WASH from the {hand.Other()} glove too";
            return false;
        }

        washingUntil = Time.time + keepWashingFor;
        return true;
    }

    // Scrubbing stops now instead of running out (a hand picked something up). The plate keeps its progress.
    public void StopWashing() => washingUntil = 0f;

    // Null = the hand is at the sink; otherwise why not, for the Console. Screen space only, like the rest of the hand checks.
    private string HandProblem(Hand hand, HandTrackingManager hands, Camera cam)
    {
        if (hands == null || !hands.TryGetHandCenterScreenPos(hand, out Vector2 palm)) return "hand not tracked";
        if (OverSinkOnScreen(palm, cam)) return null;
        Vector3 seatScreen = cam.WorldToScreenPoint(SeatPosition);
        if (seatScreen.z <= 0f) return "sink is behind the camera";
        float d = Vector2.Distance(palm, seatScreen);
        return d <= handRadius ? null : $"palm {d:F0} px from the plate seat, Hand Radius {handRadius:F0}";
    }

    // Palm inside the sink's outline on screen (its meshes' bounds, projected; particles like the bubbles don't count).
    private bool OverSinkOnScreen(Vector2 palm, Camera cam)
    {
        bool any = false;
        Bounds b = default;
        foreach (var r in GetComponentsInChildren<MeshRenderer>())
        {
            if (!any) { b = r.bounds; any = true; }
            else b.Encapsulate(r.bounds);
        }
        if (!any) return false;

        Vector2 min = new Vector2(float.PositiveInfinity, float.PositiveInfinity), max = -min;
        for (int i = 0; i < 8; i++)
        {
            Vector3 corner = b.center + Vector3.Scale(b.extents, new Vector3((i & 1) == 0 ? -1 : 1, (i & 2) == 0 ? -1 : 1, (i & 4) == 0 ? -1 : 1));
            Vector3 sp = cam.WorldToScreenPoint(corner);
            if (sp.z <= 0f) return false; // partly behind the camera
            min = Vector2.Min(min, sp);
            max = Vector2.Max(max, sp);
        }
        return palm.x >= min.x && palm.x <= max.x && palm.y >= min.y && palm.y <= max.y;
    }

    void Update()
    {
        bool washing = IsWashing;
        if (bubbles != null && washing != bubbles.isEmitting)
        {
            if (washing) bubbles.Play();
            else bubbles.Stop(true, ParticleSystemStopBehavior.StopEmitting); // bubbles already out finish
        }
        if (!washing) return;
        progress += Time.deltaTime;
        dirty.Peek().SetDirt(1f - Progress); // the dirt fades as the plate is scrubbed
        if (bar != null) bar.Show(Progress, washingColor, DirtyCount > 1 ? $"Washing ({DirtyCount})" : "Washing");
        if (progress >= washTime) FinishOne();
    }

    private void FinishOne()
    {
        Plate plate = dirty.Dequeue();
        progress = 0f;
        washingUntil = 0f; // the next plate needs its own scrubbing
        if (bar != null) bar.ResetBar();
        ShowFront();

        if (dishRack == null) dishRack = FindAnyObjectByType<DishRack>(); // once, not per frame
        if (dishRack != null) dishRack.Return(plate);
        else
        {
            Debug.LogWarning($"[Sink] {name} has no Dish Rack set, so the washed plate is left beside the sink.");
            plate.MakeClean();
            plate.transform.position = SeatPosition + transform.right * 0.15f;
            if (plate.TryGetComponent(out HandInteractable touch)) touch.enabled = true;
            plate.gameObject.SetActive(true);
        }
        Debug.Log($"[Sink] Plate washed, {DirtyCount} dirty left");
        onPlateWashed?.Invoke();
    }

    private void ShowFront()
    {
        if (dirty.Count == 0) return;
        Plate front = dirty.Peek();
        front.transform.position = SeatPosition;
        front.gameObject.SetActive(potStation == null || potStation.Occupant == null); // hidden under the pot, back when it's lifted
    }
}
