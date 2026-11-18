using UnityEngine;
using UnityEngine.Events;

// Holds the round's score. One in the scene (an empty GameObject named ScoreManager).
// Anything that scores calls ScoreManager.Instance.Add(points, reason): serving (+20 and tip, later),
// burnt food (BurntFood(), once per burnt dish). The future HUD reads Score or listens to onScoreChanged.
public class ScoreManager : MonoBehaviour
{
    public static ScoreManager Instance { get; private set; }

    [Tooltip("Off = the score never goes below 0 (penalties stop at 0).")]
    [SerializeField] private bool allowNegative = false;

    [Tooltip("Points lost per burnt dish (taken once, when it burns). A serve is worth +20.")]
    [SerializeField] private int burntPenalty = 10;

    [Tooltip("Called with (new score, change) whenever the score changes. The HUD and sounds hook in here.")]
    public UnityEvent<int, int> onScoreChanged;

    public int Score { get; private set; }
    public int BurntCount { get; private set; } // dishes burnt this round (end screen)
    public int BurntPenalty => burntPenalty;

    // True = Add does nothing. The round timer will set this when the round ends (or before it starts).
    public bool Frozen { get; set; }

    void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Debug.LogWarning($"[Score] More than one ScoreManager; ignoring the one on {name}.");
            enabled = false;
            return;
        }
        Instance = this;
    }

    void OnDestroy()
    {
        if (Instance == this) Instance = null;
    }

    public void Add(int points, string reason = null)
    {
        if (Frozen || points == 0) return;
        int before = Score;
        Score += points;
        if (!allowNegative && Score < 0) Score = 0;
        int change = Score - before;
        if (change == 0) return; // already at 0, nothing to take
        Debug.Log($"[Score] {(change > 0 ? "+" : "")}{change} {reason} -> {Score}");
        onScoreChanged?.Invoke(Score, change);
    }

    // The pot calls this once each time its food burns.
    public void BurntFood()
    {
        if (Frozen) return;
        BurntCount++;
        Add(-burntPenalty, "burnt");
    }

    // New round.
    public void ResetScore()
    {
        int change = -Score;
        Score = 0;
        BurntCount = 0;
        if (change != 0) onScoreChanged?.Invoke(Score, change);
    }
}
