using System.Collections.Generic;
using UnityEngine;
using EzySlice;

public class GestureButtonController : MonoBehaviour
{
    [SerializeField] private Camera arCamera;
    [SerializeField] private HandTrackingManager handTrackingManager;
    [Tooltip("Cut face for ingredients whose IngredientState has no Cross Section Material of their own.")]
    [SerializeField] private Material crossSectionMaterial;
    [SerializeField] private int maxChopsPerIngredient = 3;

    [Header("Held item depth")]
    [Tooltip("LiDAR hand depth. Empty = held items stay at the distance they were grabbed at (old behaviour).")]
    [SerializeField] private HandDepthEstimator handDepth;
    [Tooltip("How far in front of the finger (toward the camera) a held item sits, so the hand doesn't occlude it.")]
    [SerializeField] private float holdInFrontOfHand = 0.02f;
    [SerializeField] private float minHoldDepth = 0.1f;
    [Tooltip("Fastest a held item may move toward/away from the camera (m/s). When the hand leaves the view, LiDAR briefly " +
        "reads the table or floor behind it; limiting the speed stops the item jumping there and hiding behind the real table.")]
    [SerializeField] private float maxDepthSpeed = 0.5f;

    [Header("Drop fall")]
    [Tooltip("A dropped item that doesn't land on a board, pot or station falls to the table (the nearest anchored " +
        "QR marker's height). Untick to leave it where the hand let go.")]
    [SerializeField] private bool fallWhenDropped = true;
    [Tooltip("Downward acceleration of the fall (m/s^2). 9.81 = real gravity.")]
    [SerializeField] private float fallGravity = 9.81f;
    [Tooltip("Only falls if an anchored marker is within this many meters sideways; otherwise it stays put.")]
    [SerializeField] private float fallReach = 1f;
    [Tooltip("Bounce on landing: 0 = none, 0.2 = a small hop.")]
    [SerializeField, Range(0f, 0.6f)] private float fallBounce = 0.2f;

    [Header("Throw")]
    [Tooltip("Only ingredients can be thrown (whole, half-chopped or chopped). The aim is the centre of the screen, " +
        "i.e. where you're looking. A pot or free chopping board whose centre is within this fraction of the screen " +
        "height from the aim point is the target, and the ingredient lands in/on it.")]
    [SerializeField, Range(0.02f, 0.5f)] private float throwSnapRadius = 0.15f;
    [Tooltip("With no target, the ingredient lands on the table where you're looking, at most this many meters away " +
        "(further, or no table under the aim point: it stops this far ahead and falls if a marker is near).")]
    [SerializeField] private float maxThrowDistance = 1.5f;
    [Tooltip("Thrown ingredients stay inside the kitchen: the area outlined by the anchored markers, plus this many " +
        "meters around it. A throw aimed further out lands on the edge instead.")]
    [SerializeField] private float kitchenMargin = 0.3f;
    [Tooltip("Flight time (s).")]
    [SerializeField] private float throwDuration = 0.5f;
    [Tooltip("Height of the arc at its middle (m). 0 = straight line.")]
    [SerializeField] private float throwArcHeight = 0.15f;
    [Tooltip("Size (radius, m) of the thrown item for hitting things on the way. It stops at the first collider in its " +
        "path (add plain colliders, no Rigidbody, to stations you want solid), then drops to the table beside it, or " +
        "stays on top if it hit the top. 0 = flies through everything.")]
    [SerializeField] private float throwHitRadius = 0.03f;

    [Header("Burnt food")]
    [Tooltip("Grab at a burnt pot empties it where it stands (the old shortcut). Untick so Grab picks the burnt pot up " +
        "like any pot, and it has to be poured out into the bin.")]
    [SerializeField] private bool grabEmptiesBurntPot = true;

    [Header("Pour")]
    [Tooltip("Tick so only the hand holding the pot can pour from it: a Pour from the plate hand or an empty hand does " +
        "nothing. Unticked = the old way: an empty hand's Pour counts as the other hand's, and the plate hand pours " +
        "from the pot in the other hand or the pot it's at. A plate with food on it held over the bin still tips into " +
        "the bin either way.")]
    [SerializeField] private bool onlyPotHandPours = false;

    [Header("Late gestures")]
    [Tooltip("Glove gestures arrive a little after you make them, and by then the hand may have moved on (lifted off " +
        "the knife, left the board). If a Grab, Drop, Chop or Pour finds nothing where the hand is now, it uses where " +
        "that hand was up to this many seconds ago instead. 0 = off (only where the hand is now, the old behaviour).")]
    [SerializeField, Range(0f, 2f)] private float gestureLookback = 0.5f;

    [Header("Refused drop")]
    [Tooltip("When a drop is refused (unchopped into the pot, ingredient already in the pot, full board), " +
        "the held item shakes side to side for this long (s) as a \"no\".")]
    [SerializeField] private float refuseShakeTime = 0.35f;
    [Tooltip("How far it shakes, in screen pixels.")]
    [SerializeField] private float refuseShakePixels = 14f;

    // What one hand holds. Each hand grabs, carries and drops on its own.
    private class HandSlot
    {
        public Transform item;
        public Grabbable grabbable;
        public float depth;
        public Vector2 screenOffset; // item's on-screen offset from the palm centre; zero for normal grabs
        public Pot pot;              // the pot in this hand, or null (for pouring)
        public float refuseShakeLeft;
    }
    private readonly HandSlot[] slots = { new HandSlot(), new HandSlot() }; // indexed by Hand
    private PotPour activePour; // the pour started by a Pour gesture; held items stay still while it runs
    private HandSlot Slot(Hand hand) => slots[(int)hand];
    private readonly HandHistory history = new HandHistory(); // last few seconds per hand, for late gestures

    // One hand mode (Two Hands unticked on HandTrackingManager, or none set): every action goes to the right hand.
    private Hand Use(Hand hand) => TwoHands ? hand : Hand.Right;

    public bool TwoHands => handTrackingManager != null && handTrackingManager.TwoHands;
    public GrabbableType? HeldType(Hand hand) => Slot(hand).grabbable != null ? Slot(hand).grabbable.ItemType : (GrabbableType?)null;

    // The public On...Pressed() methods are wired to the on-screen buttons' OnClick() and act on the right hand.
    // The ...Left() ones are for a second row of test buttons. CommsBridge calls the Hand versions with the glove's hand.
    public void OnGrabPressed() => OnGrabPressed(Hand.Right);
    public void OnDropPressed() => OnDropPressed(Hand.Right);
    public void OnChopPressed() => OnChopPressed(Hand.Right);
    public void OnPourPressed() => OnPourPressed(Hand.Right);
    public void OnWashPressed() => OnWashPressed(Hand.Right);
    public void OnGrabPressedLeft() => OnGrabPressed(Hand.Left);
    public void OnDropPressedLeft() => OnDropPressed(Hand.Left);
    public void OnChopPressedLeft() => OnChopPressed(Hand.Left);
    public void OnPourPressedLeft() => OnPourPressed(Hand.Left);
    public void OnWashPressedLeft() => OnWashPressed(Hand.Left);
    public void OnThrowPressed() => OnThrowPressed(Hand.Right);
    public void OnThrowPressedLeft() => OnThrowPressed(Hand.Left);

