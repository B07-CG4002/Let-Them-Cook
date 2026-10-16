using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using TMPro;
using UnityEngine;

// The ONE object that talks to the comms teammate's CommsNative library.
// It survives scene loads, starts comms once, pulls every waiting gesture each
// frame and turns it into a GestureButtonController action.
//
// The native calls only compile when building for iOS AND the scripting define
// COMMS_NATIVE is set (Player Settings > iOS > Scripting Define Symbols).
// Without it the script still runs (Editor, or iOS builds without CommsNative)
// but does nothing native, so those builds keep linking.
// COMMS_NEXT_RESULT (second define) turns on gesture results; add it once
// CommsNextResult exists in CommsNative. Without it you can still test the
// connection: Connect button + status text work.
public class CommsBridge : MonoBehaviour
{
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE
    [DllImport("__Internal")] static extern void CommsStart();
    [DllImport("__Internal")] static extern int CommsCopyDisplay(byte[] buffer, int capacity);
    [DllImport("__Internal")] static extern void CommsStop();
#if COMMS_NEXT_RESULT
    [DllImport("__Internal")] static extern int CommsNextResult(byte[] buffer, int capacity);
#endif
    const bool NativeAvailable = true;
#else
    const bool NativeAvailable = false;
#endif

    public enum GameAction { None, Grab, Drop, Chop, Wash, Pour, Throw, Extinguish }

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
        // Final labels (Wen Ree to confirm)
        new LabelMapping { label = "PICKUPHOLD", action = GameAction.Grab },
        new LabelMapping { label = "PLACEDOWN",  action = GameAction.Drop },
        new LabelMapping { label = "CHOP",       action = GameAction.Chop },
        new LabelMapping { label = "WASH",       action = GameAction.Wash },
        new LabelMapping { label = "THROW",      action = GameAction.Throw },
        new LabelMapping { label = "FIREEXT",    action = GameAction.Extinguish },
        // Today's test labels, so the pipe can be tried end to end
        new LabelMapping { label = "FIST",  action = GameAction.Grab },
        new LabelMapping { label = "OPEN",  action = GameAction.Drop },
        new LabelMapping { label = "POINT", action = GameAction.Chop },
        new LabelMapping { label = "REST",  action = GameAction.None },
    };

    [Tooltip("Results below this confidence are ignored.")]
    [Range(0f, 1f)] public float minConfidence = 0f;

    [Tooltip("The same label again within this many seconds is ignored. 0 = every result counts.")]
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

    static CommsBridge instance;

    byte[] resultBuffer = new byte[512];
    readonly byte[] displayBuffer = new byte[2048];
    string lastLabel;
    float lastLabelTime = -999f;

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
#if UNITY_IOS && !UNITY_EDITOR && COMMS_NATIVE && COMMS_NEXT_RESULT
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
        HandleGesture(r.gesture);
    }

    // Public so you can fake a gesture in the Editor, e.g. from a test button.
    public void HandleGesture(string label)
    {
        label = label.Trim().ToUpperInvariant();

        if (repeatCooldown > 0f && label == lastLabel && Time.time - lastLabelTime < repeatCooldown)
            return;
        lastLabel = label;
        lastLabelTime = Time.time;

        GameAction action = GameAction.None;
        foreach (var m in mappings)
            if (string.Equals(m.label, label, StringComparison.OrdinalIgnoreCase)) { action = m.action; break; }
        if (action == GameAction.None) return;

        // The bridge outlives scenes; the controller doesn't, so re-find it.
        if (gestureController == null)
            gestureController = FindAnyObjectByType<GestureButtonController>();
        if (gestureController == null) return; // e.g. in the menu scene

        switch (action)
        {
            case GameAction.Grab: gestureController.OnGrabPressed(); break;
            case GameAction.Drop: gestureController.OnDropPressed(); break;
            case GameAction.Chop: gestureController.OnChopPressed(); break;
            case GameAction.Wash: gestureController.OnWashPressed(); break;
            // By name, so this compiles before pour/ is applied.
            case GameAction.Pour: gestureController.SendMessage("OnPourPressed", SendMessageOptions.DontRequireReceiver); break;
            case GameAction.Throw:      // throw-gesture/ not applied yet
            case GameAction.Extinguish: // extinguisher not built yet
                Debug.Log($"CommsBridge: {label} -> {action} has no game action yet");
                break;
        }
    }
}
