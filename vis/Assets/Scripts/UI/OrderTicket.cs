using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

// One order ticket on the HUD, Overcooked style: a timer bar on top, the dish picture, and a strip of ingredient
// icons (Water left out). No text. In the last quarter of its time the frame turns red and the ticket wobbles.
// OrderTicketRow decides which order each ticket shows and where it sits; the ticket animates itself
// (slides in, moves along, pops when served, greys and drops when it expires).
[RequireComponent(typeof(CanvasGroup))]
public class OrderTicket : MonoBehaviour
{
    [Tooltip("The coloured border around the white body.")]
    [SerializeField] private Image frame;
    [Tooltip("Timer bar Image: Image Type Filled, Fill Method Horizontal, Fill Origin Left.")]
    [SerializeField] private Image timerFill;
    [SerializeField] private Image dishImage;
    [Tooltip("The white circles in the strip, left to right. Unused ones are hidden.")]
    [SerializeField] private GameObject[] ingredientSlots;
    [Tooltip("The icon Image inside each circle, same order.")]
    [SerializeField] private Image[] ingredientIcons;

    [Header("Colours")]
    [SerializeField] private Color frameColor = new Color32(0xFC, 0xE7, 0x9A, 0xFF);
    [SerializeField] private Color expiringFrameColor = new Color32(0xE5, 0x48, 0x3D, 0xFF);
    [SerializeField] private Color fullColor = new Color32(0x3F, 0xBF, 0x7F, 0xFF);
    [SerializeField] private Color halfColor = new Color32(0xFF, 0xB2, 0x24, 0xFF);
    [SerializeField] private Color lowColor = new Color32(0xE5, 0x48, 0x3D, 0xFF);
    [Tooltip("Below this fraction of the time left the bar is amber...")]
    [SerializeField, Range(0f, 1f)] private float amberBelow = 0.5f;
    [Tooltip("...and below this, red: red frame and wobble too.")]
    [SerializeField, Range(0f, 1f)] private float redBelow = 0.25f;

    [Header("Motion")]
    [Tooltip("Wobble angle in degrees while red.")]
    [SerializeField] private float wobbleAngle = 4f;
    [Tooltip("Wobbles per second.")]
    [SerializeField] private float wobbleSpeed = 2.5f;
    [Tooltip("How far right a new ticket starts before sliding into place, in HUD pixels.")]
    [SerializeField] private float slideDistance = 300f;
    [Tooltip("How quickly a ticket slides to its place (higher = snappier).")]
    [SerializeField] private float moveSpeed = 10f;
    [SerializeField] private float fadeInTime = 0.2f;
    [Tooltip("Seconds of the served pop / expired drop before the ticket disappears.")]
    [SerializeField] private float exitTime = 0.4f;
    [SerializeField] private float servedPopScale = 1.2f;
    [SerializeField] private Color expiredTint = new Color(0.55f, 0.55f, 0.55f, 1f);
    [Tooltip("How far an expired ticket drops while fading, in HUD pixels.")]
    [SerializeField] private float expiredDrop = 60f;

    public OrderManager.Order Order { get; private set; }
    public bool Free => Order == null && !leaving;

    private RectTransform rect;
    private CanvasGroup group;
    private Vector2 target;
    private bool leaving, leftServed;
    private float exitT;
    private readonly List<Graphic> graphics = new List<Graphic>();
    private readonly Dictionary<Image, Sprite> placeholderSprites = new Dictionary<Image, Sprite>();
    private readonly List<Color> graphicColors = new List<Color>();

    void Awake()
    {
        rect = (RectTransform)transform;
        group = GetComponent<CanvasGroup>();
        group.alpha = 0f;
    }

