using UnityEngine;
using UnityEngine.UI;

// Hover-to-select: while the fingertip stays on this button, the button fills up left to right (behind its text);
// when it is full the button's SelectableMenuItem.onSelect fires (replaces the Select gesture). Put it next to HandInteractable + SelectableMenuItem.
// Only one button fills at a time: with a hand on each of two buttons, the first one touched fills and the other waits.
[RequireComponent(typeof(HandInteractable), typeof(SelectableMenuItem))]
public class DwellSelect : MonoBehaviour
{
    [Tooltip("Seconds the fingertip must stay on the button.")]
    [SerializeField] private float dwellTime = 2f;
    [Tooltip("Hand tracking drops out for a frame now and then. Leaving for less than this pauses the bar instead of resetting it.")]
    [SerializeField] private float leaveGrace = 0.25f;
    [Tooltip("Empty = the button itself fills up: a copy of its shape in Fill Color grows left to right behind the text. " +
        "If you assign an Image with Image Type = Filled, its Fill Amount is used instead (e.g. a radial ring).")]
    [SerializeField] private Image progressBar;
    [Tooltip("Colour the button fills with.")]
    [SerializeField] private Color fillColor = new Color(1f, 0.698f, 0.141f); // amber #FFB224 (UI_DESIGN accent)

    private HandInteractable interactable;
    private SelectableMenuItem selectable;
    private RectTransform fillClip;  // auto-made fill: a clipping strip whose width is the progress
    private RectTransform fillShape; // the button-shaped image inside it, kept at the button's full size
    private float progress;     // 0-1
    private float timeAway;     // how long the hand has been off the button
    private bool waitForLeave;  // after firing, the hand must leave before the button can fire again
    private static DwellSelect filling; // the one button whose bar is filling, or null

    public float Progress => progress;

    void Awake()
    {
        interactable = GetComponent<HandInteractable>();
        selectable = GetComponent<SelectableMenuItem>();
        if (progressBar == null) CreateFill();
        ShowProgress(0f);
    }

    void OnDisable()
    {
        if (filling == this) filling = null;
        progress = 0f;
        timeAway = 0f;
        waitForLeave = false;
        ShowProgress(0f);
    }

    void Update()
    {
        float dt = Time.unscaledDeltaTime; // keeps working if the game is paused with timeScale = 0

        if (interactable.IsTouched)
        {
            timeAway = 0f;
            if (waitForLeave) return;
            if (filling != null && filling != this) return; // another button is already filling
            filling = this;

            progress += dt / Mathf.Max(dwellTime, 0.01f);
            if (progress >= 1f)
            {
                progress = 0f;
                waitForLeave = true;
                if (filling == this) filling = null;
                ShowProgress(0f);
                selectable.Select();
                return;
            }
        }
        else
        {
            timeAway += dt;
            if (timeAway > leaveGrace)
            {
                progress = 0f;
                waitForLeave = false;
                if (filling == this) filling = null;
            }
        }

        ShowProgress(progress);
    }

    private void ShowProgress(float value)
    {
        if (fillClip != null)
        {
            fillClip.gameObject.SetActive(value > 0f);
            fillClip.anchorMax = new Vector2(value, 1f);
            fillShape.sizeDelta = ((RectTransform)transform).rect.size;
            return;
        }
        if (progressBar == null) return;
        progressBar.enabled = value > 0f;

        if (progressBar.type == Image.Type.Filled && progressBar.sprite != null)
        {
            progressBar.fillAmount = value;
        }
        else
        {
            var rt = progressBar.rectTransform;
            rt.anchorMax = new Vector2(value, rt.anchorMax.y);
        }
    }

    // A copy of the button's own image (same sprite, so same rounded corners) in Fill Color, inside a strip that
    // clips it from the left. It is the first child, so it draws over the button background but under the text.
    private void CreateFill()
    {
        var clipGo = new GameObject("DwellFill", typeof(RectTransform), typeof(RectMask2D));
        fillClip = (RectTransform)clipGo.transform;
        fillClip.SetParent(transform, false);
        fillClip.anchorMin = Vector2.zero;
        fillClip.anchorMax = new Vector2(0f, 1f);
        fillClip.pivot = new Vector2(0f, 0.5f);
        fillClip.offsetMin = Vector2.zero;
        fillClip.offsetMax = Vector2.zero;
        fillClip.SetAsFirstSibling();

        var shapeGo = new GameObject("Shape", typeof(RectTransform), typeof(Image));
        fillShape = (RectTransform)shapeGo.transform;
        fillShape.SetParent(fillClip, false);
        fillShape.anchorMin = fillShape.anchorMax = new Vector2(0f, 0.5f);
        fillShape.pivot = new Vector2(0f, 0.5f);
        fillShape.anchoredPosition = Vector2.zero;

        var img = shapeGo.GetComponent<Image>();
        var button = GetComponent<Image>();
        if (button != null)
        {
            img.sprite = button.sprite;
            img.type = button.type == Image.Type.Filled ? Image.Type.Simple : button.type;
            img.preserveAspect = button.preserveAspect;
            img.pixelsPerUnitMultiplier = button.pixelsPerUnitMultiplier;
        }
        img.color = fillColor;
        img.raycastTarget = false;
    }
}