    // Both hands let go of everything without dropping it (the kitchen reset destroys the items).
    public void ReleaseHands()
    {
        for (int i = 0; i < slots.Length; i++) slots[i] = new HandSlot();
        activePour = null;
        history.Clear(); // a late gesture mustn't reach back into the old kitchen
    }

    public void OnGrabPressed(Hand hand)
    {
        hand = Use(hand);
        if (Slot(hand).item != null) return; // this hand is full

        var touched = FindTouchedInteractable(hand);
        bool late = false;
        if (touched == null)
        {
            // Nothing under the hand now: what was it over when the gesture was made?
            foreach (var past in history.Recent(hand, gestureLookback))
            {
                if (!StillGrabbable(past.target)) continue; // gone, switched off, or taken by the other hand since
                touched = past.target;
                late = true;
                LogLate("Grab", hand, past, touched.name);
                break;
            }
            if (touched == null) return;
        }

        var crate = touched.GetComponent<IngredientCrate>();
        if (crate != null)
        {
            GrabFromCrate(hand, crate);
            return;
        }

        var rack = touched.GetComponent<DishRack>();
        if (rack != null)
        {
            GrabFromRack(hand, rack);
            return;
        }

        // Grab at a burnt pot empties it where it stands (Grab Empties Burnt Pot); otherwise the pot is picked up
        // (if it has a Grabbable), and burnt food is poured into the bin.
        var pot = touched.GetComponent<Pot>();
        if (pot != null)
        {
            if (pot.IsBurnt && grabEmptiesBurntPot)
            {
                pot.Clear();
                Debug.Log("[Pot] Burnt food taken out (thrown away).");
            }
            else GrabPot(hand, pot, late);
            return;
        }

        var grabbable = touched.GetComponent<Grabbable>();
        if (grabbable == null) return;

        // Held by its reach point (e.g. the knife handle) rather than its pivot; no reach point = pivot sits on the palm.
        Vector2 offset = (Vector2)arCamera.WorldToScreenPoint(grabbable.transform.position)
                       - (Vector2)arCamera.WorldToScreenPoint(touched.ReachPosition);
        StartHolding(hand, grabbable, offset);
    }

    // Grab at a crate: spawn a fresh ingredient beside the hand (around the fist, not inside it) and hold it.
    private void GrabFromCrate(Hand hand, IngredientCrate crate)
    {
        if (!handTrackingManager.TryGetHandCenterScreenPos(hand, out Vector2 handCenter))
        {
            Debug.Log("[Crate] Grab at crate, but no hand detected.");
            return;
        }

        var item = crate.SpawnNear(handCenter, arCamera);
        if (item == null) return;

        var grabbable = item.GetComponent<Grabbable>();
        if (grabbable == null)
        {
            Debug.LogWarning($"[Crate] {item.name} has no Grabbable, so it can't be held.");
            return;
        }

        // Keep the ingredient where it spawned relative to the hand, instead of jumping onto the palm.
        Vector2 itemScreen = arCamera.WorldToScreenPoint(item.transform.position);
        StartHolding(hand, grabbable, itemScreen - handCenter);
    }

    // Grab at the dish rack: its next clean plate appears on the palm, if it has one.
    private void GrabFromRack(Hand hand, DishRack rack)
    {
        if (!handTrackingManager.TryGetHandCenterScreenPos(hand, out Vector2 handCenter)) return;
        var plate = rack.TakePlate(handCenter, arCamera);
        if (plate == null) return;
        if (!plate.TryGetComponent(out Grabbable grabbable))
        {
            Debug.LogWarning($"[DishRack] {plate.name} has no Grabbable, so it can't be held.");
            return;
        }
        StartHolding(hand, grabbable, Vector2.zero);
    }

    // The pot keeps its on-screen offset from the hand (it's big, so it doesn't jump onto the palm and cover the hand).
    // late = picked from the hand history: the hand has moved since, so the offset is limited to the pot's touch
    // radius, otherwise the pot would hang far from the hand.
    private void GrabPot(Hand hand, Pot pot, bool late = false)
    {
        var grabbable = pot.GetComponent<Grabbable>();
        if (grabbable == null) return;
        if (!handTrackingManager.TryGetHandCenterScreenPos(hand, out Vector2 handCenter)) return;

        Vector2 potScreen = arCamera.WorldToScreenPoint(pot.transform.position);
        Vector2 offset = potScreen - handCenter;
        if (late && pot.TryGetComponent(out HandInteractable touch)) offset = Vector2.ClampMagnitude(offset, touch.Radius);
        StartHolding(hand, grabbable, offset);
        Slot(hand).pot = pot;
        if (pot.Station != null) pot.Station.Remove(pot);
    }

    private void StartHolding(Hand hand, Grabbable grabbable, Vector2 screenOffset)
    {
        var s = Slot(hand);
        s.item = grabbable.transform;
        s.grabbable = grabbable;
        grabbable.isHeld = true;
        s.screenOffset = screenOffset;
        FallToSurface.Cancel(s.item); // caught mid-fall
        ThrowFlight.Cancel(s.item);   // caught mid-air
        foreach (var sink in Sink.All) sink.StopWashing(); // washing needs empty hands; the plate keeps its progress

        // Off the board. Children too: a bunch's pieces each have their own state.
        ChoppingBoard leftBoard = null;
        foreach (var state in s.item.GetComponentsInChildren<IngredientState>())
        {
            if (state.Board != null) leftBoard = state.Board;
            state.OnChoppingBoard = false;
            state.Board = null;
        }

        // The chop count stays on the ChoppedIngredient for the next drop.
        if (leftBoard != null)
        {
            var bar = leftBoard.GetComponentInChildren<ChopProgressBar>(true);
            if (bar != null) bar.ResetBar();
        }

        Vector3 screenPos = arCamera.WorldToScreenPoint(s.item.position);
        s.depth = screenPos.z; // Update then moves it toward the LiDAR hand depth
    }

