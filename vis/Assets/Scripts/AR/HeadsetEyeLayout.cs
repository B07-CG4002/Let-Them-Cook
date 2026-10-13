using UnityEngine;
using UnityEngine.UI;

// Put on the overlay "Canvas" that holds LeftDisplay and RightDisplay. It sizes and places the two eye
// images so each one fits inside the part of the screen you can see through the headset, and keeps the
// picture's real shape (no stretching). Untick the component to go back to the old half-and-half layout.
public class HeadsetEyeLayout : MonoBehaviour
{
    public static HeadsetEyeLayout Active { get; private set; }

    [SerializeField] private RawImage leftDisplay;
    [SerializeField] private RawImage rightDisplay;

    [Tooltip("How much of each half of the screen an eye image may use. Lower it until the whole image fits inside the shell's opening.")]
    [Range(0.3f, 1f)] [SerializeField] private float viewScale = 0.85f;

    [Tooltip("0 = show the whole camera picture (black bars above and below). 1 = fill the eye area and cut off the sides. In between trades one for the other. Never stretches.")]
    [Range(0f, 1f)] [SerializeField] private float crop = 0.5f;

    [Tooltip("Distance between the two lens centres in mm. 0 = centre of each half of the screen.")]
    [SerializeField] private float lensSeparationMm = 0f;

    [Tooltip("Moves both eye images up (+) or down (-) in mm, if the lenses aren't at the screen's vertical centre.")]
    [SerializeField] private float verticalOffsetMm = 0f;

    [Tooltip("Fills the rest of the screen with black, so nothing old shows around the eye images.")]
    [SerializeField] private bool blackBackground = true;

    // GUI coordinates (top-left origin) of each eye image, and which part of the RenderTexture it shows.
    public UnityEngine.Rect LeftEyeGuiRect { get; private set; }
    public UnityEngine.Rect RightEyeGuiRect { get; private set; }
    public UnityEngine.Rect UvRect { get; private set; }

    private struct Saved
    {
        public Vector2 anchorMin, anchorMax, pivot, anchoredPosition, sizeDelta;
        public UnityEngine.Rect uvRect;
    }

    private Saved savedLeft, savedRight;
    private bool saved;
    private GameObject background;

    void OnEnable()
    {
        Active = this;
        if (leftDisplay == null || rightDisplay == null)
            return;

        if (!saved)
        {
            savedLeft = Save(leftDisplay);
            savedRight = Save(rightDisplay);
            saved = true;
        }

        if (blackBackground && Application.isPlaying)
            CreateBackground();

        Apply();
    }

    void OnDisable()
    {
        if (Active == this)
            Active = null;

        if (saved)
        {
            Restore(leftDisplay, savedLeft);
            Restore(rightDisplay, savedRight);
        }

        if (background != null)
            background.SetActive(false);
    }

    // Cheap (a few multiplies), and lets you tune the sliders live in Play mode.
    void LateUpdate()
    {
        Apply();
    }

