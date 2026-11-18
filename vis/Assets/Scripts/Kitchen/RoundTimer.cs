using UnityEngine;
using UnityEngine.Events;

// The round clock (3 minutes). One in the scene (an empty GameObject named RoundTimer).
// StartRound resets the score and starts OrderManager; when time is up the orders stop and the score freezes.
// The HUD's TimerPanel reads TimeLeft. It starts once Markers Needed QR markers are anchored (QrCodeItemSpawner.Anchors),
// so scanning the kitchen doesn't eat round time. In Play mode a Countdown Time countdown (3, 2, 1, Go!) runs first,
// so the player can get ready; CountdownBanner shows it. Free play (God mode / Tutorial) starts with no countdown.
// God mode and Tutorial (picked on the menu, see SceneLoader): orders and scoring start once the markers are anchored,
// but no clock runs, orders never expire and food never burns. PlayRound (the Play button) resets the kitchen in
// place and starts a timed round. UnlimitedRound and TutorialRound (in-game Unlimited / Tutorial buttons) do the same
// for free play: switch mode without reloading the scene, so the anchored kitchen stays and nothing is rescanned.
// Every switch clears the old orders at once. In Tutorial, orders start only once the How to play card is closed
// (its X calls InstructionsClosed) and the markers are anchored.
public class RoundTimer : MonoBehaviour
{
    public static RoundTimer Instance { get; private set; }

    public enum GameMode { Play, God, Tutorial }
    // Set by the menu before the game scene loads; stays set across scene loads.
    public static GameMode Mode;
    // No round clock, no order expiry, no burning.
    public static bool GodRules => Mode != GameMode.Play;

    [Tooltip("Seconds in a round.")]
    [SerializeField] private float roundTime = 180f;
    [Tooltip("The round starts once this many QR markers are anchored. 0 = start as soon as the scene starts.")]
    [SerializeField] private int markersNeeded = 6;
    [Tooltip("Play mode: seconds of countdown after the markers are anchored (or Play is pressed), before the clock " +
        "and orders start. 0 = no countdown.")]
    [SerializeField] private float countdownTime = 3f;

    [Header("Events (end screen, sounds)")]
    [Tooltip("At scene start in God mode or Tutorial: show the Play and Menu buttons, hide the timer.")]
    public UnityEvent onFreePlay;
    [Tooltip("At scene start in Tutorial only: show the How to play card.")]
    public UnityEvent onTutorial;
    [Tooltip("When the countdown starts (e.g. a countdown sound).")]
    public UnityEvent onCountdownStarted;
    public UnityEvent onRoundStarted;
    public UnityEvent onRoundOver;

    public float Duration => roundTime;
    public float TimeLeft { get; private set; }
    public float Fraction => roundTime > 0f ? Mathf.Clamp01(TimeLeft / roundTime) : 0f;
    public bool Running { get; private set; }
    public bool Over { get; private set; }
    public int MarkersNeeded => markersNeeded;
    // True during the pre-round countdown; CountdownLeft = seconds until the round starts.
    public bool CountingDown { get; private set; }
    public float CountdownLeft { get; private set; }

    private bool waitingForMarkers;
    private bool waitingForInstructions; // Tutorial: no orders until the How to play card's X is pressed

