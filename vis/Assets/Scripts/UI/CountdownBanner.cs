using UnityEngine;
using TMPro;

// The pre-round countdown, "3", "2", "1", then "GO!", one under each eye (a child of LeftDisplay / RightDisplay, like
// TimesUpBanner). Reads RoundTimer: shows while RoundTimer.CountingDown, pops in on each new number, then shows Go Text
// for Go Time seconds once the round starts, and fades out. Invisible the rest of the time.
[RequireComponent(typeof(CanvasGroup))]
public class CountdownBanner : MonoBehaviour
{
    [SerializeField] private TMP_Text label;
    [Tooltip("The child that scales on each pop (not the root, which HudScaler sizes). Empty = no pop.")]
    [SerializeField] private RectTransform banner;
    [Tooltip("{0} = whole seconds left, e.g. \"{0}\" or \"Get ready! {0}\".")]
    [SerializeField] private string numberText = "{0}";
    [Tooltip("Shown when the round starts. Empty = no Go.")]
    [SerializeField] private string goText = "GO!";
    [Tooltip("Seconds Go Text stays up.")]
    [SerializeField] private float goTime = 0.8f;
    [SerializeField] private float popTime = 0.3f;
    [SerializeField] private float startScale = 1.6f;
    [SerializeField] private float fadeTime = 0.15f;

    private CanvasGroup group;
    private int shownNumber = -1;
    private bool wasCounting;
    private float goLeft;
    private float poppedAt = -1f;

    void Awake()
    {
        group = GetComponent<CanvasGroup>();
        group.alpha = 0f;
    }

    public void Pop() => poppedAt = Time.unscaledTime;

    void Update()
    {
        var round = RoundTimer.Instance;
        bool counting = round != null && round.CountingDown;
        bool visible = false;

        if (counting)
        {
            int n = Mathf.CeilToInt(round.CountdownLeft);
            if (n != shownNumber)
            {
                shownNumber = n;
                SetText(string.Format(numberText, n));
                Pop();
            }
            goLeft = 0f;
            visible = true;
        }
        else
        {
            // Countdown just ended and the round is running: Go!
            if (wasCounting && round != null && round.Running && !string.IsNullOrEmpty(goText) && goTime > 0f)
            {
                goLeft = goTime;
                SetText(goText);
                Pop();
            }
            shownNumber = -1;
            if (goLeft > 0f)
            {
                goLeft -= Time.unscaledDeltaTime;
                visible = true;
            }
        }
        wasCounting = counting;

        float step = fadeTime > 0f ? Time.unscaledDeltaTime / fadeTime : 1f;
        group.alpha = Mathf.MoveTowards(group.alpha, visible ? 1f : 0f, step);

        if (poppedAt >= 0f && banner != null)
        {
            float t = popTime > 0f ? Mathf.Clamp01((Time.unscaledTime - poppedAt) / popTime) : 1f;
            banner.localScale = Vector3.one * Mathf.LerpUnclamped(startScale, 1f, EaseOutBack(t));
        }
    }

    private void SetText(string text)
    {
        if (label != null) label.text = text;
    }

    // Overshoots a little past 1, then settles.
    private static float EaseOutBack(float t)
    {
        const float c1 = 1.70158f, c3 = c1 + 1f;
        float u = t - 1f;
        return 1f + c3 * u * u * u + c1 * u * u;
    }
}
