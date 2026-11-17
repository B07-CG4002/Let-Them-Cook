using System.Collections.Generic;
using UnityEngine;

// The TIME'S UP! banner, one under each eye (TimesUpBanner prefab, a child of LeftDisplay / RightDisplay, next to EyeHUD).
// Invisible until RoundEnd calls ShowAll; then the dim fades in and the banner drops in with a small bounce.
// In the Editor, right-click the component > "Test: Show" to see it.
[RequireComponent(typeof(CanvasGroup))]
public class TimesUpBanner : MonoBehaviour
{
    private static readonly List<TimesUpBanner> all = new List<TimesUpBanner>();

    [Tooltip("The tilted banner itself; it scales down into place.")]
    [SerializeField] private RectTransform banner;
    [SerializeField] private float popTime = 0.35f;
    [SerializeField] private float startScale = 1.6f;

    private CanvasGroup group;
    private float shownAt = -1f; // -1 = hidden

    void Awake()
    {
        group = GetComponent<CanvasGroup>();
        group.alpha = 0f;
    }

    void OnEnable() => all.Add(this);
    void OnDisable() => all.Remove(this);

    public static void ShowAll()
    {
        foreach (var b in all) b.Show();
    }

    [ContextMenu("Test: Show")]
    public void Show() => shownAt = Time.unscaledTime;

    void Update()
    {
        if (shownAt < 0f) return;
        float t = popTime > 0f ? Mathf.Clamp01((Time.unscaledTime - shownAt) / popTime) : 1f;
        group.alpha = Mathf.Clamp01(t * 2f);
        if (banner != null) banner.localScale = Vector3.one * Mathf.LerpUnclamped(startScale, 1f, EaseOutBack(t));
    }

    // Overshoots a little past 1, then settles.
    private static float EaseOutBack(float t)
    {
        const float c1 = 1.70158f, c3 = c1 + 1f;
        float u = t - 1f;
        return 1f + c3 * u * u * u + c1 * u * u;
    }
}
