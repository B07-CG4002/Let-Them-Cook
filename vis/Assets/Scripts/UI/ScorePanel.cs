using UnityEngine;
using TMPro;

// Bottom-left score panel: the number from ScoreManager, with a small bump whenever it changes.
public class ScorePanel : MonoBehaviour
{
    [SerializeField] private TMP_Text scoreLabel;
    [Tooltip("Size of the number's bump when the score changes.")]
    [SerializeField] private float bumpScale = 1.15f;
    [SerializeField] private float bumpTime = 0.2f;

    private int shown = int.MinValue;
    private float bumpT = 1f;

    void Update()
    {
        int score = ScoreManager.Instance != null ? ScoreManager.Instance.Score : 0;
        if (score != shown)
        {
            if (shown != int.MinValue) bumpT = 0f;
            shown = score;
            if (scoreLabel != null) scoreLabel.text = score.ToString();
        }

        if (bumpT < 1f && scoreLabel != null)
        {
            bumpT = bumpTime > 0f ? Mathf.Min(1f, bumpT + Time.deltaTime / bumpTime) : 1f;
            scoreLabel.transform.localScale = Vector3.one * Mathf.Lerp(bumpScale, 1f, bumpT);
        }
    }
}