    // A new order: fills in the pictures and starts sliding in from the right towards `at`.
    public void Show(OrderManager.Order order, Vector2 at, HudIcons icons)
    {
        RestoreColors();
        Order = order;
        leaving = false;
        target = at;
        rect.anchoredPosition = at + new Vector2(slideDistance, 0f);
        rect.localScale = Vector3.one;
        rect.localRotation = Quaternion.identity;
        group.alpha = 0f;

        if (dishImage != null)
            SetIcon(dishImage, icons != null ? icons.Dish(order.Recipe) : null, icons != null ? icons.DishPlaceholder(order.Recipe) : Color.gray);
        int n = 0;
        foreach (var type in order.Recipe.ingredients)
        {
            if (type == IngredientType.Water) continue;
            if (n >= ingredientIcons.Length) { n++; continue; }
            SetIcon(ingredientIcons[n], icons != null ? icons.Ingredient(type) : null, icons != null ? icons.IngredientPlaceholder(type) : Color.gray);
            n++;
        }
        for (int i = 0; i < ingredientSlots.Length; i++)
            if (ingredientSlots[i] != null) ingredientSlots[i].SetActive(i < n);
        if (n > ingredientIcons.Length) Debug.LogWarning($"[HUD] {order.Recipe.name} has more ingredients than the ticket has circles.");
    }

    // A drawn sprite shows untinted; without one, the placeholder shape already in the Image is tinted instead.
    private void SetIcon(Image image, Sprite sprite, Color placeholder)
    {
        if (!placeholderSprites.ContainsKey(image)) placeholderSprites[image] = image.sprite;
        image.sprite = sprite != null ? sprite : placeholderSprites[image];
        image.color = sprite != null ? Color.white : placeholder;
    }

    // The order moved along the row (an older one left).
    public void MoveTo(Vector2 at) => target = at;

    // The order is gone: pop if served, grey and drop if it expired.
    public void Leave(bool served)
    {
        Order = null;
        leaving = true;
        leftServed = served;
        exitT = 0f;
        if (!served) Tint(expiredTint);
    }

    void Update()
    {
        float dt = Time.deltaTime;
        rect.anchoredPosition = Vector2.Lerp(rect.anchoredPosition, target, 1f - Mathf.Exp(-moveSpeed * dt));

        if (leaving)
        {
            exitT += exitTime > 0f ? dt / exitTime : 1f;
            float t = Mathf.Clamp01(exitT);
            group.alpha = 1f - t;
            if (leftServed) rect.localScale = Vector3.one * Mathf.Lerp(1f, servedPopScale, t);
            else target.y -= expiredDrop * (exitTime > 0f ? dt / exitTime : 1f);
            rect.localRotation = Quaternion.Slerp(rect.localRotation, Quaternion.identity, t);
            if (t >= 1f) leaving = false;
            return;
        }

        if (Order == null) return;
        group.alpha = fadeInTime > 0f ? Mathf.MoveTowards(group.alpha, 1f, dt / fadeInTime) : 1f;

        float f = Order.Fraction;
        bool red = f < redBelow;
        if (timerFill != null)
        {
            timerFill.fillAmount = f;
            timerFill.color = red ? lowColor : (f < amberBelow ? halfColor : fullColor);
        }
        if (frame != null) frame.color = red ? expiringFrameColor : frameColor;
        float angle = red ? Mathf.Sin(Time.time * wobbleSpeed * 2f * Mathf.PI) * wobbleAngle : 0f;
        rect.localRotation = Quaternion.Euler(0f, 0f, angle);
    }

    // Expired look: multiplies every Graphic's colour; RestoreColors undoes it for the next order.
    private void Tint(Color tint)
    {
        RestoreColors();
        GetComponentsInChildren(true, graphics);
        graphicColors.Clear();
        foreach (var g in graphics)
        {
            graphicColors.Add(g.color);
            g.color *= tint;
        }
    }

    private void RestoreColors()
    {
        for (int i = 0; i < graphicColors.Count && i < graphics.Count; i++)
            if (graphics[i] != null) graphics[i].color = graphicColors[i];
        graphicColors.Clear();
    }
}
