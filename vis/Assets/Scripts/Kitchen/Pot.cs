using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Events;

// The pot, starting on the stove marker. It can be grabbed and carried (Grabbable, type Pot); see PotStation.
// GestureButtonController calls TryAdd on a Drop with the hand at the pot.
// Recipes are ingredient sets (variations), max 4, in any order. The pot takes any ingredient that isn't in yet
// (max 4), even one no order needs. Once the contents exactly match a variation, ordered or not, it cooks (one stage),
// then a burn countdown starts. A dish nobody ordered can be plated, but the serve window refuses it.
// A mix that is no variation never cooks, so the player has to pour it out at the sink (PotPour).
// The soup surface (a child plane) rises a quarter per ingredient.
// Water: put down on the sink station, the pot gets its water at once. Cooking and the burn countdown only run
// while the pot sits on the stove station; off it they pause. Timers run in Update; no per-frame searches.
[RequireComponent(typeof(HandInteractable))]
public class Pot : MonoBehaviour
{
    public enum RecipeType { Soup, Curry }

    [System.Serializable]
    public class Recipe
    {
        public string name;
        public RecipeType type;
        public List<IngredientType> ingredients = new List<IngredientType>();
        public Color color = new Color(1f, 0.6f, 0.15f); // surface colour once this dish is cooking
    }

    public enum PotState { Empty, Filling, Cooking, Cooked, Burnt }

    public const int MaxIngredients = 4;

    [Tooltip("Every variation in the game; OrderManager picks its orders from these. With no OrderManager, all of them count as active orders.")]
    [SerializeField] private List<Recipe> variations = DefaultVariations();
    [SerializeField] private float cookTime = 5f;
    [Tooltip("Seconds from cooked to burnt.")]
    [SerializeField] private float burnTime = 7f;
    [Tooltip("Last seconds of the burn countdown in which the bar flashes red.")]
    [SerializeField] private float warningTime = 5f;

    [Header("Soup surface")]
    [Tooltip("The plane inside the pot. Empty = the child named SoupSurface.")]
    [SerializeField] private Transform soupSurface;
    [Tooltip("Surface local Y with nothing in the pot (it is hidden then).")]
    [SerializeField] private float emptyY;
    [Tooltip("Surface local Y with 4 ingredients in (a 3-ingredient soup stops at three quarters).")]
    [SerializeField] private float fullY;
    [SerializeField] private float riseDuration = 0.3f;
    [Tooltip("How much darker the surface gets once cooked (0 = same colour, 1 = black).")]
    [SerializeField, Range(0f, 1f)] private float cookedDarken = 0.3f;
    [SerializeField] private Color burntColor = new Color(0.08f, 0.08f, 0.08f);
    [Tooltip("Surface colour while the pot holds only water.")]
    [SerializeField] private Color waterColor = new Color(0.55f, 0.8f, 1f);
    [Tooltip("Surface colour while filling, before a curry cube is in.")]
    [SerializeField] private Color soupColor = new Color(1f, 0.6f, 0.15f);
    [Tooltip("Surface colour while filling, once a curry cube is in.")]
    [SerializeField] private Color curryColor = new Color(0.5f, 0.28f, 0.1f);

    [Header("Bar")]
    [Tooltip("A ChopProgressBar canvas above the pot. Empty = the first one in the children.")]
    [SerializeField] private ChopProgressBar bar;
    [SerializeField] private Color cookingColor = new Color(1f, 0.75f, 0.2f);
    [SerializeField] private Color cookedColor = new Color(0.3f, 0.85f, 0.35f);
    [SerializeField] private Color warningColor = new Color(0.95f, 0.15f, 0.1f);
    [SerializeField] private float flashInterval = 0.25f;

    [Header("Fire")]
    [Tooltip("Fire particle system: a child of the pot, Play On Awake off. Plays when the food burns, stops when the pot is emptied.")]
    [SerializeField] private ParticleSystem fire;
    [Tooltip("Seconds after the food burns before the fire starts. 0 = straight away (the game design suggests 8).")]
    [SerializeField] private float fireDelay = 0f;

    [Tooltip("Burner flame on the stove (in the Stove prefab), Play On Awake off. Plays while the pot is on the stove with all its ingredients in "
        + "(cooking, cooked or burnt); stops when the pot is emptied or taken off the stove.")]
    [SerializeField] private ParticleSystem stoveFire;

    [Header("Events (sounds)")]
    public UnityEvent onCooked;
    public UnityEvent onWarning;
    public UnityEvent onBurnt;
    public UnityEvent onWaterAdded;