    public void OnDropPressed(Hand hand)
    {
        hand = Use(hand);
        var s = Slot(hand);
        if (s.item == null) return;
        if (s.pot != null && s.pot.TryGetComponent(out PotPour pouring) && pouring.IsPouring) return; // keep the pot in hand until the pour ends
        if (Bin.Busy && s.item.TryGetComponent(out Plate _)) return; // keep the plate in hand while its food tips into the bin

        // Where the item is now first. If it would just fall there, try where it was when the gesture was made
        // (newest first): the hand may have moved off the board or pot while the gesture was on its way.
        DropResult result = TryDrop(hand, s, null);
        if (result == DropResult.Missed && gestureLookback > 0f)
        {
            Vector3 here = s.item.position;
            foreach (var past in history.Recent(hand, gestureLookback))
            {
                if (past.held != s.item) break; // older than this grab
                s.item.position = past.heldPosition;
                result = TryDrop(hand, s, past);
                if (result != DropResult.Missed) { LogLate("Drop", hand, past, s.item != null ? s.item.name : "item"); break; }
            }
            if (result == DropResult.Missed) s.item.position = here;
        }
        if (result == DropResult.Kept) return;

        if (result == DropResult.Missed && fallWhenDropped)
            FallToSurface.Begin(s.item, QrCodeItemSpawner.Anchors, fallReach, fallGravity, fallBounce);

        s.pot = null;
        if (s.grabbable != null) s.grabbable.isHeld = false;
        s.item = null;
        s.grabbable = null;
    }

    // Placed = it went somewhere (station, board, pot, bin, sink, window) and the hand is free; Kept = refused, it
    // stays in hand (and shakes); Missed = nothing under it, so it falls to the table.
    private enum DropResult { Placed, Kept, Missed }

    // One drop attempt at the item's current position. past = a history sample when retrying a late gesture (its
    // touched pot counts as "hand at the pot"), null = now. A Missed attempt changes nothing.
    private DropResult TryDrop(Hand hand, HandSlot s, HandHistory.Sample? past)
    {
        if (s.pot != null)
        {
            var station = PotStation.FindFor(s.pot, arCamera);
            if (station == null) return DropResult.Missed;
            station.Place(s.pot);
            Debug.Log($"[Pot] {s.pot.name} put down on {station.name}");
            return DropResult.Placed;
        }

        if (s.item.TryGetComponent(out DirtyPlateStack dirtyStack))
        {
            if (!(Sink.FindFor(s.item, arCamera) is Sink sink)) return DropResult.Missed;
            // All its plates join the sink's washing queue and the empty parent goes.
            sink.AddDirty(dirtyStack.TakeAll());
            s.grabbable.isHeld = false;
            s.item.gameObject.SetActive(false); // out of the hand-tracking lists at once; Destroy waits for end of frame
            Destroy(s.item.gameObject);
            Release(s);
            return DropResult.Placed;
        }

        if (s.grabbable.ItemType == GrabbableType.Plate && s.item.TryGetComponent(out Plate plate))
        {
            if (!(ServeStation.FindFor(s.item, arCamera) is ServeStation window)) return DropResult.Missed;
            if (!window.TryServe(plate, out string why))
            {
                Debug.Log($"[Serve] {s.item.name} refused: {why}.");
                Refuse(s);
                return DropResult.Kept;
            }
            s.grabbable.isHeld = false;
            Release(s);
            return DropResult.Placed;
        }

        if (s.grabbable.ItemType != GrabbableType.Ingredient) return DropResult.Missed;

        // Hand touching the pot: the pot gets it or it stays in hand; the boards aren't tried.
        var pot = PotForDrop(hand, s.item, past, onScreen: false);
        if (pot != null)
            return DropIntoPot(s, pot) ? DropResult.Placed : DropResult.Kept;

        // Over a free board: it goes on the board. Tried before "over the pot on screen", because a potato held up
        // over the board can line up on screen with a pot or stove further away.
        var board = PlaceOnChoppingBoard(s.item, out bool boardFull);
        if (board != null)
        {
            SetOnBoard(s.item, board);
            Debug.Log($"[BoardDrop] {s.item.name} dropped on {board.name}");
            return DropResult.Placed;
        }

        // The item over the pot on screen (hand just outside its touch radius): the pot gets it or it stays in hand.
        pot = PotForDrop(hand, s.item, past, onScreen: true);
        if (pot != null)
        {
            Debug.Log($"[Pot] {s.item.name} dropped over {pot.name} on screen");
            return DropIntoPot(s, pot) ? DropResult.Placed : DropResult.Kept;
        }

        // Over the bin: thrown away.
        if (Bin.FindFor(s.item.position, arCamera, s.item) is Bin bin && bin.AcceptsIngredients)
        {
            s.grabbable.isHeld = false;
            bin.TakeIngredient(s.item);
            Release(s);
            return DropResult.Placed;
        }

        if (boardFull)
        {
            Debug.Log($"[BoardDrop] {s.item.name} not dropped: that board already has an ingredient.");
            Refuse(s);
            return DropResult.Kept;
        }
        return DropResult.Missed;
    }

    private static void Release(HandSlot s)
    {
        s.item = null;
        s.grabbable = null;
        s.pot = null;
    }

    // A refused item stays in hand and shakes "no".
    private bool DropIntoPot(HandSlot s, Pot pot)
    {
        s.grabbable.isHeld = false;
        if (!TryPutInPot(s.item, pot))
        {
            s.grabbable.isHeld = true;
            Refuse(s);
            return false;
        }
        Release(s);
        return true;
    }

    // Adds the ingredient to the pot and removes the item, or returns false (logged) if the pot won't take it.
    // Only a fully chopped bunch, or an ingredient that needs no chopping (curry cube), goes in. Shared by drops and throws.
    private bool TryPutInPot(Transform item, Pot pot)
    {
        IngredientType type;
        var chopped = item.GetComponent<ChoppedIngredient>();
        var whole = item.GetComponent<IngredientState>();
        if (chopped != null)
        {
            if (!chopped.IsFullyChopped)
            {
                Debug.Log($"[Pot] {item.name} refused: only {chopped.Chops}/{chopped.MaxChops} chopped.");
                return false;
            }
            type = chopped.Type;
        }
        else if (whole != null && !IngredientState.NeedsChopping(whole.Type))
        {
            type = whole.Type;
        }
        else
        {
            Debug.Log($"[Pot] {item.name} refused: chop it first.");
            return false;
        }

        if (!pot.TryAdd(type, out string why))
        {
            Debug.Log($"[Pot] {item.name} refused: {why}.");
            return false;
        }

        item.gameObject.SetActive(false); // out of the hand-tracking lists at once; Destroy waits for end of frame
        Destroy(item.gameObject);
        return true;
    }

    // Marks the ingredient (and its pieces) as on `board`, or on no board if null, and updates the board's bar.
    private static void SetOnBoard(Transform item, ChoppingBoard board)
    {
        foreach (var state in item.GetComponentsInChildren<IngredientState>())
        {
            state.OnChoppingBoard = board != null;
            state.Board = board;
        }
        if (board == null) return;

        var bar = board.GetComponentInChildren<ChopProgressBar>(true);
        var chopped = item.GetComponent<ChoppedIngredient>();
        if (bar != null)
        {
            if (chopped != null) bar.SetProgress(chopped.Chops, chopped.MaxChops);
            else bar.ResetBar();
        }
    }

    // ---- Throw ----

    // The crosshair shows only while this is true.
    public bool HoldingThrowable => IsThrowable(Slot(Hand.Right)) || IsThrowable(Slot(Hand.Left));
    private static bool IsThrowable(HandSlot s) => s.grabbable != null && s.grabbable.ItemType == GrabbableType.Ingredient;

