using UnityEngine;

// Caps how many frames per second the whole game renders. Fewer frames = less GPU and CPU work = less heat.
// ARSession's "Match Frame Rate" sets the game to the camera's 60 fps and overrides this, so untick it on the
// ARSession object. Put this on any object in the game scene.
public class FrameRateCap : MonoBehaviour
{
    [Tooltip("Frames per second for the whole game. 60 = smoothest, 30 = much cooler, below 30 looks choppy in a headset.")]
    [SerializeField] private int targetFps = 30;

    void Start()
    {
        QualitySettings.vSyncCount = 0; // iOS ignores vSync, but in the Editor it would override targetFrameRate
        Application.targetFrameRate = targetFps;
    }
}
