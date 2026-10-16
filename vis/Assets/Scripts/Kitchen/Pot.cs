using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Events;

// The pot, starting on the stove marker. It can be grabbed and carried (Grabbable, type Pot); see PotStation.
// GestureButtonController calls TryAdd on a Drop with the hand at the pot.
// The first ingredient picks the dish; the pot then only takes what that dish still needs. When a stage has all its
// ingredients it cooks, then a burn countdown starts (the curry's next ingredient must go in before it ends).
// The soup surface (a child plane) rises one step per ingredient. Timers run in Update; no per-frame searches.
[RequireComponent(typeof(HandInteractable))]
public class Pot : MonoBehaviour
{
    [System.Serializable]
    public class Stage
    {
        public List<IngredientType> ingredients = new List<IngredientType>();
        public Color color = Color.white; // surface colour while this stage is raw/cooking
    }

    [System.Serializable]
    public class Recipe
    {
        public string name;
        public List<Stage> stages = new List<Stage>();
    }

    public enum PotState { Empty, Filling, Cooking, Cooked, Burnt }

    [Tooltip("The dishes. The recipe whose first stage contains the first ingredient dropped in is the one cooked.")]
    [SerializeField] private List<Recipe> recipes = DefaultRecipes();
    [SerializeField] private float cookTime = 10f;
    [Tooltip("Seconds from cooked to burnt (the grace period between curry stages).")]
    [SerializeField] private float burnTime = 10f;
    [Tooltip("Last seconds of the burn countdown in which the bar flashes red.")]
    [SerializeField] private float warningTime = 5f;

    [Header("Soup surface")]
    [Tooltip("The plane inside the pot. Empty = the child named SoupSurface.")]
    [SerializeField] private Transform soupSurface;
    [Tooltip("Surface local Y with nothing in the pot (it is hidden then).")]
    [SerializeField] private float emptyY;
    [Tooltip("Surface local Y with every ingredient of the dish in.")]
    [SerializeField] private float fullY;
    [SerializeField] private float riseDuration = 0.3f;
    [Tooltip("How much darker the surface gets once a stage is cooked (0 = same colour, 1 = black).")]
    [SerializeField, Range(0f, 1f)] private float cookedDarken = 0.3f;
    [SerializeField] private Color burntColor = new Color(0.08f, 0.08f, 0.08f);

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

    public PotState State { get; private set; } = PotState.Empty;
    public Recipe CurrentRecipe => recipe;
    // Every stage done and not burnt: what plating will check.
    public bool IsReadyToPlate => State == PotState.Cooked && IsLastStage;
    public bool IsBurnt => State == PotState.Burnt;
    public bool IsOnFire { get; private set; }
    private bool stoveFireOn;
    // The stove/sink it's sitting on; null while held or put down elsewhere. Set by PotStation.
    public PotStation Station { get; set; }

    private Recipe recipe;
    private int stageIndex;
    private readonly List<IngredientType> stageAdded = new List<IngredientType>();
    private int ingredientsIn;
    private int totalIngredients;
    private float timer;      // counts down while Cooking (cook) and Cooked (burn)
    private bool warned;
    private string cookedLabel;

    private Renderer surfaceRenderer;
    private Material surfaceMaterial;
    private float surfaceFrom, surfaceTo, surfaceT = 1f;

    private bool IsLastStage => recipe != null && stageIndex == recipe.stages.Count - 1;
    private Stage CurrentStage => recipe.stages[stageIndex];

