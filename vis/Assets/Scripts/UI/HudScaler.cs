using UnityEngine;

// On the root of the EyeHUD prefab, a child of LeftDisplay / RightDisplay.
// The HUD is laid out at one eye's full size (1278 x 1179 device pixels); this scales it to whatever size the eye
// image has now (HeadsetEyeLayout's View Scale and Crop change it), so text and panels shrink with the picture.
// Set the RectTransform's anchors and pivot to the middle, position 0 (the prefab builder does this).
[RequireComponent(typeof(RectTransform))]
public class HudScaler : MonoBehaviour
{
    [Tooltip("The size the HUD is laid out at, in HUD pixels.")]
    [SerializeField] private Vector2 referenceSize = new Vector2(1278f, 1179f);

    private RectTransform rect;
    private RectTransform parent;

    void Awake()
    {
        rect = (RectTransform)transform;
        parent = transform.parent as RectTransform;
    }

    // LateUpdate: HeadsetEyeLayout resizes the eye images in its own LateUpdate.
    void LateUpdate()
    {
        if (parent == null || referenceSize.x <= 0f) return;
        Vector2 size = parent.rect.size;
        if (size.x <= 0f || size.y <= 0f) return;
        float scale = size.x / referenceSize.x;
        rect.localScale = new Vector3(scale, scale, 1f);
        rect.sizeDelta = size / scale; // fills the eye image exactly, whatever its shape
    }
}
