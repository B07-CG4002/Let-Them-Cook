using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

// The orders on screen. One in the scene (an empty GameObject named OrderManager).
// Keeps a fixed number of slots (2): each holds a random variation from the Pot's Variations with a 60 s timer.
// Served or expired, the slot empties and a new random order fills it after a short wait.
// Tells the pot which variations are on order (Pot.SetActiveOrders), so the pot only takes what fits one.
// ServeStation calls TryServe; serving scores +20 and a tip of up to +10 through ScoreManager.
public class OrderManager : MonoBehaviour
{
    public class Order
    {
        public Pot.Recipe Recipe;
        public float Duration;
        public float TimeLeft;
        public float CreatedAt; // Time.time; the oldest matching order is served first
        public float Fraction => Duration > 0f ? Mathf.Clamp01(TimeLeft / Duration) : 0f;
    }

    public static OrderManager Instance { get; private set; }

    [Tooltip("The pot. Its Variations are the dishes that can be ordered. Leave empty: the pot hands itself over when its marker spawns it.")]
    [SerializeField] private Pot pot;
    [Tooltip("Orders on screen at once.")]
    [SerializeField] private int slotCount = 2;
    [Tooltip("Seconds each order lasts before it expires.")]
    [SerializeField] private float orderTime = 60f;
    [Tooltip("Seconds before a new order fills a slot that was served or expired.")]
    [SerializeField] private float refillDelay = 2f;
    [Tooltip("Seconds before the first orders appear.")]
    [SerializeField] private float firstOrderDelay = 0f;
    [SerializeField] private int servePoints = 20;
    [Tooltip("Tip for a fast serve: this many points with the whole timer left, scaling down to 0 as it runs out.")]
    [SerializeField] private int maxTip = 10;
    [Tooltip("Start taking orders as soon as the scene starts. Untick once the round timer exists; it will call StartOrders.")]
    [SerializeField] private bool startOnPlay = true;

    [Header("Events (HUD, sounds)")]
    public UnityEvent<int> onOrderAdded;        // slot
    public UnityEvent<int, int> onOrderServed;  // slot, points
    public UnityEvent<int> onOrderExpired;      // slot

    public int SlotCount => slots.Length;
    public bool Running { get; private set; }
    public int Served { get; private set; }
    public int Expired { get; private set; }
    public int Tips { get; private set; } // tip points this round (end screen)
    public int ServePoints => servePoints;

    private Order[] slots;
    private float[] refillTimers; // counts down while a slot is empty

    void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Debug.LogWarning($"[Orders] More than one OrderManager; ignoring the one on {name}.");
            enabled = false;
            return;
        }
        Instance = this;
        slots = new Order[Mathf.Max(1, slotCount)];
        refillTimers = new float[slots.Length];
    }

    void OnDestroy()
    {
        if (Instance == this) Instance = null;
    }

    void Start()
    {
        if (startOnPlay) StartOrders();
        else SyncPot(); // no orders yet: the pot takes nothing until the round starts
    }

    // Called by Pot.Start. Orders need the pot's Variations, so none appear until the stove marker has been seen.
    public void SetPot(Pot p)
    {
        pot = p;
        SyncPot();
    }

    // New round: clears the slots and fills them after First Order Delay.
    public void StartOrders()
    {
        Running = true;
        Served = 0;
        Expired = 0;
        Tips = 0;
        for (int i = 0; i < slots.Length; i++)
        {
            slots[i] = null;
            refillTimers[i] = firstOrderDelay;
        }
        SyncPot();
    }

    // Round over: timers stop, the orders stay on screen as they were.
    public void StopOrders() => Running = false;

    // Mode switch: stops and removes every order, so the tickets leave the screen now. StartOrders brings new ones.
    public void ClearOrders()
    {
        Running = false;
        for (int i = 0; i < slots.Length; i++) slots[i] = null;
        SyncPot();
    }

    // The order in a slot, or null while it waits for a new one.
    public Order Get(int slot) => slot >= 0 && slot < slots.Length ? slots[slot] : null;

    void Update()
    {
        if (!Running) return;
        for (int i = 0; i < slots.Length; i++)
        {
            var order = slots[i];
            if (order == null)
            {
                refillTimers[i] -= Time.deltaTime;
                if (refillTimers[i] <= 0f) Fill(i);
                continue;
            }
            if (!RoundTimer.GodRules) order.TimeLeft -= Time.deltaTime; // God mode / Tutorial: orders never expire
            if (order.TimeLeft <= 0f)
            {
                Debug.Log($"[Orders] {order.Recipe.name} expired");
                Expired++;
                Empty(i);
                onOrderExpired?.Invoke(i);
            }
        }
    }

    // A plate of `dish` was put on the serve window. Serves the oldest order for it; false if none wants it.
    public bool TryServe(string dish, out int points)
    {
        points = 0;
        int best = -1;
        for (int i = 0; i < slots.Length; i++)
        {
            var o = slots[i];
            if (o != null && o.Recipe.name == dish && (best < 0 || o.CreatedAt < slots[best].CreatedAt)) best = i;
        }
        if (best < 0) return false;

        var order = slots[best];
        int tip = Mathf.RoundToInt(maxTip * order.Fraction);
        points = servePoints + tip;
        if (ScoreManager.Instance != null) ScoreManager.Instance.Add(points, $"served {dish} (tip {tip})");
        else Debug.LogWarning("[Orders] No ScoreManager in the scene; the serve scores nothing.");
        Debug.Log($"[Orders] {dish} served with {order.TimeLeft:F0}s left: +{servePoints} +{tip} tip");
        Served++;
        Tips += tip;
        Empty(best);
        onOrderServed?.Invoke(best, points);
        return true;
    }

    private void Fill(int slot)
    {
        var variations = pot != null ? pot.Variations : null;
        if (variations == null || variations.Count == 0) return; // no pot yet: tried again next frame
        var recipe = variations[Random.Range(0, variations.Count)];
        slots[slot] = new Order { Recipe = recipe, Duration = orderTime, TimeLeft = orderTime, CreatedAt = Time.time };
        Debug.Log($"[Orders] New order: {recipe.name}");
        SyncPot();
        onOrderAdded?.Invoke(slot);
    }

    private void Empty(int slot)
    {
        slots[slot] = null;
        refillTimers[slot] = refillDelay;
        SyncPot();
    }

    // The pot only takes ingredients that fit one of these. Duplicates are fine (two soups = one entry twice).
    private void SyncPot()
    {
        if (pot == null) return;
        var recipes = new List<Pot.Recipe>();
        foreach (var o in slots) if (o != null) recipes.Add(o.Recipe);
        pot.SetActiveOrders(recipes);
    }
}
