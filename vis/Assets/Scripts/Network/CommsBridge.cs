using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using TMPro;
using UnityEngine;

// The ONE object that talks to the comms teammate's CommsNative library.
// It survives scene loads, starts comms once, pulls every waiting gesture each
// frame and turns it into a GestureButtonController action for the hand that glove is on (by device_id).
//
// The native calls only compile when building for iOS AND the scripting define
// COMMS_NATIVE is set (Player Settings > iOS > Scripting Define Symbols).
// Without it the script still runs (Editor, or iOS builds without CommsNative)
// but does nothing native, so those builds keep linking.
// COMMS_NEXT_RESULT (second define) turns on gesture results; add it once
// CommsNextResult exists in CommsNative. Without it you can still test the
// connection: Connect button + status text work.
// COMMS_HAND_EVENTS (third define) reads hand events INSTEAD of results, plus each hand's current state
// (CHOP/WASH/POURING) for the sink. Needs a CommsNative and Ultra96 service that have hand events.
public class CommsBridge : MonoBehaviour
{
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE
    [DllImport("__Internal")] static extern void CommsStart();
    [DllImport("__Internal")] static extern int CommsCopyDisplay(byte[] buffer, int capacity);
    [DllImport("__Internal")] static extern void CommsStop();
#if COMMS_NEXT_RESULT
    [DllImport("__Internal")] static extern int CommsNextResult(byte[] buffer, int capacity);
#endif
#if COMMS_HAND_EVENTS
    [DllImport("__Internal")] static extern int CommsNextHandEvent(byte[] buffer, int capacity);
    [DllImport("__Internal")] static extern int CommsHandState(byte[] buffer, int capacity);
#endif
    const bool NativeAvailable = true;
#else
    const bool NativeAvailable = false;
#endif

    public enum GameAction { None, Grab, Drop, Chop, Wash, Pour, Throw, Extinguish }

    // Every recognised glove gesture, as its game action and the hand it acts on. The HUD's GesturePill shows its name.
    public static event Action<GameAction, Hand> GestureReceived;

    [Serializable]
    public class LabelMapping
    {
        public string label;
        public GameAction action;
    }

    [Header("Gesture labels -> game actions")]
    [Tooltip("Label text from the Ultra96 (case-insensitive) and what it does. Unknown labels are ignored.")]
    public List<LabelMapping> mappings = new List<LabelMapping>
    {
        new LabelMapping { label = "PICKUPHOLD", action = GameAction.Grab },
        new LabelMapping { label = "PLACEDOWN",  action = GameAction.Drop },
        new LabelMapping { label = "CHOP",       action = GameAction.Chop },
        new LabelMapping { label = "WASH",       action = GameAction.Wash },
        new LabelMapping { label = "THROW",      action = GameAction.Throw },
        new LabelMapping { label = "FIREEXT",    action = GameAction.Extinguish },
        new LabelMapping { label = "POURING",    action = GameAction.Pour },
        new LabelMapping { label = "IDLE",       action = GameAction.None },
    };

    [Serializable]
    public class DeviceHand
    {
        public int deviceId;
        public Hand hand;
    }

    [Header("Gloves -> hands")]
    [Tooltip("Which hand each glove is on, by the device_id in its results. With Two Hands unticked on " +
        "HandTrackingManager every glove acts on the one hand anyway.")]
    public List<DeviceHand> deviceHands = new List<DeviceHand>
    {
        // As the comms dashboard shows them (2026-10-22)
        new DeviceHand { deviceId = 1, hand = Hand.Left },
        new DeviceHand { deviceId = 2, hand = Hand.Right },
    };
    [Tooltip("Hand for a device_id that isn't in the list, and for HandleGesture(label) without one.")]
    public Hand defaultHand = Hand.Right;

    [Tooltip("Results below this confidence are ignored.")]
    [Range(0f, 1f)] public float minConfidence = 0f;

    [Tooltip("The same label again from the same glove within this many seconds is ignored. 0 = every result counts.")]
    public float repeatCooldown = 0f;

    [Header("Optional status text")]
    [Tooltip("Shows CommsNative's 4-line status. Leave empty to hide.")]
    public TMP_Text statusText;

    [Tooltip("Found automatically in each scene if left empty.")]
    public GestureButtonController gestureController;

    [Serializable]
    class Result
    {
        public string result_id;
        public string gesture;
        public float confidence = 1f;
        public int device_id;
    }

    [Serializable]
    class HandEvent
    {
        public string kind;    // pulse, state_start, state_refresh, state_end, idle, reset
        public string gesture;
        public float confidence = 1f;
        public int device_id;
    }

    [Serializable]
    class HandStates { public HandStateEntry[] hands; }

    [Serializable]
    class HandStateEntry
    {
        public int device_id;
        public string gesture; // CHOP, WASH, POURING, or null
    }

    // True when the game reads hand events, so each hand's state is known.
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE && COMMS_HAND_EVENTS
    public static bool HandStatesOn => instance != null;
#else
    public static bool HandStatesOn => false;
#endif

    // The ongoing action of each hand (Left, Right) from the latest hand state, e.g. "WASH". Null = none.
    static readonly string[] handState = new string[2];

    public static bool HandIsDoing(Hand hand, string gesture) =>
        string.Equals(handState[(int)hand], gesture, StringComparison.OrdinalIgnoreCase);

    static CommsBridge instance;

    byte[] resultBuffer = new byte[512];
    readonly byte[] displayBuffer = new byte[2048];
    readonly Dictionary<int, string> lastLabel = new Dictionary<int, string>();  // per device_id
    readonly Dictionary<int, float> lastLabelTime = new Dictionary<int, float>();

