using System.Collections;
using UnityEngine;
using UnityEngine.UI;
using TMPro;

// The results card in the End scene (EndCard prefab). Reads RoundResults: the lines appear one by one,
// then the total counts up and each star pops in as the total passes its threshold, then "New best!".
// Play again / Menu are ordinary DwellSelect buttons (SelectableMenuItem > SceneLoader.LoadScene).
public class EndScreen : MonoBehaviour
{
    [System.Serializable]
    public class Line
    {
        public GameObject root;
        public TMP_Text label;
        [Tooltip("Empty = no points shown on this line.")]
        public TMP_Text points;
        [Tooltip("{0} = the count.")]
        public string format = "{0}";
        [Tooltip("Ticked: the line stays hidden when its count is 0.")]
        public bool hideIfZero;
    }

    [SerializeField] private Line served = new Line { format = "Dishes served x{0}" };
    [SerializeField] private Line tips = new Line { format = "Tips" };
    [SerializeField] private Line burnt = new Line { format = "Burnt x{0}" };
    [SerializeField] private Line missed = new Line { format = "Orders missed x{0}" };

    [Header("Total")]
    [SerializeField] private GameObject totalRow;
    [SerializeField] private TMP_Text totalLabel;
    [SerializeField] private GameObject newBest;

    [Header("Stars")]
    [SerializeField] private Image[] stars = new Image[3];
    [Tooltip("Points needed for each star, lowest first.")]
    [SerializeField] private int[] starScores = { 20, 30, 60 };
    [SerializeField] private Color starEarned = new Color(0.961f, 0.773f, 0.259f);   // #F5C542
    [SerializeField] private Color starMissing = new Color(0.914f, 0.835f, 0.541f);  // #E9D58A
    [Tooltip("Optional. Set both to swap pictures instead of tinting (then set both colours above to white).")]
    [SerializeField] private Sprite starEarnedSprite;
    [SerializeField] private Sprite starMissingSprite;

    [Header("Timing (seconds)")]
    [SerializeField] private float startDelay = 0.5f;
    [SerializeField] private float lineDelay = 0.4f;
    [SerializeField] private float countTime = 1.2f;
    [SerializeField] private float popScale = 1.4f;
    [SerializeField] private float popTime = 0.2f;

    void Start()
    {
        foreach (var line in new[] { served, tips, burnt, missed })
            if (line.root != null) line.root.SetActive(false);
        if (totalRow != null) totalRow.SetActive(false);
        if (newBest != null) newBest.SetActive(false);
        foreach (var star in stars)
            SetStar(star, false);
        if (!RoundResults.HasResult) Debug.Log("[End] No round played yet (scene opened directly); showing zeros.");
        StartCoroutine(Play());
    }

    private IEnumerator Play()
    {
        yield return new WaitForSecondsRealtime(startDelay);

        if (ShowLine(served, RoundResults.Served, "+" + RoundResults.ServedPoints)) yield return new WaitForSecondsRealtime(lineDelay);
        if (ShowLine(tips, RoundResults.Tips, "+" + RoundResults.Tips)) yield return new WaitForSecondsRealtime(lineDelay);
        if (ShowLine(burnt, RoundResults.Burnt, "-" + RoundResults.BurntPoints)) yield return new WaitForSecondsRealtime(lineDelay);
        if (ShowLine(missed, RoundResults.Missed, "0")) yield return new WaitForSecondsRealtime(lineDelay);

        if (totalRow != null) totalRow.SetActive(true);
        int target = Mathf.Max(0, RoundResults.Score);
        int earned = 0;
        float t = 0f;
        while (true)
        {
            t = countTime > 0f ? Mathf.Min(1f, t + Time.unscaledDeltaTime / countTime) : 1f;
            int shown = Mathf.RoundToInt(target * t);
            if (totalLabel != null) totalLabel.text = shown.ToString();
            while (earned < stars.Length && earned < starScores.Length && shown >= starScores[earned])
            {
                if (stars[earned] != null)
                {
                    SetStar(stars[earned], true);
                    StartCoroutine(Pop(stars[earned].transform));
                }
                earned++;
            }
            if (t >= 1f) break;
            yield return null;
        }

        if (RoundResults.NewBest && newBest != null)
        {
            newBest.SetActive(true);
            StartCoroutine(Pop(newBest.transform));
        }
    }

    private void SetStar(Image star, bool earned)
    {
        if (star == null) return;
        star.color = earned ? starEarned : starMissing;
        var sprite = earned ? starEarnedSprite : starMissingSprite;
        if (sprite != null) star.sprite = sprite;
    }

    // False if the line stays hidden (no wait needed).
    private bool ShowLine(Line line, int count, string points)
    {
        if (line.root == null || (line.hideIfZero && count == 0)) return false;
        if (line.label != null) line.label.text = string.Format(line.format, count);
        if (line.points != null) line.points.text = points;
        line.root.SetActive(true);
        StartCoroutine(Pop(line.root.transform));
        return true;
    }

    private IEnumerator Pop(Transform target)
    {
        for (float t = 0f; t < 1f; t += Time.unscaledDeltaTime / Mathf.Max(popTime, 0.01f))
        {
            target.localScale = Vector3.one * Mathf.Lerp(popScale, 1f, t);
            yield return null;
        }
        target.localScale = Vector3.one;
    }
}
