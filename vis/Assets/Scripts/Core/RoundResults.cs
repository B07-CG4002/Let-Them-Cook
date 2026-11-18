using UnityEngine;

// The last round's numbers, carried from the game scene to the End scene (a static class survives LoadScene).
// RoundEnd fills it when time is up; EndScreen reads it. The best score is kept on the phone (PlayerPrefs).
public static class RoundResults
{
    const string BestKey = "OvercookAR.BestScore";

    public static bool HasResult { get; private set; }
    public static int Score { get; private set; }
    public static int Served { get; private set; }
    public static int ServedPoints { get; private set; }
    public static int Tips { get; private set; }
    public static int Burnt { get; private set; }
    public static int BurntPoints { get; private set; } // points lost, as a positive number
    public static int Missed { get; private set; }      // orders that expired
    public static bool NewBest { get; private set; }
    public static int Best => PlayerPrefs.GetInt(BestKey, 0);

    // Reads ScoreManager and OrderManager in the game scene. Call before leaving it.
    public static void RecordRound()
    {
        var score = ScoreManager.Instance;
        var orders = OrderManager.Instance;
        Score = score != null ? score.Score : 0;
        Burnt = score != null ? score.BurntCount : 0;
        BurntPoints = score != null ? Burnt * score.BurntPenalty : 0;
        Served = orders != null ? orders.Served : 0;
        ServedPoints = orders != null ? Served * orders.ServePoints : 0;
        Tips = orders != null ? orders.Tips : 0;
        Missed = orders != null ? orders.Expired : 0;

        NewBest = Score > 0 && Score > Best;
        if (NewBest)
        {
            PlayerPrefs.SetInt(BestKey, Score);
            PlayerPrefs.Save();
        }
        HasResult = true;
        Debug.Log($"[Results] Score {Score}: served {Served} (+{ServedPoints}), tips +{Tips}, burnt {Burnt} (-{BurntPoints}), missed {Missed}{(NewBest ? ", new best" : "")}");
    }
}