    // The pot, free board or bin under the aim point, or null. Looked up at most every ThrowTargetRefresh seconds, since
    // the crosshairs ask every frame; a throw always looks it up fresh.
    public Component ThrowTarget
    {
        get
        {
            if (Time.time >= nextTargetLookup)
            {
                cachedThrowTarget = FindThrowTarget();
                nextTargetLookup = Time.time + ThrowTargetRefresh;
            }
            return cachedThrowTarget;
        }
    }
    private const float ThrowTargetRefresh = 0.25f;
    private Component cachedThrowTarget;
    private float nextTargetLookup;

    private Vector2 AimPoint => new Vector2(arCamera.pixelWidth, arCamera.pixelHeight) * 0.5f;

    // Throw needs no hand tracking: the item flies from wherever it is (it stays put when the hand leaves the view).
    public void OnThrowPressed(Hand hand)
    {
        hand = Use(hand);
        var s = Slot(hand);
        if (s.item == null) return;
        if (!IsThrowable(s))
        {
            Debug.Log($"[Throw] {s.item.name} can't be thrown, only ingredients.");
            Refuse(s);
            return;
        }

        Transform item = s.item;
        s.grabbable.isHeld = false;
        s.item = null;
        s.grabbable = null;
        s.refuseShakeLeft = 0f;

        Component target = FindThrowTarget();
        cachedThrowTarget = target;
        nextTargetLookup = Time.time + ThrowTargetRefresh;

        Vector3 landing = target != null ? TopOf(target.transform) : KeepInKitchen(TableUnderAim());
        Vector3 to = landing + PivotAboveBottom(item);
        Debug.Log($"[Throw] {item.name} thrown ({hand} hand) at {(target != null ? target.name : "the table")}");
        ThrowFlight.Begin(item, to, throwDuration, throwArcHeight, throwHitRadius,
            IgnoredWhenAimedAt(target),
            landed => OnThrowLanded(landed, target), OnThrowBlocked);
    }

    // Hit something on the way. Another pot or board: treated as if aimed at it (in the pot, onto the board, or
    // refused). Anything else: its top (e.g. a crate's lid) = stays there; its side = drops to the table beside it.
    private void OnThrowBlocked(Transform item, RaycastHit hit)
    {
        if (item == null) return;
        Debug.Log($"[Throw] {item.name} hit {hit.collider.name} on the way");
        Component station = hit.collider.GetComponentInParent<Pot>();
        if (station == null) station = hit.collider.GetComponentInParent<ChoppingBoard>();
        if (station == null && hit.collider.GetComponentInParent<Bin>() is Bin hitBin && hitBin.AcceptsIngredients) station = hitBin;
        if (station != null)
        {
            OnThrowLanded(item, station);
            return;
        }
        SetOnBoard(item, null);
        if (hit.normal.y > 0.7f) return;
        item.position += hit.normal * 0.01f; // so it doesn't sit inside the model
        FallToSurface.Begin(item, QrCodeItemSpawner.Anchors, fallReach, fallGravity, fallBounce);
    }

    // What a throw at `target` must not bump into on its way: the target's own QR prefab and, for a pot, the station
    // it stands on (the pot can sit on the sink while still belonging to the stove's prefab).
    private static Transform[] IgnoredWhenAimedAt(Component target)
    {
        if (target == null) return null;
        if (target is Pot pot && pot.Station != null)
            return new[] { StationRoot(pot.transform), StationRoot(pot.Station.transform) };
        return new[] { StationRoot(target.transform) };
    }

    // Moves `item` sideways, toward the camera, until it's just past the edge of everything in `objects` (seen from
    // above), so a fall from there lands on the table beside them rather than inside them.
    private void MoveOutsideOf(Transform item, Transform[] objects)
    {
        bool any = false;
        Bounds all = default;
        foreach (var o in objects)
        {
            if (o == null || !TryGetSolidBounds(o, out Bounds b)) continue;
            if (!any) { all = b; any = true; }
            else all.Encapsulate(b);
        }
        if (!any) return;

        Vector3 away = Vector3.ProjectOnPlane(arCamera.transform.position - all.center, Vector3.up);
        if (away.sqrMagnitude < 1e-6f) away = Vector3.ProjectOnPlane(-arCamera.transform.forward, Vector3.up);
        away.Normalize();
        // Distance from the centre to the box's edge along `away` (whichever side it leaves through first).
        float tx = Mathf.Abs(away.x) > 1e-4f ? all.extents.x / Mathf.Abs(away.x) : float.PositiveInfinity;
        float tz = Mathf.Abs(away.z) > 1e-4f ? all.extents.z / Mathf.Abs(away.z) : float.PositiveInfinity;
        float itemHalf = TryGetBounds(item, out Bounds ib) ? Mathf.Max(ib.extents.x, ib.extents.z) : 0f;
        Vector3 spot = all.center + away * (Mathf.Min(tx, tz) + itemHalf + 0.03f);
        Debug.Log($"[Throw] {item.name} bounced off, moved {Vector3.Distance(Vector3.ProjectOnPlane(item.position, Vector3.up), Vector3.ProjectOnPlane(spot, Vector3.up)):0.00} m sideways");
        item.position = new Vector3(spot.x, item.position.y, spot.z);
    }

    // The whole QR prefab a station belongs to (e.g. board + knife, stove + pot), so the target's own parts don't
    // block a throw aimed at it. Not under a marker: just the station itself.
    private static Transform StationRoot(Transform t)
    {
        for (Transform c = t; c.parent != null; c = c.parent)
            if (QrCodeItemSpawner.Anchors.Contains(c.parent) || c.parent.GetComponent<UnityEngine.XR.ARFoundation.ARTrackedImage>() != null)
                return c;
        return t;
    }

    private void OnThrowLanded(Transform item, Component target)
    {
        if (item == null) return;

        if (target is Pot pot && pot != null)
        {
            if (TryPutInPot(item, pot)) return;
            // Refused: bounces off toward you and lands on the table in front of the pot AND whatever it stands on
            // (stove, sink). Only clearing the pot's rim dropped it inside the stove's model, where it vanished.
            MoveOutsideOf(item, pot.Station != null ? new[] { pot.transform, StationRoot(pot.Station.transform) }
                                                     : new[] { pot.transform });
        }
        else if (target is Bin bin && bin != null)
        {
            bin.TakeIngredient(item);
            return;
        }
        else if (target is ChoppingBoard board && board != null)
        {
            // Looks straight down onto the board, so it settles where it landed even if you've turned away.
            if (TryGetBounds(item, out Bounds b)
                && board.Place(item, b.center + Vector3.up) == ChoppingBoard.PlaceResult.Placed)
            {
                SetOnBoard(item, board);
                Debug.Log($"[Throw] {item.name} landed on {board.name}");
                return;
            }
            Debug.Log($"[Throw] {item.name} missed {board.name} (taken meanwhile?)");
        }

        // No target, or refused: drops onto the table beside it.
        SetOnBoard(item, null);
        FallToSurface.Begin(item, QrCodeItemSpawner.Anchors, fallReach, fallGravity, fallBounce);
    }