    void Awake()
    {
        // Exactly one bridge for the whole app.
        if (instance != null && instance != this) { Destroy(gameObject); return; }
        instance = this;
        DontDestroyOnLoad(gameObject);
    }

    void Start()
    {
        if (instance != this) return;
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE
        CommsStart(); // adds the Connect button at the top right
#endif
        if (!NativeAvailable && statusText != null)
            statusText.text = "Comms off (Editor or no COMMS_NATIVE)";
    }

    void Update()
    {
        if (instance != this) return;
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE && COMMS_HAND_EVENTS
        // Each hand's state first, so a Wash below already sees the other hand's.
        int stateLen = CommsHandState(resultBuffer, resultBuffer.Length);
        if (stateLen < 0) resultBuffer = new byte[-stateLen];
        else ReadHandStates(Encoding.UTF8.GetString(resultBuffer, 0, stateLen));

        // Take every waiting hand event, oldest first.
        while (true)
        {
            int n = CommsNextHandEvent(resultBuffer, resultBuffer.Length);
            if (n == 0) break;
            if (n < 0) { resultBuffer = new byte[-n]; continue; } // too small: grow and retry
            HandleHandEvent(Encoding.UTF8.GetString(resultBuffer, 0, n));
        }
#elif UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE && COMMS_NEXT_RESULT
        // Take every waiting result, oldest first.
        while (true)
        {
            int n = CommsNextResult(resultBuffer, resultBuffer.Length);
            if (n == 0) break;
            if (n < 0) { resultBuffer = new byte[-n]; continue; } // too small: grow and retry
            HandleJson(Encoding.UTF8.GetString(resultBuffer, 0, n));
        }
#endif
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE

        if (statusText != null)
        {
            int len = CommsCopyDisplay(displayBuffer, displayBuffer.Length);
            if (len > 0) statusText.text = Encoding.UTF8.GetString(displayBuffer, 0, len);
        }
#endif
    }

    void OnApplicationQuit()
    {
        if (instance != this) return;
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE
        CommsStop(); // only on quit, never on scene change or backgrounding
#endif
    }

    void HandleJson(string json)
    {
        Result r;
        try { r = JsonUtility.FromJson<Result>(json); }
        catch (Exception e) { Debug.LogWarning($"CommsBridge: bad JSON {json} ({e.Message})"); return; }
        if (r == null || string.IsNullOrEmpty(r.gesture)) return;
        if (r.confidence < minConfidence) return;
        HandleGesture(r.gesture, r.device_id);
    }

    // A pulse (PICKUP, DROP, THROW) acts once. A state (CHOP, WASH, POURING) acts on its start and on each refresh
    // (about every 0.2 s), as the repeated results did; its end needs nothing, the repeats just stop.
    void HandleHandEvent(string json)
    {
        HandEvent e;
        try { e = JsonUtility.FromJson<HandEvent>(json); }
        catch (Exception ex) { Debug.LogWarning($"CommsBridge: bad JSON {json} ({ex.Message})"); return; }
        if (e == null || string.IsNullOrEmpty(e.gesture)) return;
        if (e.kind != "pulse" && e.kind != "state_start" && e.kind != "state_refresh") return;
        if (e.confidence < minConfidence) return;
        HandleGesture(e.gesture, e.device_id);
    }

    void ReadHandStates(string json)
    {
        HandStates s;
        try { s = JsonUtility.FromJson<HandStates>(json); }
        catch (Exception ex) { Debug.LogWarning($"CommsBridge: bad hand state {json} ({ex.Message})"); return; }
        handState[0] = handState[1] = null;
        if (s?.hands == null) return;
        foreach (var h in s.hands)
            handState[(int)HandFor(h.device_id)] = string.IsNullOrEmpty(h.gesture) ? null : h.gesture;
    }

    public Hand HandFor(int deviceId)
    {
        foreach (var d in deviceHands)
            if (d.deviceId == deviceId) return d.hand;
        return defaultHand;
    }

    // Public so you can fake a gesture in the Editor, e.g. from a test button. No device_id = Default Hand.
    public void HandleGesture(string label) => HandleGesture(label, int.MinValue);

    public void HandleGesture(string label, int deviceId)
    {
        label = label.Trim().ToUpperInvariant();

        if (repeatCooldown > 0f && lastLabel.TryGetValue(deviceId, out string last) && label == last
            && Time.time - lastLabelTime[deviceId] < repeatCooldown)
            return;
        lastLabel[deviceId] = label;
        lastLabelTime[deviceId] = Time.time;

        GameAction action = GameAction.None;
        foreach (var m in mappings)
            if (string.Equals(m.label, label, StringComparison.OrdinalIgnoreCase)) { action = m.action; break; }
        if (action == GameAction.None) return;

        // The bridge outlives scenes; the controller doesn't, so re-find it.
        if (gestureController == null)
            gestureController = FindAnyObjectByType<GestureButtonController>();

        Hand hand = deviceId == int.MinValue ? defaultHand : HandFor(deviceId);
        if (gestureController != null && !gestureController.TwoHands) hand = Hand.Right; // one hand mode
        GestureReceived?.Invoke(action, hand);
        if (gestureController == null) return; // e.g. in the menu scene

        switch (action)
        {
            case GameAction.Grab: gestureController.OnGrabPressed(hand); break;
            case GameAction.Drop: gestureController.OnDropPressed(hand); break;
            case GameAction.Chop: gestureController.OnChopPressed(hand); break;
            case GameAction.Wash: gestureController.OnWashPressed(hand); break;
            case GameAction.Pour: gestureController.OnPourPressed(hand); break;
            case GameAction.Throw: gestureController.OnThrowPressed(hand); break;
            case GameAction.Extinguish: // extinguisher not built yet
                Debug.Log($"CommsBridge: {label} -> {action} has no game action yet");
                break;
        }
    }
}
