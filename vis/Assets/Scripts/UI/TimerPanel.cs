using UnityEngine;
using UnityEngine.UI;
using TMPro;

// Bottom-right round timer: "2:41" and a stopwatch whose yellow wedge shrinks as the round runs out.
// Text and wedge turn red, and the number pulses, in the last Warn Below seconds. Reads RoundTimer in Update.
public class TimerPanel : MonoBehaviour
{
    [SerializeField] private TMP_Text timeLabel;
    [Tooltip("Image Type Filled, Fill Method Radial 360, Fill Origin Top, Clockwise ticked.")]
    [SerializeField] private Image wedge;
    [Tooltip("Seconds left when it turns red.")]
    [SerializeField] private float warnBelow = 10f;
    [SerializeField] private Color textColor = new Color32(0x1F, 0x1B, 0x16, 0xFF);
    [SerializeField] private Color warnTextColor = new Color32(0xC7, 0x37, 0x2D, 0xFF);
    [SerializeField] private Color wedgeColor = new Color32(0xF5, 0xC5, 0x42, 0xFF);
    [SerializeField] private Color warnWedgeColor = new Color32(0xFF, 0x5A, 0x4E, 0xFF);
    [Tooltip("Size of the once-a-second pulse while red. 1 = no pulse.")]
    [SerializeField] private float pulseScale = 1.12f;

    private int shownSeconds = -1;

    void Update()
    {
        var round = RoundTimer.Instance;
        float left = round != null ? round.TimeLeft : 0f;
        float fraction = round != null ? round.Fraction : 0f;
        bool warn = round != null && left <= warnBelow && (round.Running || round.Over);

        int seconds = Mathf.CeilToInt(left);
        if (seconds != shownSeconds && timeLabel != null)
        {
            shownSeconds = seconds;
            timeLabel.text = $"{seconds / 60}:{seconds % 60:00}";
        }
        if (timeLabel != null)
        {
            timeLabel.color = warn ? warnTextColor : textColor;
            // Counting down, the fraction jumps to ~1 as each second ticks over, then shrinks: a beat per second.
            float frac = left - Mathf.Floor(left);
            float pulse = warn && round.Running ? Mathf.Lerp(1f, pulseScale, frac * frac * frac * frac) : 1f;
            timeLabel.transform.localScale = Vector3.one * pulse;
        }
        if (wedge != null)
        {
            wedge.fillAmount = fraction;
            wedge.color = warn ? warnWedgeColor : wedgeColor;
        }
    }
}
