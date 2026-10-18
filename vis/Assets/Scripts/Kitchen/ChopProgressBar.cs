using UnityEngine;
using UnityEngine.UI;
using UnityEngine.XR.ARFoundation;
using TMPro;

// Put this on a World Space Canvas that is a child of the ChoppingBoard in the QR prefab.
// World space (not Screen Space) so Left Camera renders it into the RenderTexture like the 3D content:
// it shows in both eyes and follows the board with no screen-space conversion.
public class ChopProgressBar : MonoBehaviour
{
    [SerializeField] private RectTransform fill;   // child of the background, Pivot X = 0, stretched to full size
    [SerializeField] private Image fillImage;      // the Image on `fill`, recolored when done
    [SerializeField] private TMP_Text label;       // optional, e.g. "1/3" then "Done!"
    [SerializeField] private Color inProgressColor = new Color(1f, 0.75f, 0.2f);
    [SerializeField] private Color doneColor = new Color(0.3f, 0.85f, 0.35f);
    [SerializeField] private bool hideUntilFirstChop = true;

    private Canvas canvas;
    private Camera arCamera;

    void Awake()
    {
        canvas = GetComponent<Canvas>();
        // Prefab spawned from the QR marker can't reference a scene camera, so find the one with ARCameraManager (Left Camera).
        var camManager = FindAnyObjectByType<ARCameraManager>();
        if (camManager != null) arCamera = camManager.GetComponent<Camera>();
        ResetBar();
    }

    void LateUpdate()
    {
        if (arCamera == null) return;
        // Billboard: face the camera, but keep "up" as world up (ARKit's world Y is gravity-aligned)
        // so the bar stays level when the phone rolls between landscape and portrait.
        Vector3 toBar = (transform.position - arCamera.transform.position).normalized;
        if (Mathf.Abs(Vector3.Dot(toBar, Vector3.up)) > 0.99f) return; // camera straight above: keep last rotation
        transform.rotation = Quaternion.LookRotation(toBar, Vector3.up);
    }

    public void SetProgress(int chops, int maxChops)
    {
        float t = maxChops > 0 ? Mathf.Clamp01((float)chops / maxChops) : 0f;
        bool done = chops >= maxChops;

        canvas.enabled = true;
        fill.localScale = new Vector3(t, 1f, 1f);
        if (fillImage != null) fillImage.color = done ? doneColor : inProgressColor;
        if (label != null) label.text = done ? "Done!" : $"{chops}/{maxChops}";

    }

    // For stations other than the board (the pot): any fill amount, colour and label.
    public void Show(float fraction, Color color, string text)
    {
        canvas.enabled = true;
        fill.localScale = new Vector3(Mathf.Clamp01(fraction), 1f, 1f);
        if (fillImage != null) fillImage.color = color;
        if (label != null) label.text = text;
    }

    public void ResetBar()
    {
        fill.localScale = new Vector3(0f, 1f, 1f);
        if (fillImage != null) fillImage.color = inProgressColor;
        if (label != null) label.text = "";
        canvas.enabled = !hideUntilFirstChop;
    }
}
