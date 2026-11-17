using UnityEngine;
using UnityEngine.UI;

// Small aim dot at the centre of an eye view: shown only while a hand holds an ingredient, and coloured when a throw
// would land in a pot or on a free board. Put one in each eye's canvas, centred, so both eyes show it.
[RequireComponent(typeof(Graphic))]
public class ThrowCrosshair : MonoBehaviour
{
    [Tooltip("Found automatically in the scene if left empty. None in the scene (menu) = always hidden.")]
    [SerializeField] private GestureButtonController gestureController;
    [SerializeField] private Color noTargetColor = new Color(1f, 1f, 1f, 0.6f);
    [SerializeField] private Color onTargetColor = new Color(0.3f, 1f, 0.3f, 0.9f);

    private Graphic graphic;

    void Awake()
    {
        graphic = GetComponent<Graphic>();
        if (gestureController == null) gestureController = FindAnyObjectByType<GestureButtonController>();
    }

    void Update()
    {
        bool show = gestureController != null && gestureController.HoldingThrowable;
        graphic.enabled = show;
        if (show) graphic.color = gestureController.ThrowTarget != null ? onTargetColor : noTargetColor;
    }
}
