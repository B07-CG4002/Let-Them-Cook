using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;
using TMPro;

// Top-right warning pill: "Pot on fire!" or "Pot burns in 4 s", most urgent first, with a
// flashing border. Fades away when nothing is wrong. The pot is spawned by its marker, so it looks
// for it about once a second rather than every frame.
// Between warnings it shows short notices in Info Color, one after another: "Stove anchored" as each QR marker is
// anchored (it watches QrCodeItemSpawner.Anchors), then "All stations ready!" once RoundTimer's Markers Needed is met.
// While a station can't anchor because it overlaps another (QrCodeItemSpawner.Blocked), it warns "Stove overlaps Sink".
// Until Markers Needed markers are anchored, and nothing else is showing, it asks the player to scan the QR codes.
// While Hide While Open is active (the tutorial's How to play card), the pill stays hidden so it can't cover the card's X.
[RequireComponent(typeof(CanvasGroup))]
public class WarningPill : MonoBehaviour
{
    [SerializeField] private TMP_Text label;
    [Tooltip("The border Image; flashes between Border Color and Flash Color.")]
    [SerializeField] private Image border;
    [SerializeField] private string fireText = "Pot on fire!";
    [Tooltip("{0} = whole seconds left.")]
    [SerializeField] private string burnText = "Pot burns in {0} s";
    [System.Serializable]
    public class MarkerName
    {
        [Tooltip("Exactly as in the Reference Image Library (same as QrCodeItemSpawner's Image Name).")]
        public string imageName;
        public string displayName;
    }

    [Header("Anchoring notices")]
    [Tooltip("Nice names for the notices. A marker not in this list shows its image name.")]
    [SerializeField] private List<MarkerName> markerNames = new List<MarkerName>();
    [Tooltip("{0} = marker name, {1} = markers anchored, {2} = RoundTimer's Markers Needed. E.g. \"{0} anchored ({1}/{2})\".")]
    [SerializeField] private string anchoredText = "{0} anchored";
    [Tooltip("Shown until RoundTimer's Markers Needed markers are anchored, when nothing else is showing. " +
        "{0} = markers anchored, {1} = Markers Needed. Empty = nothing.")]
    [SerializeField] private string scanText = "Start scanning the QR codes! ({0}/{1})";
    [Tooltip("Shown after the last required marker is anchored. Empty = nothing.")]
    [SerializeField] private string allAnchoredText = "All stations ready!";
    [Tooltip("Seconds each notice stays up.")]
    [SerializeField] private float noticeTime = 1.5f;
    [Tooltip("Border colour for notices (steady, no flashing).")]
    [SerializeField] private Color infoColor = new Color32(0x4C, 0xAF, 0x50, 0xFF);

    [Tooltip("Shown while a station overlaps an anchored one. {0} = that station, {1} = the one it overlaps.")]
    [SerializeField] private string overlapText = "{0} overlaps {1}: move the QR";

    [Tooltip("The pill stays hidden while this object is active, e.g. the tutorial's How to play card. Empty = never hide.")]
    [SerializeField] private GameObject hideWhileOpen;

    [Header("Look")]
    [SerializeField] private Color borderColor = new Color32(0xE5, 0x48, 0x3D, 0xFF);
    [SerializeField] private Color flashColor = new Color32(0xFC, 0xE7, 0x9A, 0xFF);
    [Tooltip("Flashes per second.")]
    [SerializeField] private float flashRate = 2f;
    [SerializeField] private float fadeTime = 0.15f;
    [Tooltip("Seconds between looks for the pot.")]
    [SerializeField] private float searchInterval = 1f;

    private CanvasGroup group;
    private Pot pot;
    private float nextSearch;
    private readonly Queue<string> notices = new Queue<string>();
    private float noticeLeft; // seconds the front notice has left
    private bool allAnnounced;
    private int anchorsSeen; // how many of QrCodeItemSpawner.Anchors have had a notice

    void Awake()
    {
        group = GetComponent<CanvasGroup>();
        group.alpha = 0f;
    }

    // Queues a notice for each marker anchored since last frame.
    private void CheckAnchors()
    {
        var anchors = QrCodeItemSpawner.Anchors;
        int needed = RoundTimer.Instance != null ? RoundTimer.Instance.MarkersNeeded : anchors.Count;
        while (anchorsSeen < anchors.Count)
        {
            var anchor = anchors[anchorsSeen++];
            string imageName = anchor != null ? anchor.name.Substring("Anchor ".Length) : "?"; // spawner names them "Anchor <image>"
            notices.Enqueue(string.Format(anchoredText, NiceName(imageName), anchorsSeen, needed));
            if (notices.Count == 1) noticeLeft = noticeTime;
            if (anchorsSeen == needed && !allAnnounced && !string.IsNullOrEmpty(allAnchoredText))
            {
                notices.Enqueue(allAnchoredText);
                allAnnounced = true;
            }
        }
    }

    private string NiceName(string imageName)
    {
        foreach (var m in markerNames)
            if (m.imageName == imageName && !string.IsNullOrEmpty(m.displayName)) return m.displayName;
        return imageName;
    }

    void Update()
    {
        if (Time.time >= nextSearch)
        {
            nextSearch = Time.time + searchInterval;
            if (pot == null) pot = FindAnyObjectByType<Pot>();
        }

        CheckAnchors();
        string warning = Message() ?? OverlapWarning();
        string notice = Notice(); // always called, so notices time out even under a warning
        string message = warning ?? notice ?? ScanPrompt();
        if (hideWhileOpen != null && hideWhileOpen.activeInHierarchy) message = null;
        if (message != null && label != null) label.text = message;
        float step = fadeTime > 0f ? Time.deltaTime / fadeTime : 1f;
        group.alpha = Mathf.MoveTowards(group.alpha, message != null ? 1f : 0f, step);

        if (border != null && warning != null)
            border.color = Mathf.FloorToInt(Time.time * flashRate * 2f) % 2 == 0 ? borderColor : flashColor;
        else if (border != null && message != null)
            border.color = infoColor;
    }

    // The front notice, or null.
    private string Notice()
    {
        if (notices.Count == 0) return null;
        noticeLeft -= Time.deltaTime;
        if (noticeLeft <= 0f)
        {
            notices.Dequeue();
            noticeLeft = noticeTime;
            if (notices.Count == 0) return null;
        }
        return notices.Peek();
    }

    // The scan prompt while markers are still needed, or null.
    private string ScanPrompt()
    {
        if (string.IsNullOrEmpty(scanText)) return null;
        int anchored = QrCodeItemSpawner.Anchors.Count;
        int needed = RoundTimer.Instance != null ? RoundTimer.Instance.MarkersNeeded : 0;
        return anchored < needed ? string.Format(scanText, anchored, needed) : null;
    }

    private string OverlapWarning()
    {
        foreach (var b in QrCodeItemSpawner.Blocked)
            return string.Format(overlapText, NiceName(b.Key), NiceName(b.Value));
        return null;
    }

    private string Message()
    {
        if (pot == null) return null;
        if (pot.IsOnFire) return fireText;
        if (pot.BurnWarning) return string.Format(burnText, Mathf.CeilToInt(pot.BurnTimeLeft));
        return null;
    }
}