    public PotState State { get; private set; } = PotState.Empty;
    // The dish being cooked; null until the contents exactly match a variation (an active order first).
    public Recipe CurrentRecipe => recipe;
    public IReadOnlyList<IngredientType> Contents => contents;
    public IReadOnlyList<Recipe> Variations => variations;
    public bool IsReadyToPlate => State == PotState.Cooked;
    public bool IsBurnt => State == PotState.Burnt;
    public bool IsOnFire { get; private set; }
    // For the HUD's warning pill: cooked, on the stove and in the last Warning Time seconds before it burns.
    public bool BurnWarning => State == PotState.Cooked && OnStove && timer <= warningTime;
    public float BurnTimeLeft => State == PotState.Cooked ? Mathf.Max(0f, timer) : 0f;
    private bool stoveFireOn;
    // The stove/sink it's sitting on; null while held or put down elsewhere. Set by PotStation.
    public PotStation Station { get; set; }
    // With no stove station in the scene at all the pot cooks wherever it is, as before the stove existed.
    public bool OnStove => (Station != null && Station.Kind == PotStationKind.Stove) || !AnyStove();

    private Recipe recipe;
    private readonly List<IngredientType> contents = new List<IngredientType>();
    private List<Recipe> activeOrders; // null = every variation counts as an order
    private float timer;      // counts down while Cooking (cook) and Cooked (burn), only on a stove
    private bool warned;

    private Renderer surfaceRenderer;
    private Material surfaceMaterial;
    private float surfaceFrom, surfaceTo, surfaceT = 1f;

    private static List<Recipe> DefaultVariations() => new List<Recipe>
    {
        new Recipe
        {
            name = "Carrot & Potato Soup", type = RecipeType.Soup,
            ingredients = { IngredientType.Carrot, IngredientType.Potato, IngredientType.Water },
            color = new Color(1f, 0.6f, 0.15f)
        },
        new Recipe
        {
            name = "Carrot & Potato Curry", type = RecipeType.Curry,
            ingredients = { IngredientType.Carrot, IngredientType.Potato, IngredientType.CurryCube, IngredientType.Water },
            color = new Color(0.6f, 0.35f, 0.1f)
        },
        new Recipe
        {
            name = "Carrot & Beef Curry", type = RecipeType.Curry,
            ingredients = { IngredientType.Carrot, IngredientType.Beef, IngredientType.CurryCube, IngredientType.Water },
            color = new Color(0.45f, 0.22f, 0.08f)
        }
    };

    void Awake()
    {
        if (soupSurface == null) soupSurface = FindChild(transform, "SoupSurface");
        if (soupSurface != null)
        {
            surfaceRenderer = soupSurface.GetComponent<Renderer>();
            if (surfaceRenderer != null) surfaceMaterial = surfaceRenderer.material; // own copy, so recolouring doesn't touch the asset
        }
        else Debug.LogWarning($"[Pot] {name} has no SoupSurface child; the pot works but nothing rises.");
        if (Mathf.Approximately(emptyY, fullY)) Debug.LogWarning("[Pot] Empty Y and Full Y are the same, so the surface won't rise.");

        foreach (var v in variations)
            if (v.ingredients.Count > MaxIngredients) Debug.LogWarning($"[Pot] {v.name} has more than {MaxIngredients} ingredients; it can never be made.");

        if (bar == null) bar = GetComponentInChildren<ChopProgressBar>(true);
        StopFire(); // in case Play On Awake was left on
        if (stoveFire != null) stoveFire.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
    }

    // In Start, not Awake: the bar sets itself up in its own Awake, which may run after this one.
    // The pot is spawned by its QR marker, so it tells OrderManager it exists (which starts the orders' dishes).
    void Start()
    {
        Clear();
        if (OrderManager.Instance != null) OrderManager.Instance.SetPot(this);
    }

    // From OrderManager: the variations currently on order. null = all variations (no OrderManager in the scene).
    // Ingredients already in stay in; contents that now exactly match a new order start cooking.
    public void SetActiveOrders(List<Recipe> orders)
    {
        activeOrders = orders;
        if (State == PotState.Filling) TryStartCooking();
        RefreshBar();
    }

