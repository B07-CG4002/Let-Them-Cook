using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

// The serve window (marker 6). Dropping a held plate of food over it serves the oldest order for that dish:
// the plate leaves the hand and hides, and after Dirty Delay it comes back dirty on top of the stack on the dirty-plate spot
// (one grabbable DirtyPlateStack parent, however many plates; a new one is made if the spot is empty).
// Wrong dish, empty or dirty plate: refused, the plate stays in the hand. GestureButtonController calls FindFor/TryServe on a drop.
public class ServeStation : MonoBehaviour
{
    [Tooltip("The shelf spot the plate is dropped on (ServeSeat). Empty = this object's position.")]
    [SerializeField] private Transform serveSeat;
    [Tooltip("A plate dropped within this many screen pixels of the seat counts as at the window, " +
        "on top of anywhere over the window's own model.")]
    [SerializeField] private float snapRadius = 150f;

    [Header("Dirty plates")]
    [Tooltip("Where served plates come back dirty (DirtyPlateSeat, the round recess). Empty = they don't come back.")]
    [SerializeField] private Transform dirtyPlateSeat;
    [Tooltip("Seconds after serving before the dirty plate appears.")]
    [SerializeField] private float dirtyDelay = 5f;
    [Tooltip("How much higher each extra dirty plate on the stack sits (m).")]
    [SerializeField] private float stackStep = 0.015f;
    [Tooltip("For testing without washing: served plates go straight back onto this Dish Rack, clean.")]
    [SerializeField] private bool comeBackClean = false;
    [Tooltip("Only for Come Back Clean. Empty = the DishRack in the scene.")]
    [SerializeField] private DishRack dishRack;

    [Header("Events (bell, sounds)")]
    public UnityEvent<string> onServed;   // dish
    public UnityEvent<string> onRefused;  // why

    public Vector3 SeatPosition => serveSeat != null ? serveSeat.position : transform.position;

    // Every enabled window; read only on a plate drop, so nothing searches the scene.
    public static readonly List<ServeStation> All = new List<ServeStation>();

    private DirtyPlateStack stackOnSeat; // the stack last made here; a new one is made once it's carried off

    void OnEnable() => All.Add(this);
    void OnDisable() => All.Remove(this);

    // The window the plate is being dropped at, or null. Same test as a pot over a stove.
    public static ServeStation FindFor(Transform plate, Camera cam)
    {
        ServeStation best = null;
        float bestDist = float.PositiveInfinity;
        Vector3 platePos = plate.position;
        Vector3 plateScreen = cam.WorldToScreenPoint(platePos);
        if (plateScreen.z <= 0f) return null;

        foreach (var station in All)
        {
            Vector3 seatScreen = cam.WorldToScreenPoint(station.SeatPosition);
            float d = seatScreen.z > 0f ? Vector2.Distance(plateScreen, seatScreen) : float.PositiveInfinity;
            bool over = d <= station.snapRadius || ScreenBounds.Covers(station.transform, platePos, cam, plate);
            if (over && d < bestDist) { best = station; bestDist = d; }
        }
        return best;
    }

    // False (and why) = refused; the caller keeps the plate in the hand.
    public bool TryServe(Plate plate, out string why)
    {
        why = null;
        if (plate.State != Plate.PlateState.Full)
            why = plate.State == Plate.PlateState.Dirty ? "the plate is dirty" : "the plate is empty";
        else if (OrderManager.Instance == null)
            why = "there's no OrderManager in the scene";
        else if (!OrderManager.Instance.TryServe(plate.Dish, out _))
            why = $"nobody ordered {plate.Dish}";

        if (why != null)
        {
            onRefused?.Invoke(why);
            return false;
        }

        string dish = plate.Dish;
        plate.TakeFood(); // dirty now
        plate.gameObject.SetActive(false); // gone from the hand and the hand-tracking lists
        onServed?.Invoke(dish);
        if (comeBackClean || dirtyPlateSeat != null) StartCoroutine(ReturnDirty(plate));
        return true;
    }

    private IEnumerator ReturnDirty(Plate plate)
    {
        yield return new WaitForSeconds(dirtyDelay);
        if (comeBackClean)
        {
            if (dishRack == null) dishRack = FindAnyObjectByType<DishRack>();
            if (dishRack != null) dishRack.Return(plate);
            yield break;
        }
        if (!IsOnDirtySeat(stackOnSeat)) stackOnSeat = MakeStack();
        stackOnSeat.Add(plate, stackStep);
        Debug.Log($"[Serve] {plate.name} back dirty at {dirtyPlateSeat.name} ({stackOnSeat.Count} in the stack)");
    }

    // Empty parent on the seat that the plates go under, grabbable as one item (like a chopped carrot's bunch).
    private DirtyPlateStack MakeStack()
    {
        var go = new GameObject("Dirty Plates");
        go.transform.SetPositionAndRotation(dirtyPlateSeat.position, dirtyPlateSeat.rotation); // no parent, like a crate's ingredient
        var stack = go.AddComponent<DirtyPlateStack>();
        go.AddComponent<HandInteractable>(); // default touch radius, like a chopped bunch
        go.AddComponent<Grabbable>().ItemType = GrabbableType.Plate;
        return stack;
    }

    // Still sitting on the dirty spot (not carried off or put in the sink), within 3 cm sideways.
    private bool IsOnDirtySeat(DirtyPlateStack stack)
    {
        if (stack == null || !stack.gameObject.activeInHierarchy) return false;
        Vector3 d = stack.transform.position - dirtyPlateSeat.position;
        d.y = 0f;
        return d.magnitude < 0.03f && !(stack.TryGetComponent(out Grabbable g) && g.isHeld);
    }
}