    // Pot, free chopping board or bin whose top appears nearest the aim point, within the snap radius. Held pots don't count.
    private Component FindThrowTarget()
    {
        Vector2 aim = AimPoint;
        float best = throwSnapRadius * arCamera.pixelHeight;
        Component found = null;

        foreach (var pot in FindObjectsByType<Pot>(FindObjectsInactive.Exclude))
        {
            if (pot.TryGetComponent(out Grabbable g) && g.isHeld) continue;
            Consider(pot);
        }
        foreach (var board in FindObjectsByType<ChoppingBoard>(FindObjectsInactive.Exclude))
            Consider(board);
        foreach (var bin in Bin.All)
            if (bin.AcceptsIngredients) Consider(bin);
        return found;

        void Consider(Component c)
        {
            Vector3 p = arCamera.WorldToScreenPoint(TopOf(c.transform));
            if (p.z <= 0f) return; // behind the camera
            float d = Vector2.Distance(p, aim);
            if (d > best) return;
            if (c is ChoppingBoard b && b.IsOccupied()) return;
            best = d;
            found = c;
        }
    }

    // Top centre of an object's meshes (where a thrown item lands on it). Grabbable things under it (a knife that is a
    // child of the board's prefab) and particles don't count.
    private static Vector3 TopOf(Transform t) =>
        TryGetSolidBounds(t, out Bounds b) ? new Vector3(b.center.x, b.max.y, b.center.z) : t.position;

    // Box around an object's visible meshes only. Particles don't count: the pot's pour stream and flames simulate in
    // world space, so their bounds can reach far away (e.g. to where it last poured). Hidden renderers and grabbable
    // things under it (a knife in the board's prefab) don't count either.
    private static bool TryGetSolidBounds(Transform t, out Bounds bounds)
    {
        bool any = false;
        bounds = default;
        foreach (var r in t.GetComponentsInChildren<Renderer>())
        {
            if (!r.enabled || r is ParticleSystemRenderer || r is TrailRenderer || r is LineRenderer) continue;
            var g = r.GetComponentInParent<Grabbable>();
            if (g != null && g.transform != t && g.transform.IsChildOf(t)) continue;
            if (!any) { bounds = r.bounds; any = true; }
            else bounds.Encapsulate(r.bounds);
        }
        return any;
    }

    // How far the item's pivot sits above its lowest point, so it lands resting on the spot rather than sunk into it.
    private static Vector3 PivotAboveBottom(Transform item) =>
        TryGetBounds(item, out Bounds b) ? Vector3.up * (item.position.y - b.min.y) : Vector3.zero;

    // Where the aim ray meets the table (the height of an anchored marker near that spot), within Max Throw Distance.
    // No table there: the point Max Throw Distance along the aim ray.
    private Vector3 TableUnderAim()
    {
        Ray ray = arCamera.ScreenPointToRay(AimPoint);
        float bestDist = maxThrowDistance;
        Vector3 result = ray.GetPoint(maxThrowDistance);
        bool hitTable = false;
        foreach (var marker in QrCodeItemSpawner.Anchors)
        {
            if (marker == null) continue;
            var table = new UnityEngine.Plane(Vector3.up, marker.position);
            if (!table.Raycast(ray, out float enter) || enter > bestDist) continue;
            Vector3 hit = ray.GetPoint(enter);
            Vector3 d = hit - marker.position;
            if (d.x * d.x + d.z * d.z > fallReach * fallReach) continue; // that marker's table isn't under this spot
            bestDist = enter;
            result = hit;
            hitTable = true;
        }
        // Looking up (or past every table): the spot Max Throw Distance ahead, brought down to the nearest table,
        // so it lobs forward and lands instead of stopping in mid-air.
        if (!hitTable) result.y = NearestMarkerY(result, result.y);
        return result;
    }

    // Height of the anchored marker nearest `p` seen from above, or `fallback` if none is anchored.
    private static float NearestMarkerY(Vector3 p, float fallback)
    {
        float y = fallback, best = float.PositiveInfinity;
        foreach (var m in QrCodeItemSpawner.Anchors)
        {
            if (m == null) continue;
            float dx = m.position.x - p.x, dz = m.position.z - p.z;
            if (dx * dx + dz * dz < best) { best = dx * dx + dz * dz; y = m.position.y; }
        }
        return y;
    }

    // Moves a landing spot outside the kitchen back to its edge: the outline around the anchored markers (seen from
    // above), widened by Kitchen Margin. A moved spot gets the height of the nearest marker's table. No markers = unchanged.
    private Vector3 KeepInKitchen(Vector3 p)
    {
        var points = new List<Vector2>();
        foreach (var m in QrCodeItemSpawner.Anchors)
            if (m != null) points.Add(new Vector2(m.position.x, m.position.z));
        if (points.Count == 0) return p;

        var hull = ConvexHull(points);
        Vector2 flat = new Vector2(p.x, p.z);
        if (hull.Count >= 3 && InsideConvex(hull, flat)) return p;

        // Nearest point on the outline (a lone marker or a line of them: a point or segment).
        Vector2 nearest = hull[0];
        for (int i = 0; i < hull.Count; i++)
        {
            Vector2 a = hull[i], b = hull[(i + 1) % hull.Count];
            Vector2 ab = b - a;
            float k = ab.sqrMagnitude > 0f ? Mathf.Clamp01(Vector2.Dot(flat - a, ab) / ab.sqrMagnitude) : 0f;
            Vector2 c = a + ab * k;
            if ((c - flat).sqrMagnitude < (nearest - flat).sqrMagnitude) nearest = c;
        }
        Vector2 off = flat - nearest;
        if (off.magnitude <= kitchenMargin) return p;
        Vector2 edge = nearest + off.normalized * kitchenMargin;

        var moved = new Vector3(edge.x, p.y, edge.y);
        moved.y = NearestMarkerY(moved, p.y);
        return moved;
    }

    // Outline around the points, counter-clockwise (Andrew's monotone chain). 1 or 2 points come back as they are.
    private static List<Vector2> ConvexHull(List<Vector2> pts)
    {
        pts.Sort((a, b) => a.x != b.x ? a.x.CompareTo(b.x) : a.y.CompareTo(b.y));
        if (pts.Count < 3) return pts;
        var hull = new List<Vector2>();
        for (int pass = 0; pass < 2; pass++)
        {
            int start = hull.Count;
            for (int i = 0; i < pts.Count; i++)
            {
                Vector2 q = pass == 0 ? pts[i] : pts[pts.Count - 1 - i];
                while (hull.Count >= start + 2 && Cross(hull[hull.Count - 2], hull[hull.Count - 1], q) <= 0f)
                    hull.RemoveAt(hull.Count - 1);
                hull.Add(q);
            }
            hull.RemoveAt(hull.Count - 1);
        }
        return hull;
    }

