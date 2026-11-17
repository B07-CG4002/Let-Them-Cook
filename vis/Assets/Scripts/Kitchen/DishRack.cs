using System.Collections.Generic;
using UnityEngine;

// The dish rack: where players get clean plates. It makes Starting Plates real plates from Plate Prefab once, kept
// hidden, and reuses them for the whole round. Grab at the rack and the next one appears in the hand; the sink returns
// each washed plate. The rack's own plate meshes (PlatePile Slots) show how many are left.
[RequireComponent(typeof(HandInteractable))]
public class DishRack : MonoBehaviour
{
    [Tooltip("Your Plate prefab asset (Plate, Grabbable (Plate), HandInteractable, Food child).")]
    [SerializeField] private Plate platePrefab;
    [Tooltip("How many plates exist in the round. No more than the number of PlatePile slots, or the extras don't show on the rack.")]
    [SerializeField] private int startingPlates = 4;
    [Tooltip("Draws the plates on the rack. Empty = the PlatePile on this object, if any.")]
    [SerializeField] private PlatePile pile;

    private readonly Queue<Plate> onRack = new Queue<Plate>();
    public int Count => onRack.Count;

    void Awake()
    {
        if (pile == null) pile = GetComponent<PlatePile>();
        if (platePrefab == null) Debug.LogWarning($"[DishRack] {name} has no plate prefab assigned.");
        else
            for (int i = 0; i < startingPlates; i++)
            {
                var plate = Instantiate(platePrefab); // no parent: stays put in AR world space once out
                plate.name = $"{platePrefab.name} {i + 1}";
                plate.gameObject.SetActive(false);
                onRack.Enqueue(plate);
            }
        Show();
    }

    // A clean plate comes back (washed, or served with Come Back Clean ticked).
    public void Return(Plate plate)
    {
        plate.MakeClean();
        plate.transform.SetParent(null, worldPositionStays: true);
        plate.gameObject.SetActive(false);
        onRack.Enqueue(plate);
        Show();
        Debug.Log($"[DishRack] {plate.name} back, {Count} clean plates");
    }

    // The next plate, shown at the hand (at the rack's distance from the camera; once held it glides to the
    // LiDAR hand depth), or null if the rack is empty.
    public Plate TakePlate(Vector2 handScreenPos, Camera cam)
    {
        if (Count == 0) { Debug.Log($"[DishRack] {name} is empty."); return null; }
        float depth = cam.WorldToScreenPoint(transform.position).z;
        if (depth <= 0f) return null;

        Plate plate = onRack.Dequeue();
        plate.transform.position = cam.ScreenToWorldPoint(new Vector3(handScreenPos.x, handScreenPos.y, depth));
        plate.transform.rotation = transform.rotation * platePrefab.transform.rotation;
        if (plate.TryGetComponent(out HandInteractable touch)) touch.enabled = true;
        plate.gameObject.SetActive(true);
        Show();
        return plate;
    }

    private void Show()
    {
        if (pile != null) pile.Show(Count);
    }
}