    // Would the pot take this now? Same answer TryAdd gives, without changing anything.
    public bool CanAdd(IngredientType type, out string why)
    {
        why = null;
        switch (State)
        {
            case PotState.Empty:
            case PotState.Filling:
            case PotState.Cooking: // turns it into another dish (a curry cube into a soup) or spoils it into a wrong mix
                break;
            case PotState.Cooked:
                why = $"the {recipe.name} is already cooked"; return false;
            default:
                why = "it's burnt"; return false;
        }
        if (contents.Count >= MaxIngredients) { why = "the pot is full"; return false; }
        if (contents.Contains(type)) { why = $"there's already {Name(type)} in"; return false; }
        return true;
    }

    // Returns false (and why) when the pot won't take it; the caller keeps it in the hand.
    public bool TryAdd(IngredientType type, out string why)
    {
        if (!CanAdd(type, out why)) return false;

        bool wasCooking = State == PotState.Cooking;
        contents.Add(type);
        State = PotState.Filling;
        RiseTo(Mathf.Lerp(emptyY, fullY, (float)contents.Count / MaxIngredients));
        TryStartCooking();
        if (State == PotState.Filling) recipe = null;

        SetSurfaceColor(FillColor());
        RefreshBar();
        Debug.Log($"[Pot] {type} in ({JoinNames(contents)}), now {State}" +
                  (recipe != null ? $" {recipe.name}" : "") +
                  (wasCooking ? " (cook restarted)" : "") +
                  (State == PotState.Cooking && !OnStove ? ", waiting for the stove" : "") +
                  (State == PotState.Filling && !AnyOrderFits(contents) ? ", fits no order" : ""));
        return true;
    }

    // Contents exactly match a variation (an active order first): start the 10 s cook (from the start, if it was already cooking).
    private void TryStartCooking()
    {
        Recipe match = ExactMatch() ?? ExactVariation();
        if (match == null) return;
        recipe = match;
        State = PotState.Cooking;
        timer = cookTime;
        SetSurfaceColor(recipe.color);
    }

    // True if `worldPos` appears on screen inside the pot's outline (its meshes' bounds, projected). Only called on a drop.
    public bool CoversOnScreen(Vector3 worldPos, Camera cam) => ScreenBounds.Covers(transform, worldPos, cam);

    // Back to an empty pot. Plating, pouring out at the sink and grabbing burnt food out call this.
    public void Clear()
    {
        State = PotState.Empty;
        recipe = null;
        contents.Clear();
        warned = false;
        surfaceT = 1f;
        if (soupSurface != null) SetSurfaceY(emptyY);
        if (surfaceRenderer != null) surfaceRenderer.enabled = false;
        if (bar != null) bar.ResetBar();
        StopFire();
    }

    void Update()
    {
        UpdateStoveFire();
        if (Station != null && Station.Kind == PotStationKind.Sink) FillAtSink();

        if (surfaceT < 1f)
        {
            surfaceT = riseDuration > 0f ? surfaceT + Time.deltaTime / riseDuration : 1f;
            SetSurfaceY(Mathf.Lerp(surfaceFrom, surfaceTo, Mathf.SmoothStep(0f, 1f, surfaceT)));
        }

        if (State == PotState.Cooking)
        {
            if (OnStove) timer -= Time.deltaTime; // off the stove: paused
            if (timer <= 0f)
            {
                State = PotState.Cooked;
                timer = burnTime;
                warned = false;
                SetSurfaceColor(Color.Lerp(recipe.color, Color.black, cookedDarken));
                Debug.Log($"[Pot] {recipe.name} cooked");
                onCooked?.Invoke();
            }
            RefreshBar();
        }
        else if (State == PotState.Cooked)
        {
            if (OnStove && !RoundTimer.GodRules) timer -= Time.deltaTime; // taking it off the heat stops it burning; never burns in God mode / Tutorial
            if (!warned && timer <= warningTime)
            {
                warned = true;
                onWarning?.Invoke();
            }
            if (timer <= 0f)
            {
                State = PotState.Burnt;
                SetSurfaceColor(burntColor);
                Debug.Log($"[Pot] {recipe.name} burnt");
                onBurnt?.Invoke();
                if (ScoreManager.Instance != null) ScoreManager.Instance.BurntFood();
                timer = fireDelay; // now counts down to the fire
                if (timer <= 0f) StartFire();
            }
            RefreshBar();
        }
        else if (State == PotState.Burnt && !IsOnFire)
        {
            timer -= Time.deltaTime;
            if (timer <= 0f) StartFire();
        }
    }

    // On the sink without water yet: the water goes straight in. Checked every frame while on the sink;
    // after that CanAdd says no (water already in), so it only happens once.
    private void FillAtSink()
    {
        if (!CanAdd(IngredientType.Water, out _)) return;
        TryAdd(IngredientType.Water, out _);
        onWaterAdded?.Invoke();
    }