    private void Apply()
    {
        if (leftDisplay == null || rightDisplay == null)
            return;

        float screenW = Screen.width;
        float screenH = Screen.height;
        // The picture's real shape is the phone screen's, whatever the RenderTexture's pixel size: ARKit
        // draws a screen-shaped view into it (squashed if the texture is narrower, e.g. 1278 x 1179).
        float pictureAspect = screenW / screenH;

        float areaW = screenW * 0.5f * viewScale;
        float areaH = screenH * viewScale;
        float areaAspect = areaW / areaH;

        // The shape actually shown: the whole texture's shape at crop 0, the eye area's shape at crop 1.
        float shownAspect = Mathf.Lerp(pictureAspect, areaAspect, crop);

        // Which part of the texture to show (centred, so the lens centre stays the picture centre).
        UnityEngine.Rect uv;
        if (shownAspect < pictureAspect)
        {
            float w = shownAspect / pictureAspect;
            uv = new UnityEngine.Rect((1f - w) * 0.5f, 0f, w, 1f);
        }
        else
        {
            float h = pictureAspect / shownAspect;
            uv = new UnityEngine.Rect(0f, (1f - h) * 0.5f, 1f, h);
        }

        // The biggest box of that shape that fits in the eye area.
        Vector2 size = shownAspect > areaAspect
            ? new Vector2(areaW, areaW / shownAspect)
            : new Vector2(areaH * shownAspect, areaH);

        float pxPerMm = Screen.dpi > 0f ? Screen.dpi / 25.4f : 0f;
        float halfSeparation = lensSeparationMm > 0f && pxPerMm > 0f
            ? lensSeparationMm * pxPerMm * 0.5f
            : screenW * 0.25f;
        float centerY = screenH * 0.5f + verticalOffsetMm * pxPerMm;

        Vector2 leftCenter = new Vector2(screenW * 0.5f - halfSeparation, centerY);
        Vector2 rightCenter = new Vector2(screenW * 0.5f + halfSeparation, centerY);

        Place(leftDisplay, leftCenter, size, uv);
        Place(rightDisplay, rightCenter, size, uv);

        UvRect = uv;
        LeftEyeGuiRect = ToGuiRect(leftCenter, size, screenH);
        RightEyeGuiRect = ToGuiRect(rightCenter, size, screenH);
    }

    // Positions in screen pixels from the bottom-left corner, converted to canvas units.
    private static void Place(RawImage image, Vector2 centerPx, Vector2 sizePx, UnityEngine.Rect uv)
    {
        var canvas = image.canvas != null ? image.canvas.rootCanvas : null;
        float scale = canvas != null && canvas.scaleFactor > 0f ? canvas.scaleFactor : 1f;

        var rt = image.rectTransform;
        rt.anchorMin = Vector2.zero;
        rt.anchorMax = Vector2.zero;
        rt.pivot = new Vector2(0.5f, 0.5f);
        rt.anchoredPosition = centerPx / scale;
        rt.sizeDelta = sizePx / scale;
        image.uvRect = uv;
    }

    private static UnityEngine.Rect ToGuiRect(Vector2 centerPx, Vector2 sizePx, float screenH)
    {
        return new UnityEngine.Rect(centerPx.x - sizePx.x * 0.5f, screenH - centerPx.y - sizePx.y * 0.5f, sizePx.x, sizePx.y);
    }

    private void CreateBackground()
    {
        if (background == null)
        {
            background = new GameObject("Eye Background", typeof(RectTransform), typeof(Image));
            background.transform.SetParent(leftDisplay.transform.parent, false);
            var rt = (RectTransform)background.transform;
            rt.anchorMin = Vector2.zero;
            rt.anchorMax = Vector2.one;
            rt.offsetMin = Vector2.zero;
            rt.offsetMax = Vector2.zero;
            var image = background.GetComponent<Image>();
            image.color = UnityEngine.Color.black;
            image.raycastTarget = false; // touches must still reach the buttons
        }

        background.transform.SetAsFirstSibling(); // behind the eye images
        background.SetActive(true);
    }

    private static Saved Save(RawImage image)
    {
        var rt = image.rectTransform;
        return new Saved
        {
            anchorMin = rt.anchorMin, anchorMax = rt.anchorMax, pivot = rt.pivot,
            anchoredPosition = rt.anchoredPosition, sizeDelta = rt.sizeDelta, uvRect = image.uvRect
        };
    }

    private static void Restore(RawImage image, Saved s)
    {
        if (image == null)
            return;

        var rt = image.rectTransform;
        rt.anchorMin = s.anchorMin;
        rt.anchorMax = s.anchorMax;
        rt.pivot = s.pivot;
        rt.anchoredPosition = s.anchoredPosition;
        rt.sizeDelta = s.sizeDelta;
        image.uvRect = s.uvRect;
    }
}
