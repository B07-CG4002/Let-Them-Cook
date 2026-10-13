using UnityEngine;

// An ingredient crate spawned from its own QR marker. Grab near it and GestureButtonController asks it
// for a fresh ingredient, which appears just beside the player's hand and is then held.
// One crate model serves every ingredient: each crate gets its ingredient prefab and its label material here.
// Runs before other scripts' Awake, so MeshHoverFeedback remembers the label material, not the model's default.
[DefaultExecutionOrder(-10)]
[RequireComponent(typeof(HandInteractable))]
public class IngredientCrate : MonoBehaviour
{
    [Tooltip("Ingredient to hand out. Needs Grabbable (Ingredient), HandInteractable and IngredientState, like the Carrot.")]
    [SerializeField] private GameObject ingredientPrefab;

    [Header("Label")]
    [Tooltip("This crate's material (its label). Empty = keep the model's own material.")]
    [SerializeField] private Material crateMaterial;
    [Tooltip("Renderer to put the material on. Empty = the first Renderer on this object or its children.")]
    [SerializeField] private Renderer crateRenderer;
    [Tooltip("Which material slot gets it, if the model has several (e.g. 0 = wood, 1 = label).")]
    [SerializeField] private int materialSlot = 0;

    // Which ingredient this crate holds, read from the prefab's IngredientState.
    public IngredientType Ingredient { get; private set; }

    void Awake()
    {
        var state = ingredientPrefab != null ? ingredientPrefab.GetComponent<IngredientState>() : null;
        if (state != null) Ingredient = state.Type;
        else Debug.LogWarning($"[Crate] {name}: ingredient prefab missing or has no IngredientState.");

        if (crateMaterial == null) return;
        if (crateRenderer == null) crateRenderer = GetComponentInChildren<Renderer>();
        if (crateRenderer == null) return;

        // sharedMaterials: the label is a material asset, so no per-crate copy is needed.
        var mats = crateRenderer.sharedMaterials;
        if (materialSlot < 0 || materialSlot >= mats.Length)
        {
            Debug.LogWarning($"[Crate] {name}: material slot {materialSlot} doesn't exist (model has {mats.Length}).");
            return;
        }
        mats[materialSlot] = crateMaterial;
        crateRenderer.sharedMaterials = mats;
    }

    [Tooltip("Screen pixels between the hand's centre and the spawned ingredient. " +
        "A random point in this ring is picked, so it lands around the fist rather than inside it.")]
    [SerializeField] private float minSpawnOffset = 40f;
    [SerializeField] private float maxSpawnOffset = 100f;

    // Spawns the ingredient near `handScreenPos` (pixels, bottom-left origin, same space as WorldToScreenPoint).
    // Depth: the hand is at the crate, so the crate's own distance from the camera is the best guess for the
    // hand's depth. Once held, the item glides to the real LiDAR hand depth.
    public GameObject SpawnNear(Vector2 handScreenPos, Camera cam)
    {
        if (ingredientPrefab == null)
        {
            Debug.LogWarning($"[Crate] {name} has no ingredient prefab assigned.");
            return null;
        }

        float depth = cam.WorldToScreenPoint(transform.position).z;
        if (depth <= 0f) return null; // crate is behind the camera

        Vector2 offset = Random.insideUnitCircle.normalized * Random.Range(minSpawnOffset, maxSpawnOffset);
        Vector2 screen = handScreenPos + offset;
        screen.x = Mathf.Clamp(screen.x, 0f, cam.pixelWidth);
        screen.y = Mathf.Clamp(screen.y, 0f, cam.pixelHeight);

        Vector3 worldPos = cam.ScreenToWorldPoint(new Vector3(screen.x, screen.y, depth));
        // Keep the prefab's authored rotation on top of the crate's, same as QrCodeItemSpawner does.
        Quaternion rot = transform.rotation * ingredientPrefab.transform.rotation;

        var item = Instantiate(ingredientPrefab, worldPos, rot); // no parent: stays put in AR world space once dropped
        item.name = ingredientPrefab.name;
        Debug.Log($"[Crate] {name} ({Ingredient}) spawned {item.name} at hand {handScreenPos} + {offset}");
        return item;
    }
}
