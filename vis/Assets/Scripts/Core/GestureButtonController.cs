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
    

    private Transform heldItem;
    private Grabbable heldGrabbable;
    private float heldItemDepth;
    private Vector2 heldScreenOffset; // item's on-screen offset from the fingertip; zero for normal grabs

    public GrabbableType? CurrentlyHeldType => heldGrabbable != null ? heldGrabbable.ItemType : (GrabbableType?)null;
    
    // The public On...Pressed methods are wired to the on-screen buttons' OnClick(); Ultra96 gestures will call them later.
    public void OnSelectPressed()
    {
        var touched = FindTouchedInteractable();
        if (touched != null)
        {
            var selectable = touched.GetComponent<SelectableMenuItem>();
            selectable?.Select();
        }
    }

    public void OnGrabPressed() 
    {
        if (heldItem != null) return;

        var touched = FindTouchedInteractable();
        if (touched == null) return;

        var crate = touched.GetComponent<IngredientCrate>();
        if (crate != null)
        {
            GrabFromCrate(crate);
            return;
        }

        // Grab at a burnt pot takes the burnt food out. Until the bin is built it just disappears.
        var pot = touched.GetComponent<Pot>();
        if (pot != null)
        {
            if (pot.IsBurnt)
            {
                pot.Clear();
                Debug.Log("[Pot] Burnt food taken out (thrown away; no bin yet).");
            }
            return;
        }

        var grabbable = touched.GetComponent<Grabbable>();
        if (grabbable == null) return;

        StartHolding(grabbable, Vector2.zero); // zero offset: sits on the fingertip
    }

    // Grab at a crate: spawn a fresh ingredient beside the hand (around the fist, not inside it) and hold it.
    private void GrabFromCrate(IngredientCrate crate)
    {
        if (!handTrackingManager.TryGetPrimaryHandCenterScreenPos(out Vector2 handCenter))
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

        // Keep the ingredient where it spawned relative to the hand, instead of jumping onto the fingertip.
        Vector2 itemScreen = arCamera.WorldToScreenPoint(item.transform.position);
        StartHolding(grabbable, itemScreen - handTrackingManager.GetPrimaryFingertipScreenPos());
    }

    private void StartHolding(Grabbable grabbable, Vector2 screenOffset)
    {
        heldItem = grabbable.transform;
        heldGrabbable = grabbable;
        grabbable.isHeld = true;
        heldScreenOffset = screenOffset;

        // Picked up, so no longer on the board. Children too: a bunch's pieces each have their own state.
        ChoppingBoard leftBoard = null;
        foreach (var state in heldItem.GetComponentsInChildren<IngredientState>())
        {
            if (state.Board != null) leftBoard = state.Board;
            state.OnChoppingBoard = false;
            state.Board = null; // frees the board it was on
        }

        // The board is empty now, so clear its bar. The chop count stays on the ChoppedIngredient for the next drop.
        if (leftBoard != null)
        {
            var bar = leftBoard.GetComponentInChildren<ChopProgressBar>(true);
            if (bar != null) bar.ResetBar();
        }

        Vector3 screenPos = arCamera.WorldToScreenPoint(heldItem.position);
        heldItemDepth = screenPos.z; // starting depth; Update then moves it toward the LiDAR hand depth
    }
    public void OnDropPressed() 
    {
        if (heldItem == null) return;

        if (heldGrabbable.ItemType == GrabbableType.Ingredient)
        {
            // Hand at the pot, or the item over it on screen: the pot gets it or it stays in hand; the boards aren't tried.
            var pot = PotForDrop();
            if (pot != null)
            {
                DropIntoPot(pot);
                return;
            }

            // Whole ingredient or chopped bunch: every IngredientState on it or its pieces gets the result.
            var board = PlaceOnChoppingBoard(heldItem, out bool refused);
            if (refused)
            {
                // Over a board that already holds an ingredient: keep holding it rather than stacking.
                Debug.Log($"[BoardDrop] {heldItem.name} not dropped: that board already has an ingredient.");
                return;
            }
            foreach (var state in heldItem.GetComponentsInChildren<IngredientState>())
            {
                state.OnChoppingBoard = board != null;
                state.Board = board;
            }

            if (board != null)
            {
                // Show how far this ingredient already got: empty for a whole one, "1/3" for a half-chopped one, "Done!" when finished.
                var bar = board.GetComponentInChildren<ChopProgressBar>(true);
                var chopped = heldItem.GetComponent<ChoppedIngredient>();
                if (bar != null)
                {
                    if (chopped != null) bar.SetProgress(chopped.Chops, chopped.MaxChops);
                    else bar.ResetBar();
                }
            }
            Debug.Log($"[BoardDrop] {heldItem.name} dropped, onChoppingBoard={board != null}");
        }

        heldGrabbable.isHeld = false;
        heldItem = null;
        heldGrabbable = null;
    }

    // Only a fully chopped bunch, or an ingredient that needs no chopping (curry cube), goes in. The pot also refuses
    // what the dish doesn't need. A refused item stays in hand, like a drop on a full board.
    private void DropIntoPot(Pot pot)
    {
        IngredientType type;
        var chopped = heldItem.GetComponent<ChoppedIngredient>();
        var whole = heldItem.GetComponent<IngredientState>();
        if (chopped != null)
        {
            if (!chopped.IsFullyChopped)
            {
                Debug.Log($"[Pot] {heldItem.name} refused: only {chopped.Chops}/{chopped.MaxChops} chopped.");
                return;
            }
            type = chopped.Type;
        }
        else if (whole != null && !IngredientState.NeedsChopping(whole.Type))
        {
            type = whole.Type;
        }
        else
        {
            Debug.Log($"[Pot] {heldItem.name} refused: chop it first.");
            return;
        }

        if (!pot.TryAdd(type, out string why))
        {
            Debug.Log($"[Pot] {heldItem.name} refused: {why}.");
            return;
        }

        // In the pot now: the item itself goes, the pot's surface shows it.
        heldGrabbable.isHeld = false;
        heldItem.gameObject.SetActive(false); // out of the hand-tracking lists at once; Destroy waits for end of frame
        Destroy(heldItem.gameObject);
        heldItem = null;
        heldGrabbable = null;
    }

    public void OnChopPressed()
    {
        Debug.Log($"Chop pressed. HeldGrabbable: {(heldGrabbable != null ? heldGrabbable.ItemType.ToString() : "none")}");

        if (heldGrabbable == null || heldGrabbable.ItemType != GrabbableType.Knife)
        {
            Debug.Log("Not holding knife, aborting.");
            return;
        }

        IngredientState ingredient = null;
        float closestDist = float.PositiveInfinity;
        Transform bladePlane = heldItem.Find("BladePlane");
        Vector3 bladePos = bladePlane != null ? bladePlane.position : heldItem.position;
        Vector3 bladeNormal = bladePlane != null ? bladePlane.up : heldItem.right;
        Vector3 cutPos = bladePos; // blade point moved to the chosen piece's depth

        // The knife is held at the hand's depth, usually in front of the carrot. So, like the rest of the hand
        // interaction, the chop goes by where the blade appears on screen: the blade point is slid along the
        // camera ray to each piece's depth before testing. Only the blade's angle comes from the knife itself.
        foreach (var candidate in FindObjectsByType<IngredientState>(FindObjectsInactive.Exclude))
        {
            if (!candidate.OnChoppingBoard) continue;
            var rend = candidate.GetComponent<Renderer>();
            if (rend == null) continue;

            Bounds b = rend.bounds;
            Vector3 bladeAtPiece = AtDepthOf(bladePos, b.center);
            if (!PlaneCrossesBounds(new UnityEngine.Plane(bladeNormal, bladeAtPiece), b)) continue;

            float d = Vector3.Distance(b.ClosestPoint(bladeAtPiece), bladeAtPiece); // 0 if the blade point is inside the piece
            if (d < closestDist) { closestDist = d; ingredient = candidate; cutPos = bladeAtPiece; }
        }

        if (ingredient == null)
        {
            int onBoard = 0;
            foreach (var c in FindObjectsByType<IngredientState>(FindObjectsInactive.Exclude))
                if (c.OnChoppingBoard) onBoard++;
            Debug.Log($"Blade isn't passing through any choppable piece. Pieces on a board: {onBoard}, " +
                      $"knife distance from camera: {arCamera.WorldToScreenPoint(bladePos).z:F2} m.");
            return;
        }

        // A whole ingredient has no ChoppedIngredient yet; a piece finds the one it belongs to (its parent).
        var chopped = ingredient.GetComponentInParent<ChoppedIngredient>();
        if (chopped != null && chopped.IsFullyChopped)
        {
            Debug.Log($"Fully chopped ({chopped.Chops}/{chopped.MaxChops}).");
            return;
        }

        if (closestDist > 0.05f) // within 5 cm of the piece's box
        {
            Debug.Log($"Nearest piece too far ({closestDist:F3}m) - move knife closer.");
            return;
        }

        Debug.Log($"Chopping nearest ingredient: {ingredient.name}, distance={closestDist:F3}, chopCount={(chopped != null ? chopped.Chops : 0)}");

        Vector3 planePosition = bladePlane != null ? bladePlane.position : ingredient.GetComponent<Renderer>().bounds.center;
        Vector3 planeNormal = bladePlane != null ? bladePlane.up : heldItem.right;

        // Each ingredient can have its own cut face (orange inside a carrot, white inside a potato); else the shared one.
        Material cutFace = ingredient.CrossSectionMaterial != null ? ingredient.CrossSectionMaterial : crossSectionMaterial;
        SlicedHull hull = ingredient.gameObject.Slice(cutPos, bladeNormal, cutFace);
        Debug.Log($"Slice() returned: {(hull != null ? "SUCCESS" : "NULL")}");

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
                Vector3 offsetDirection = (piece == upper) ? planeNormal.normalized : -planeNormal.normalized;
                piece.transform.position += offsetDirection * separationDistance;

                // ...then into the bunch, keeping where it is. From the second chop on these are the same parent.
                piece.transform.SetParent(bunch, worldPositionStays: true);

                var pieceState = piece.AddComponent<IngredientState>();
                pieceState.Type = ingredient.Type;
                pieceState.CrossSectionMaterial = ingredient.CrossSectionMaterial; // so later chops of this piece match
                pieceState.OnChoppingBoard = true;
                pieceState.Board = board; // pieces keep the board occupied
            }

            // Hidden now (Destroy only happens at the end of the frame), so it's out of the bunch's colliders below.
            ingredient.gameObject.SetActive(false);
            Destroy(ingredient.gameObject);

            if (firstChop)
            {
                // Added after the pieces are in, since HandPushable collects its colliders when it's added.
                bunch.gameObject.AddComponent<HandInteractable>();
                bunch.gameObject.AddComponent<Grabbable>().ItemType = GrabbableType.Ingredient;
                bunch.gameObject.AddComponent<HandPushable>(); // hand can bump the bunch (uses the pieces' colliders)
            }
            else
            {
                var pushable = bunch.GetComponent<HandPushable>();
                if (pushable != null) pushable.RefreshColliders(); // swap the cut piece's collider for the two new ones
            }

            if (chopped.IsFullyChopped) Debug.Log("Ingredient fully chopped!");
        }
    }

    // Empty parent for one ingredient's pieces, placed at the uncut ingredient's visual centre (not its pivot):
    // hand proximity is measured from here, and it's the point that follows the fingertip while held.
    private ChoppedIngredient CreateChoppedIngredient(IngredientState whole, Transform parent)
    {
        var go = new GameObject($"Chopped {whole.name}");
        go.transform.SetParent(parent, worldPositionStays: false);
        go.transform.position = whole.GetComponent<Renderer>().bounds.center;
        var chopped = go.AddComponent<ChoppedIngredient>();
        chopped.Init(maxChopsPerIngredient, whole.Type);
        Debug.Log($"[ChoppedGrab] Made {go.name} on the first chop");
        return chopped;
    }

    public void OnWashPressed() { /* next step */ }

    void Update()
    {
        if (heldItem != null)
        {
            Vector2 targetScreenPos = handTrackingManager.GetPrimaryFingertipScreenPos() + heldScreenOffset;
            // Follow the real hand depth when LiDAR has a reading; otherwise keep the last depth we had.
            if (handDepth != null && handDepth.HasDepth)
            {
                float target = Mathf.Max(handDepth.Depth - holdInFrontOfHand, minHoldDepth);
                heldItemDepth = Mathf.MoveTowards(heldItemDepth, target, maxDepthSpeed * Time.deltaTime);
            }
            Vector3 worldPos = arCamera.ScreenToWorldPoint(
                new Vector3(targetScreenPos.x, targetScreenPos.y, heldItemDepth)
            );
            heldItem.position = worldPos;
        }
    }

    // Closest touched one, so a hand near both the crate and a dropped ingredient picks whichever it's nearer.
    private HandInteractable FindTouchedInteractable()
    {
        HandInteractable closest = null;
        foreach (var interactable in FindObjectsByType<HandInteractable>(FindObjectsInactive.Exclude))
        {
            if (interactable.IsTouched && (closest == null || interactable.ClosestHandDistance < closest.ClosestHandDistance))
                closest = interactable;
        }
        return closest;
    }

    // The pot the held item is going into, if any: the hand is at it, or the item appears over it on screen.
    // The second check stops an item being released next to the pot (looking as if it went in) when the hand
    // is just outside the pot's touch radius. Only called on a drop.
    private Pot PotForDrop()
    {
        bool haveCenter = TryGetBounds(heldItem, out Bounds itemBounds);
        foreach (var pot in FindObjectsByType<Pot>(FindObjectsInactive.Exclude))
        {
            if (pot.GetComponent<HandInteractable>().IsTouched) return pot;
            if (haveCenter && pot.CoversOnScreen(itemBounds.center, arCamera)) return pot;
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
            if (front && back) return true; // corners on both sides = plane cuts through
        }
        return false;
    }
}
