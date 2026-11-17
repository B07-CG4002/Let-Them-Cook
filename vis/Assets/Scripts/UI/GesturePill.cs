using UnityEngine;
using TMPro;

// Bottom-centre pill: the name of the last glove gesture ("Chop", "Grab"), with a small pop, fading out after
// a moment. With Show Hand ticked it says which hand too ("L Chop", "R Grab"). Listens to CommsBridge.GestureReceived, so both eyes' copies show it at once.
// For one pill per hand, tick One Hand Only and pick the Hand on each copy.
// In the Editor, right-click the component > "Test: Chop" to see it.
[RequireComponent(typeof(CanvasGroup))]
public class GesturePill : MonoBehaviour
{
    [SerializeField] private TMP_Text label;
    [Tooltip("Seconds the name stays fully visible.")]
    [SerializeField] private float holdTime = 1.2f;
    [SerializeField] private float fadeTime = 0.3f;
    [SerializeField] private float popScale = 1.15f;
    [SerializeField] private float popTime = 0.15f;
    [Tooltip("Put L or R in front of the gesture. Untick when playing with one hand.")]
    [SerializeField] private bool showHand = true;
    [Tooltip("Show only the gestures of the Hand below. Untick to show both hands in this one pill.")]
    [SerializeField] private bool oneHandOnly;
    [SerializeField] private Hand hand = Hand.Right;

    private CanvasGroup group;
    private float shownAt = -999f;

    void Awake()
    {
        group = GetComponent<CanvasGroup>();
        group.alpha = 0f;
    }

    void OnEnable() => CommsBridge.GestureReceived += OnGesture;
    void OnDisable() => CommsBridge.GestureReceived -= OnGesture;

    private void OnGesture(CommsBridge.GameAction action, Hand from)
    {
        if (oneHandOnly && from != hand) return;
        Show(showHand ? $"{(from == Hand.Left ? "L" : "R")} {action}" : action.ToString());
    }

    public void Show(string gestureName)
    {
        if (label != null) label.text = gestureName;
        shownAt = Time.time;
    }

    void Update()
    {
        float t = Time.time - shownAt;
        group.alpha = t < holdTime ? 1f : (fadeTime > 0f ? Mathf.Clamp01(1f - (t - holdTime) / fadeTime) : 0f);
        float pop = popTime > 0f ? Mathf.Clamp01(t / popTime) : 1f;
        transform.localScale = Vector3.one * Mathf.Lerp(popScale, 1f, pop);
    }
}