    private static float Cross(Vector2 o, Vector2 a, Vector2 b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);

    private static bool InsideConvex(List<Vector2> hull, Vector2 p)
    {
        for (int i = 0; i < hull.Count; i++)
            if (Cross(hull[i], hull[(i + 1) % hull.Count], p) < 0f) return false;
        return true;
    }

    private void Refuse(HandSlot s) => s.refuseShakeLeft = refuseShakeTime;

    // Sideways screen offset for the "no" shake: a few quick swings that die down.
    private Vector2 RefuseShake(HandSlot s)
    {
        if (s.refuseShakeLeft <= 0f) return Vector2.zero;
        s.refuseShakeLeft -= Time.deltaTime;
        float fade = Mathf.Clamp01(s.refuseShakeLeft / refuseShakeTime);
        return new Vector2(Mathf.Sin(s.refuseShakeLeft * 50f) * refuseShakePixels * fade, 0f);
    }

    // Chops with the knife in that hand.
    public void OnChopPressed(Hand hand)
    {
        hand = Use(hand);
        var s = Slot(hand);
        if (s.grabbable == null || s.grabbable.ItemType != GrabbableType.Knife)
        {
            Debug.Log($"[Chop] Not holding the knife ({hand} hand).");
            return;
        }

        Transform bladePlane = s.item.Find("BladePlane");
        Vector3 bladePos = bladePlane != null ? bladePlane.position : s.item.position;
        Vector3 bladeNormal = bladePlane != null ? bladePlane.up : s.item.right;

        // Blade where the knife is now; if it's not near a piece (the hand already lifted it), where the knife was
        // when the gesture was made, newest first.
        FindChopPiece(bladePos, bladeNormal, out IngredientState ingredient, out float closestDist, out Vector3 cutPos);
        if ((ingredient == null || closestDist > 0.05f) && gestureLookback > 0f)
        {
            foreach (var past in history.Recent(hand, gestureLookback))
            {
                if (past.held != s.item) break; // older than this grab
                Vector3 pastBlade = bladePos + (past.heldPosition - s.item.position);
                FindChopPiece(pastBlade, bladeNormal, out var pastPiece, out float pastDist, out Vector3 pastCut);
                if (pastPiece == null || pastDist > 0.05f) continue;
                ingredient = pastPiece; closestDist = pastDist; cutPos = pastCut;
                LogLate("Chop", hand, past, pastPiece.name);
                break;
            }
        }

        if (ingredient == null)
        {
            Debug.Log("[Chop] Blade isn't passing through any piece on a board.");
            return;
        }

        // A whole ingredient has no ChoppedIngredient yet; a piece finds the one it belongs to (its parent).
        var chopped = ingredient.GetComponentInParent<ChoppedIngredient>();
        if (chopped != null && chopped.IsFullyChopped)
        {
            Debug.Log($"[Chop] Already fully chopped ({chopped.Chops}/{chopped.MaxChops}).");
            return;
        }

        if (closestDist > 0.05f) // within 5 cm of the piece's box
        {
            Debug.Log($"[Chop] Nearest piece too far ({closestDist:F3} m), move the knife closer.");
            return;
        }

        // Each ingredient can have its own cut face (orange inside a carrot, white inside a potato); else the shared one.
        Material cutFace = ingredient.CrossSectionMaterial != null ? ingredient.CrossSectionMaterial : crossSectionMaterial;
        // Undo any hover dim first: the slices copy the current material, so a dimmed colour would stay on the pieces.
        var wholeHover = ingredient.GetComponent<MeshHoverFeedback>();
        if (wholeHover != null) wholeHover.ResetColor();
        var bunchHover = chopped != null ? chopped.GetComponent<MeshHoverFeedback>() : null;
        if (bunchHover != null) bunchHover.ResetColor();

        SlicedHull hull = ingredient.gameObject.Slice(cutPos, bladeNormal, cutFace);

        if (hull != null)
        {
            Transform originalParent = ingredient.transform.parent;

            // First chop: make the ChoppedIngredient now, so a half-chopped ingredient can already be picked up.
            bool firstChop = chopped == null;
            if (firstChop) chopped = CreateChoppedIngredient(ingredient, originalParent);
            chopped.AddChop();
            Transform bunch = chopped.transform;

            var board = ingredient.Board;
            var bar = board != null ? board.GetComponentInChildren<ChopProgressBar>(true) : null;
            if (bar != null) bar.SetProgress(chopped.Chops, chopped.MaxChops);

            GameObject upper = hull.CreateUpperHull(ingredient.gameObject, cutFace);
            GameObject lower = hull.CreateLowerHull(ingredient.gameObject, cutFace);

            foreach (var piece in new[] { upper, lower })
            {
                if (piece == null) continue;

                // EzySlice copies the original's local pose, so parent to the original's parent first...
                piece.transform.SetParent(originalParent, worldPositionStays: false);

                var rb = piece.AddComponent<Rigidbody>();
                rb.useGravity = false;
                rb.isKinematic = true;
                var col = piece.AddComponent<MeshCollider>();
                col.convex = true;
                float separationDistance = 0.01f;
                Vector3 offsetDirection = (piece == upper) ? bladeNormal.normalized : -bladeNormal.normalized;
                piece.transform.position += offsetDirection * separationDistance;

                // ...then into the bunch, keeping where it is. From the second chop on these are the same parent.
                piece.transform.SetParent(bunch, worldPositionStays: true);

                var pieceState = piece.AddComponent<IngredientState>();
                pieceState.Type = ingredient.Type;
                pieceState.CrossSectionMaterial = ingredient.CrossSectionMaterial; // so later chops of this piece match
                pieceState.OnChoppingBoard = true;
                pieceState.Board = board;
            }

            // Hidden now (Destroy only happens at the end of the frame), so it's out of the bunch's colliders below.
            ingredient.gameObject.SetActive(false);
            Destroy(ingredient.gameObject);

            if (firstChop)
            {
                // Added after the pieces are in, since HandPushable collects its colliders when it's added.
                var interactable = bunch.gameObject.AddComponent<HandInteractable>();
                bunch.gameObject.AddComponent<Grabbable>().ItemType = GrabbableType.Ingredient;
                bunch.gameObject.AddComponent<HandPushable>();

                // Same hover highlight as the whole ingredient had (same Dim), wired the way the Inspector does it.
                var hover = bunch.gameObject.AddComponent<MeshHoverFeedback>();
                if (wholeHover != null) hover.Dim = wholeHover.Dim;
                interactable.onHandEnter.AddListener(hover.Highlight);
                interactable.onHandExit.AddListener(hover.ResetColor);
            }
            else
            {
                var pushable = bunch.GetComponent<HandPushable>();
                if (pushable != null) pushable.RefreshColliders(); // the cut piece's collider -> the two new ones
                if (bunchHover != null)
                {
                    bunchHover.RefreshRenderers(); // the cut piece's renderer -> the two new ones
                    var interactable = bunch.GetComponent<HandInteractable>();
                    if (interactable != null && interactable.IsTouched) bunchHover.Highlight(); // still hovered
                }
            }
        }
    }