    void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Debug.LogWarning($"[Round] More than one RoundTimer; ignoring the one on {name}.");
            enabled = false;
            return;
        }
        Instance = this;
        TimeLeft = roundTime;
    }

    void OnDestroy()
    {
        if (Instance == this) Instance = null;
    }

    void Start()
    {
        if (GodRules) onFreePlay?.Invoke();
        if (Mode == GameMode.Tutorial) onTutorial?.Invoke();
        waitingForInstructions = Mode == GameMode.Tutorial;
        waitingForMarkers = QrCodeItemSpawner.Anchors.Count < markersNeeded;
        if (waitingForMarkers) Debug.Log($"[Round] Waiting for {markersNeeded} markers to be anchored");
        else Begin();
    }

    private void Begin()
    {
        if (!GodRules) StartCountdown();
        else if (!waitingForInstructions) StartFreePlay();
        else Debug.Log("[Round] Tutorial: orders start when the How to play card is closed");
    }

    // The How to play card's X (On Select): in Tutorial, orders start now (or once the markers are anchored).
    public void InstructionsClosed()
    {
        if (!waitingForInstructions) return;
        waitingForInstructions = false;
        if (GodRules && !waitingForMarkers) StartFreePlay();
    }

    // Counts down Countdown Time seconds, then StartRound. With Countdown Time 0 the round starts now.
    public void StartCountdown()
    {
        waitingForMarkers = false;
        if (countdownTime <= 0f)
        {
            StartRound();
            return;
        }
        CountingDown = true;
        CountdownLeft = countdownTime;
        Running = false;
        Over = false;
        TimeLeft = roundTime;
        Debug.Log($"[Round] Countdown ({countdownTime:F0}s)");
        onCountdownStarted?.Invoke();
    }

    // Orders and scoring as in a round, but no clock.
    private void StartFreePlay()
    {
        if (ScoreManager.Instance != null) ScoreManager.Instance.ResetScore();
        if (OrderManager.Instance != null) OrderManager.Instance.StartOrders();
        Debug.Log($"[Round] {Mode}: no clock");
    }

    // The Play button in God mode / Tutorial: fresh kitchen in place (no rescan), then a timed round.
    public void PlayRound()
    {
        Mode = GameMode.Play;
        waitingForInstructions = false;
        if (OrderManager.Instance != null) OrderManager.Instance.ClearOrders(); // old tickets go now, new ones at GO!
        if (ScoreManager.Instance != null) ScoreManager.Instance.ResetScore();
        var spawner = FindAnyObjectByType<QrCodeItemSpawner>();
        if (spawner != null) spawner.ResetStations();
        var gbc = FindAnyObjectByType<GestureButtonController>();
        if (gbc != null) gbc.ReleaseHands();
        // Not all stations scanned yet: the clock starts once they are, as when Play is picked on the menu.
        waitingForMarkers = QrCodeItemSpawner.Anchors.Count < markersNeeded;
        CountingDown = false;
        if (!waitingForMarkers) StartCountdown();
        else if (OrderManager.Instance != null) OrderManager.Instance.StopOrders();
    }

    // The in-game Unlimited button: fresh kitchen in place (no rescan), then free play with no clock. Ends a timed
    // round or countdown in progress without the End screen.
    public void UnlimitedRound() => SwitchToFreePlay(GameMode.God);

    // The in-game Tutorial button: as Unlimited, plus On Tutorial (the How to play card).
    public void TutorialRound() => SwitchToFreePlay(GameMode.Tutorial);

    private void SwitchToFreePlay(GameMode mode)
    {
        Mode = mode;
        waitingForInstructions = mode == GameMode.Tutorial;
        if (OrderManager.Instance != null) OrderManager.Instance.ClearOrders();
        if (ScoreManager.Instance != null) ScoreManager.Instance.ResetScore();
        CountingDown = false;
        CountdownLeft = 0f;
        Running = false;
        Over = false;
        TimeLeft = roundTime;
        if (ScoreManager.Instance != null) ScoreManager.Instance.Frozen = false;
        var spawner = FindAnyObjectByType<QrCodeItemSpawner>();
        if (spawner != null) spawner.ResetStations();
        var gbc = FindAnyObjectByType<GestureButtonController>();
        if (gbc != null) gbc.ReleaseHands();
        // Same events, in the same order, as when this mode is loaded from the menu.
        onFreePlay?.Invoke();
        if (mode == GameMode.Tutorial) onTutorial?.Invoke();
        // Not all stations scanned yet: free play starts once they are, as when the mode is picked on the menu.
        waitingForMarkers = QrCodeItemSpawner.Anchors.Count < markersNeeded;
        if (!waitingForMarkers) Begin();
    }

    public void StartRound()
    {
        waitingForMarkers = false; // in case it's called by hand
        CountingDown = false;
        CountdownLeft = 0f;
        TimeLeft = roundTime;
        Running = true;
        Over = false;
        if (ScoreManager.Instance != null)
        {
            ScoreManager.Instance.ResetScore();
            ScoreManager.Instance.Frozen = false;
        }
        if (OrderManager.Instance != null) OrderManager.Instance.StartOrders();
        Debug.Log($"[Round] Started ({roundTime:F0}s)");
        onRoundStarted?.Invoke();
    }

    void Update()
    {
        if (waitingForMarkers)
        {
            if (QrCodeItemSpawner.Anchors.Count < markersNeeded) return;
            waitingForMarkers = false;
            Begin();
        }
        if (CountingDown)
        {
            CountdownLeft -= Time.deltaTime;
            if (CountdownLeft <= 0f) StartRound();
            return;
        }
        if (!Running) return;
        TimeLeft -= Time.deltaTime;
        if (TimeLeft <= 0f) EndRound();
    }

    // Time's up (or call it to end early). Orders stay on screen but stop; the score stops changing.
    public void EndRound()
    {
        TimeLeft = 0f;
        CountingDown = false;
        Running = false;
        Over = true;
        if (OrderManager.Instance != null) OrderManager.Instance.StopOrders();
        if (ScoreManager.Instance != null) ScoreManager.Instance.Frozen = true;
        Debug.Log($"[Round] Over. Score {(ScoreManager.Instance != null ? ScoreManager.Instance.Score : 0)}");
        onRoundOver?.Invoke();
    }
}