    private void RefreshBar()
    {
        if (bar == null) return;
        // Off the stove the cooking bar hides (timers are paused too).
        if ((State == PotState.Cooking || State == PotState.Cooked) && !OnStove)
        {
            bar.ResetBar();
            return;
        }
        switch (State)
        {
            // No bar while ingredients go in: dishes have 3 or 4 ingredients, so a fill amount would mislead.
            // The rising surface shows the filling instead.
            case PotState.Cooking:
                bar.Show(1f - timer / cookTime, cookingColor, "Cooking");
                break;
            case PotState.Cooked:
                // Drains over the burn countdown; flashes red for the last warningTime seconds.
                bool warning = timer <= warningTime;
                bool flashOn = warning && Mathf.FloorToInt(Time.time / flashInterval) % 2 == 0;
                bar.Show(timer / burnTime, flashOn ? warningColor : (warning ? Color.white : cookedColor), warning ? "Burning!" : "Ready!");
                break;
            case PotState.Burnt:
                bar.Show(1f, burntColor, "Burnt!");
                break;
            default:
                bar.ResetBar();
                break;
        }
    }

    private void StartFire()
    {
        IsOnFire = true;
        if (fire != null) fire.Play(true); // true = child particle systems (smoke, sparks) too
        Debug.Log("[Pot] On fire");
    }

    private void UpdateStoveFire()
    {
        bool on = (State == PotState.Cooking || State == PotState.Cooked || State == PotState.Burnt) && OnStove;
        if (on == stoveFireOn) return;
        stoveFireOn = on;
        if (stoveFire == null) return;
        if (on) stoveFire.Play(true);
        else stoveFire.Stop(true, ParticleSystemStopBehavior.StopEmitting); // last flames fade out
    }

    private void StopFire()
    {
        IsOnFire = false;
        if (fire != null) fire.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
    }

    private IEnumerable<Recipe> Orders() => activeOrders ?? variations;

    // Everything in `have` is part of the recipe (each at most as often as it lists it).
    private static bool Fits(Recipe r, List<IngredientType> have)
    {
        var left = new List<IngredientType>(r.ingredients);
        foreach (var t in have)
            if (!left.Remove(t)) return false;
        return true;
    }

    private bool AnyOrderFits(List<IngredientType> have)
    {
        foreach (var r in Orders()) if (Fits(r, have)) return true;
        return false;
    }

    private Recipe ExactMatch()
    {
        foreach (var r in Orders())
            if (r.ingredients.Count == contents.Count && Fits(r, contents)) return r;
        return null;
    }

    // Exact match with any variation, ordered or not.
    private Recipe ExactVariation()
    {
        foreach (var r in variations)
            if (r.ingredients.Count == contents.Count && Fits(r, contents)) return r;
        return null;
    }

    private Color FillColor()
    {
        if (recipe != null) return recipe.color;
        if (contents.Contains(IngredientType.CurryCube)) return curryColor;
        if (contents.Count == 1 && contents[0] == IngredientType.Water) return waterColor;
        return soupColor;
    }

    private static bool AnyStove()
    {
        foreach (var s in PotStation.All) if (s.Kind == PotStationKind.Stove) return true;
        return false;
    }

    private static string Name(IngredientType type) => type == IngredientType.CurryCube ? "Curry Cube" : type.ToString();

    private static string JoinNames(List<IngredientType> types)
    {
        if (types.Count == 0) return "nothing";
        var sb = new StringBuilder();
        for (int i = 0; i < types.Count; i++)
        {
            if (i > 0) sb.Append(i == types.Count - 1 ? " and " : ", ");
            sb.Append(Name(types[i]));
        }
        return sb.ToString();
    }

    private void RiseTo(float y)
    {
        if (soupSurface == null) return;
        if (surfaceRenderer != null) surfaceRenderer.enabled = true;
        surfaceFrom = soupSurface.localPosition.y;
        surfaceTo = y;
        surfaceT = 0f;
    }

    private void SetSurfaceY(float y)
    {
        Vector3 p = soupSurface.localPosition;
        p.y = y;
        soupSurface.localPosition = p;
    }

    private void SetSurfaceColor(Color c)
    {
        if (surfaceMaterial != null) surfaceMaterial.color = c;
    }

    private static Transform FindChild(Transform parent, string childName)
    {
        foreach (Transform child in parent.GetComponentsInChildren<Transform>(true))
            if (child.name == childName) return child;
        return null;
    }
}