    // The knife is held at the hand's depth, usually in front of the carrot. So, like the rest of the hand
    // interaction, the chop goes by where the blade appears on screen: the blade point is slid along the
    // camera ray to each piece's depth before testing. Only the blade's angle comes from the knife itself.
    private void FindChopPiece(Vector3 bladePos, Vector3 bladeNormal, out IngredientState ingredient, out float closestDist, out Vector3 cutPos)
    {
        ingredient = null;
        closestDist = float.PositiveInfinity;
        cutPos = bladePos; // blade point moved to the chosen piece's depth
        foreach (var candidate in FindObjectsByType<IngredientState>(FindObjectsInactive.Exclude))
        {
            if (!candidate.OnChoppingBoard) continue;
            var rend = candidate.GetComponent<Renderer>();
            if (rend == null) continue;

            Bounds b = rend.bounds;
            Vector3 bladeAtPiece = AtDepthOf(bladePos, b.center);
            if (!PlaneCrossesBounds(new UnityEngine.Plane(bladeNormal, bladeAtPiece), b)) continue;

            float d = Vector3.Distance(b.ClosestPoint(bladeAtPiece), bladeAtPiece);
            if (d < closestDist) { closestDist = d; ingredient = candidate; cutPos = bladeAtPiece; }
        }
    }

    // Empty parent for one ingredient's pieces, placed at the uncut ingredient's visual centre (not its pivot):
    // hand proximity is measured from here, and it's the point that follows the palm while held.
    private ChoppedIngredient CreateChoppedIngredient(IngredientState whole, Transform parent)
    {
        var go = new GameObject($"Chopped {whole.name}");
        go.transform.SetParent(parent, worldPositionStays: false);
        go.transform.position = whole.GetComponent<Renderer>().bounds.center;
        var chopped = go.AddComponent<ChoppedIngredient>();
        chopped.Init(maxChopsPerIngredient, whole.Type);
        return chopped;
    }

    // Scrubs the sink's front dirty plate. Washing is a two-hand gesture: both gloves must send Wash with both hands
    // at the sink (in one-hand mode, just the one).
    public void OnWashPressed(Hand hand)
    {
        hand = Use(hand);
        if (Slot(hand).item != null || (TwoHands && Slot(hand.Other()).item != null))
        {
            Debug.Log("[Wash] Refused: put down what you're holding first (washing needs empty hands).");
            return;
        }
        string why = "there's no Sink in the scene";
        foreach (var sink in Sink.All)
            if (sink.TryWash(hand, TwoHands, handTrackingManager, arCamera, out why)) return;
        Debug.Log($"[Wash] Refused: {why}.");
    }

    // Pour the cooked pot onto a clean plate. Ways: pot in one hand and a plate in the other -> onto that plate;
    // holding the pot -> onto the clean plate nearest it on screen; holding a plate -> from the pot the hand is at
    // (or that the plate appears over). Refused pours change nothing. Only Pot Hand Pours ticked -> only the pot hand pours.
    // Holding the pot at the bin with no cooked dish and clean plate to pour onto -> pours it all out (a wrong mix, burnt food).
    // Holding a plate with food on it over the bin -> tips the food into the bin.
    public void OnPourPressed(Hand hand)
    {
        hand = Use(hand);
        // Pouring is a two-hand gesture, so both gloves may send it: an empty hand pours with what the other hand holds.
        // The second glove's Pour is then refused as "already pouring", which changes nothing.
        if (!onlyPotHandPours && Slot(hand).item == null && Slot(hand.Other()).item != null) hand = hand.Other();
        var s = Slot(hand);
        var other = Slot(hand.Other());
        if (s.item == null) { Debug.Log("[Pour] Not holding the pot or a plate."); return; }

        PotPour pour = null;
        Plate plate = null;
        if (s.pot != null)
        {
            pour = s.pot.GetComponent<PotPour>();
            if (pour == null) { Debug.LogWarning($"[Pour] {s.pot.name} has no PotPour component."); return; }
            plate = other.item != null && other.item.TryGetComponent(out Plate otherPlate) && otherPlate.CanTakeFood
                ? otherPlate
                : pour.FindPlate(arCamera);
            if ((plate == null || !s.pot.IsReadyToPlate) && pour.FindBin(arCamera) is Bin bin)
            {
                if (pour.TryPourOut(bin, arCamera, out string whyBin)) activePour = pour;
                else Debug.Log($"[Pour] Refused: {whyBin}.");
                return;
            }
        }
        else if (s.item.TryGetComponent(out Plate heldPlate))
        {
            plate = heldPlate;
            if (heldPlate.State == Plate.PlateState.Full && other.pot == null)
            {
                Bin bin = Bin.FindFor(heldPlate.PourPoint, arCamera, heldPlate.transform);
                if (bin == null) { Debug.Log("[Pour] The plate already has food; hold it over the bin to throw it away."); return; }
                if (!bin.TryTipPlate(heldPlate, out string whyTip)) Debug.Log($"[Pour] Refused: {whyTip}.");
                return;
            }
            if (onlyPotHandPours) { Debug.Log("[Pour] Refused: pour with the hand holding the pot, not the plate."); return; }
            if (other.pot != null) pour = other.pot.GetComponent<PotPour>(); // the pot is in the other hand
            else
            {
                foreach (var p in FindObjectsByType<PotPour>(FindObjectsInactive.Exclude))
                {
                    var pot = p.GetComponent<Pot>();
                    if (pot.GetComponent<HandInteractable>().IsTouchedBy(hand) || pot.CoversOnScreen(heldPlate.PourPoint, arCamera)) { pour = p; break; }
                }
                if (pour == null) pour = RecentPotPour(hand); // the hand was at a pot when the gesture was made
            }
            if (pour == null) { Debug.Log("[Pour] Holding a plate, but not at the pot."); return; }
        }
        else { Debug.Log($"[Pour] Can't pour {s.item.name}."); return; }

        if (pour.TryPour(plate, arCamera, out string why)) activePour = pour;
        else Debug.Log($"[Pour] Refused: {why}.");
    }

    void Update()
    {
        // While pouring, the pot and plate stay where the pour started, so the hand's pouring motion
        // (and the glove on the plate hand) doesn't move them and the stream stays on the plate.
        if (activePour != null && activePour.IsPouring) return;
        activePour = null;
        if (Bin.Busy) return; // a plate is tipping into the bin

        foreach (Hand hand in HandExtensions.Both)
            MoveHeldItem(hand);
    }

