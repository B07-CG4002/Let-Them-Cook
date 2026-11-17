using UnityEngine;

// A plate. Pour fills a clean plate with the pot's food; serving empties it and makes it dirty, washing makes it clean.
// The food is the plate's surface model (the child named "Food" unless set): hidden while clean, given the dish's
// material (or tinted with the pot's colour if the dish has none listed), and grown from nothing while the pour runs.
public class Plate : MonoBehaviour
{
    public enum PlateState { Clean, Full, Dirty }

    [System.Serializable]
    public class DishMaterial
    {
        [Tooltip("Exactly as the dish's Name on the Pot's Variations, e.g. Carrot & Potato Soup.")]
        public string dish;
        public Material material;
    }

    [Tooltip("The food surface on the plate. Empty = the child named Food.")]
    [SerializeField] private Transform food;
    [Tooltip("One material per dish. A dish not listed here gets the surface's own material tinted with the pot's colour.")]
    [SerializeField] private DishMaterial[] dishMaterials;
    [Tooltip("Where the pour stream lands. Empty = the food disc, else the plate's centre.")]
    [SerializeField] private Transform pourTarget;
    [Tooltip("Food surface material while the plate is dirty (leftover smears). Empty = a dirty plate looks clean.")]
    [SerializeField] private Material dirtyMaterial;

    public PlateState State { get; private set; } = PlateState.Clean;
    public string Dish { get; private set; } // what's on it while Full, e.g. "Vegetable Soup"
    public bool CanTakeFood => State == PlateState.Clean;

    private Renderer foodRenderer;
    private Material defaultMaterial; // the surface's own material, kept for dishes with none listed
    private Material tintMaterial;    // a copy of it, recoloured per dish
    private Vector3 foodFullScale = Vector3.one;
    private MaterialPropertyBlock dirtBlock; // fades the dirty material without changing the shared asset
    private int dirtColorId;                 // _BaseColor (URP) or _Color (Built-in)
    private Color dirtColor;                 // the Dirty Material's own colour, alpha = fully dirty

    void Awake()
    {
        if (food == null) food = FindChild(transform, "Food");
        if (food != null)
        {
            foodFullScale = food.localScale;
            foodRenderer = food.GetComponent<Renderer>();
            if (foodRenderer != null) defaultMaterial = foodRenderer.sharedMaterial;
        }
        else Debug.LogWarning($"[Plate] {name} has no Food child; pouring still works but nothing shows on the plate.");
        if (dirtyMaterial != null)
        {
            dirtColorId = Shader.PropertyToID(dirtyMaterial.HasProperty("_BaseColor") ? "_BaseColor" : "_Color");
            dirtColor = dirtyMaterial.GetColor(dirtColorId);
        }
        ShowFood(false);
    }

    // The point the pour stream aims at, in world space.
    public Vector3 PourPoint
    {
        get
        {
            if (pourTarget != null) return pourTarget.position;
            if (food != null) return food.position;
            return transform.position;
        }
    }

    // Called by PotPour as the stream starts: the food appears at zero size with the dish's material.
    public void BeginFill(string dish, Color color)
    {
        Dish = dish;
        if (foodRenderer != null)
        {
            foodRenderer.SetPropertyBlock(null); // drop any dirt fade left from the last wash
            Material m = MaterialFor(dish);
            if (m == null)
            {
                if (tintMaterial == null && defaultMaterial != null) tintMaterial = new Material(defaultMaterial);
                if (tintMaterial != null) tintMaterial.color = color;
                m = tintMaterial;
            }
            if (m != null) foodRenderer.sharedMaterial = m;
        }
        ShowFood(true);
        SetFill(0f);
    }

    // 0..1 while the stream runs: the disc grows to its full size.
    public void SetFill(float t)
    {
        if (food != null) food.localScale = foodFullScale * Mathf.SmoothStep(0f, 1f, Mathf.Clamp01(t));
    }

    public void FinishFill()
    {
        SetFill(1f);
        State = PlateState.Full;
        Debug.Log($"[Plate] {name} now has {Dish}");
    }

    // Served: food gone, plate dirty. The food surface stays at full size with the dirty material. Washing calls MakeClean.
    public void TakeFood()
    {
        Dish = null;
        State = PlateState.Dirty;
        bool showDirt = foodRenderer != null && dirtyMaterial != null;
        if (showDirt) foodRenderer.sharedMaterial = dirtyMaterial;
        ShowFood(showDirt);
        if (showDirt) SetFill(1f);
        SetDirt(1f);
    }

    // 1 = fully dirty, 0 = gone. The sink calls this as washing progresses; the Dirty Material must be Transparent to fade.
    public void SetDirt(float amount)
    {
        if (foodRenderer == null || dirtyMaterial == null || State != PlateState.Dirty) return;
        if (dirtBlock == null) dirtBlock = new MaterialPropertyBlock();
        Color c = dirtColor;
        c.a *= Mathf.Clamp01(amount);
        dirtBlock.SetColor(dirtColorId, c);
        foodRenderer.SetPropertyBlock(dirtBlock);
    }

    public void MakeClean()
    {
        Dish = null;
        if (foodRenderer != null) foodRenderer.SetPropertyBlock(null);
        ShowFood(false);
        State = PlateState.Clean;
    }

    private Material MaterialFor(string dish)
    {
        if (dishMaterials != null)
            foreach (var d in dishMaterials)
                if (d.material != null && d.dish == dish) return d.material;
        if (dishMaterials != null && dishMaterials.Length > 0)
            Debug.LogWarning($"[Plate] No material listed for \"{dish}\"; tinting instead. Check the name matches the Pot's Variations.");
        return null;
    }

    private void ShowFood(bool on)
    {
        if (foodRenderer != null) foodRenderer.enabled = on;
        if (food != null) food.localScale = on ? food.localScale : foodFullScale;
    }

    private static Transform FindChild(Transform parent, string childName)
    {
        foreach (Transform child in parent.GetComponentsInChildren<Transform>(true))
            if (child.name == childName) return child;
        return null;
    }
}