    private static List<Recipe> DefaultRecipes() => new List<Recipe>
    {
        new Recipe
        {
            name = "Vegetable Soup",
            stages = { new Stage { ingredients = { IngredientType.Carrot, IngredientType.Potato }, color = new Color(1f, 0.6f, 0.15f) } }
        },
        new Recipe
        {
            name = "Beef Curry",
            stages =
            {
                new Stage { ingredients = { IngredientType.Beef }, color = new Color(0.55f, 0.3f, 0.2f) },
                new Stage { ingredients = { IngredientType.CurryCube }, color = new Color(0.45f, 0.25f, 0.08f) }
            }
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

        if (bar == null) bar = GetComponentInChildren<ChopProgressBar>(true);
        StopFire(); // in case Play On Awake was left on
        if (stoveFire != null) stoveFire.Stop(true, ParticleSystemStopBehavior.StopEmittingAndClear);
    }

    // In Start, not Awake: the bar sets itself up in its own Awake, which may run after this one.
    void Start() => Clear();

    // Returns false (and why) when the pot won't take it; the caller keeps it in the hand.
    public bool TryAdd(IngredientType type, out string why)
    {
        why = null;
        switch (State)
        {
            case PotState.Empty:
                recipe = FindRecipeStartingWith(type);
                if (recipe == null) { why = $"no dish starts with {type}"; return false; }
                stageIndex = 0;
                stageAdded.Clear();
                ingredientsIn = 0;
                totalIngredients = 0;
                foreach (var stage in recipe.stages) totalIngredients += stage.ingredients.Count;
                break;

            case PotState.Filling:
                if (!StageStillNeeds(type)) { why = $"the {recipe.name} doesn't need (more) {type}"; return false; }
                break;

            case PotState.Cooked:
                // Only during a grace period, and only what the next stage needs. This also ends the burn countdown.
                if (IsLastStage || !recipe.stages[stageIndex + 1].ingredients.Contains(type))
                {
                    why = IsLastStage ? $"the {recipe.name} is already cooked" : $"the {recipe.name} needs {NextStageList()} now";
                    return false;
                }
                stageIndex++;
                stageAdded.Clear();
                break;

            case PotState.Cooking:
                why = "it's still cooking"; return false;
            default:
                why = "it's burnt"; return false;
        }

        stageAdded.Add(type);
        ingredientsIn++;
        RiseTo(Mathf.Lerp(emptyY, fullY, totalIngredients > 0 ? (float)ingredientsIn / totalIngredients : 1f));

        if (stageAdded.Count >= CurrentStage.ingredients.Count)
        {
            State = PotState.Cooking;
            timer = cookTime;
        }
        else State = PotState.Filling;

        SetSurfaceColor(CurrentStage.color);
        RefreshBar();
        Debug.Log($"[Pot] {type} in ({recipe.name}, {ingredientsIn}/{totalIngredients}), now {State}");
        return true;
    }

    // True if `worldPos` appears on screen inside the pot's outline (its meshes' bounds, projected). Only called on a drop.
    public bool CoversOnScreen(Vector3 worldPos, Camera cam) => ScreenBounds.Covers(transform, worldPos, cam);

    // Back to an empty pot. Plating and grabbing burnt food out will call this.
    public void Clear()
    {
        State = PotState.Empty;
        recipe = null;
        stageIndex = 0;
        stageAdded.Clear();
        ingredientsIn = 0;
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
        if (surfaceT < 1f)
        {
            surfaceT = riseDuration > 0f ? surfaceT + Time.deltaTime / riseDuration : 1f;
            SetSurfaceY(Mathf.Lerp(surfaceFrom, surfaceTo, Mathf.SmoothStep(0f, 1f, surfaceT)));
        }

        if (State == PotState.Cooking)
        {
            timer -= Time.deltaTime;
            if (timer <= 0f)
            {
                State = PotState.Cooked;
                timer = burnTime;
                warned = false;
                cookedLabel = IsLastStage ? "Ready!" : $"Add {NextStageList()}!";
                SetSurfaceColor(Color.Lerp(CurrentStage.color, Color.black, cookedDarken));
                Debug.Log($"[Pot] {recipe.name} stage {stageIndex + 1}/{recipe.stages.Count} cooked");
                onCooked?.Invoke();
            }
            RefreshBar();
        }
        else if (State == PotState.Cooked)
        {
            timer -= Time.deltaTime;
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

    private void RefreshBar()
    {
        if (bar == null) return;
        switch (State)
        {
            case PotState.Filling:
                bar.Show((float)stageAdded.Count / CurrentStage.ingredients.Count, cookingColor, "Need " + MissingList());
                break;
            case PotState.Cooking:
                bar.Show(1f - timer / cookTime, cookingColor, "Cooking");
                break;
            case PotState.Cooked:
                // Drains over the burn countdown; flashes red for the last warningTime seconds.
                bool flashOn = timer <= warningTime && Mathf.FloorToInt(Time.time / flashInterval) % 2 == 0;
                string label = timer <= warningTime && IsLastStage ? "Burning!" : cookedLabel;
                bar.Show(timer / burnTime, flashOn ? warningColor : (timer <= warningTime ? Color.white : cookedColor), label);
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
        bool on = (State == PotState.Cooking || State == PotState.Cooked || State == PotState.Burnt) && Station != null && Station.Kind == PotStationKind.Stove;
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

    private Recipe FindRecipeStartingWith(IngredientType type)
    {
        foreach (var r in recipes)
            if (r.stages.Count > 0 && r.stages[0].ingredients.Contains(type)) return r;
        return null;
    }

    // Allows duplicates only where the stage lists the same ingredient twice.
    private bool StageStillNeeds(IngredientType type)
    {
        int needed = 0, have = 0;
        foreach (var t in CurrentStage.ingredients) if (t == type) needed++;
        foreach (var t in stageAdded) if (t == type) have++;
        return have < needed;
    }

    private string MissingList()
    {
        var missing = new List<IngredientType>(CurrentStage.ingredients);
        foreach (var t in stageAdded) missing.Remove(t);
        return JoinNames(missing);
    }

    private string NextStageList() => JoinNames(recipe.stages[stageIndex + 1].ingredients);

    private static string JoinNames(List<IngredientType> types)
    {
        var sb = new StringBuilder();
        for (int i = 0; i < types.Count; i++)
        {
            if (i > 0) sb.Append(i == types.Count - 1 ? " and " : ", ");
            sb.Append(types[i] == IngredientType.CurryCube ? "Curry Cube" : types[i].ToString());
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