    // One history sample per hand per frame, after every Update (so hand distances and held items are this frame's).
    // Recorded even while pouring or a plate tips into the bin, so the history has no gaps.
    void LateUpdate()
    {
        if (gestureLookback <= 0f || handTrackingManager == null) return;
        HandInteractable[] all = null; // one scene search per frame, shared by both hands (phone heat)
        foreach (Hand hand in HandExtensions.Both)
        {
            var s = Slot(hand);
            var sample = new HandHistory.Sample { time = Time.time, held = s.item };
            sample.tracked = handTrackingManager.TryGetHandCenterScreenPos(hand, out sample.center);
            if (sample.tracked)
            {
                if (all == null) all = FindObjectsByType<HandInteractable>(FindObjectsInactive.Exclude);
                float best = float.PositiveInfinity;
                foreach (var touch in all)
                {
                    if (touch == null || !touch.enabled || !touch.IsTouchedBy(hand)) continue;
                    if (sample.pot == null && touch.TryGetComponent(out Pot pot)) sample.pot = pot;
                    if (touch.TryGetComponent(out Grabbable g) && g.isHeld) continue;
                    if (touch.DistanceTo(hand) < best) { best = touch.DistanceTo(hand); sample.target = touch; }
                }
            }
            if (s.item != null) sample.heldPosition = s.item.position;
            history.Record(hand, sample);
        }
    }

    // A history target that Grab may still take: not destroyed, active, switched on, not held (e.g. by the other hand).
    private static bool StillGrabbable(HandInteractable target)
    {
        if (target == null || !target.isActiveAndEnabled) return false;
        return !(target.TryGetComponent(out Grabbable g) && g.isHeld);
    }

    // The PotPour of the pot this hand touched within the lookback, newest first, or null.
    private PotPour RecentPotPour(Hand hand)
    {
        foreach (var past in history.Recent(hand, gestureLookback))
            if (past.pot != null && past.pot.isActiveAndEnabled && past.pot.TryGetComponent(out PotPour p))
            {
                LogLate("Pour", hand, past, past.pot.name);
                return p;
            }
        return null;
    }

    // Each late rescue is logged with how old the hand state was: a lower bound on the gesture's delay.
    private int lateRescues;
    private void LogLate(string action, Hand hand, HandHistory.Sample past, string what)
    {
        lateRescues++;
        Debug.Log($"[Late] {action} ({hand} hand) used {what} from {(Time.time - past.time) * 1000f:F0} ms ago " +
            $"(late rescues so far: {lateRescues})");
    }

    // Held items follow the palm centre (it barely moves when the hand closes). Hand lost: the item stays put.
    private void MoveHeldItem(Hand hand)
    {
        var s = Slot(hand);
        if (s.item == null || !handTrackingManager.TryGetHandCenterScreenPos(hand, out Vector2 handCenter)) return;

        Vector2 targetScreenPos = handCenter + s.screenOffset + RefuseShake(s);
        // Follow the real hand depth when LiDAR has a reading; otherwise keep the last depth we had.
        if (handDepth != null && handDepth.HasDepthFor(hand))
        {
            float target = handDepth.DepthOf(hand) - holdInFrontOfHand + s.grabbable.holdBehindHand;
            target = Mathf.Max(target, minHoldDepth, s.grabbable.minHoldDistance);
            s.depth = Mathf.MoveTowards(s.depth, target, maxDepthSpeed * Time.deltaTime);
        }
        s.depth = Mathf.Max(s.depth, s.grabbable.minHoldDistance); // also without LiDAR
        Vector3 worldPos = arCamera.ScreenToWorldPoint(
            new Vector3(targetScreenPos.x, targetScreenPos.y, s.depth)
        );
        s.item.position = worldPos;
    }

    // Closest one this hand touches. Items in the other hand are skipped.
    private HandInteractable FindTouchedInteractable(Hand hand)
    {
        HandInteractable closest = null;
        foreach (var interactable in FindObjectsByType<HandInteractable>(FindObjectsInactive.Exclude))
        {
            if (!interactable.enabled || !interactable.IsTouchedBy(hand)) continue; // switched off = part of a stack, or in the sink
            if (interactable.TryGetComponent(out Grabbable g) && g.isHeld) continue;
            if (closest == null || interactable.DistanceTo(hand) < closest.DistanceTo(hand))
                closest = interactable;
        }
        return closest;
    }

    // The pot the held item is going into, if any. onScreen false: the hand is at it. onScreen true: the item appears
    // over it on screen, which stops an item being released next to the pot (looking as if it went in) when the hand
    // is just outside the pot's touch radius. Only called on a drop. Works with the pot in the other hand too.
    // past = a late gesture's history sample: "hand at the pot" means the pot that hand touched then.
    private Pot PotForDrop(Hand hand, Transform item, HandHistory.Sample? past, bool onScreen)
    {
        bool haveCenter = TryGetBounds(item, out Bounds itemBounds);
        foreach (var pot in FindObjectsByType<Pot>(FindObjectsInactive.Exclude))
        {
            if (!onScreen)
            {
                bool handAtPot = past.HasValue ? past.Value.pot == pot : pot.GetComponent<HandInteractable>().IsTouchedBy(hand);
                if (handAtPot) return pot;
            }
            else if (haveCenter && pot.CoversOnScreen(itemBounds.center, arCamera)) return pot;
        }
        return null;
    }

    // Combined bounds of an item and its pieces.
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

    // Uses where the item appears on screen (camera -> item ray), not its world distance to the board,
    // since the held item's distance from the camera is only an estimate.
    // `refused` = it was over a board that already holds an ingredient (and wasn't over a free one).
    private ChoppingBoard PlaceOnChoppingBoard(Transform item, out bool refused)
    {
        refused = false;
        foreach (var board in FindObjectsByType<ChoppingBoard>(FindObjectsInactive.Exclude))
        {
            var result = board.Place(item, arCamera.transform.position);
            if (result == ChoppingBoard.PlaceResult.Placed) return board;
            if (result == ChoppingBoard.PlaceResult.Occupied) refused = true;
        }
        return null;
    }

    // Same spot on screen as `worldPos`, but at `reference`'s distance from the camera.
    private Vector3 AtDepthOf(Vector3 worldPos, Vector3 reference)
    {
        Vector3 screen = arCamera.WorldToScreenPoint(worldPos);
        screen.z = arCamera.WorldToScreenPoint(reference).z;
        return arCamera.ScreenToWorldPoint(screen);
    }

    private bool PlaneCrossesBounds(UnityEngine.Plane plane, Bounds b)
    {
        Vector3 c = b.center, e = b.extents;
        bool front = false, back = false;
        for (int i = 0; i < 8; i++)
        {
            Vector3 corner = c + new Vector3(
                (i & 1) == 0 ? -e.x : e.x,
                (i & 2) == 0 ? -e.y : e.y,
                (i & 4) == 0 ? -e.z : e.z);
            if (plane.GetSide(corner)) front = true; else back = true;
            if (front && back) return true;
        }
        return false;
    }
}
