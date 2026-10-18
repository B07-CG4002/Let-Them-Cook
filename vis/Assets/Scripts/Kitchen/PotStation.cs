using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

public enum PotStationKind { Stove, Sink, Counter }

// A spot the pot can be put down on (the stove, the sink). Dropping a held pot over one snaps it onto the seat.
// Other scripts read pot.Station / Kind, or listen to the events (the sink fills the pot with water on onPotPlaced).
public class PotStation : MonoBehaviour
{
    [SerializeField] private PotStationKind kind = PotStationKind.Stove;
    [Tooltip("Where the pot's pivot goes. Empty = this object's position.")]
    [SerializeField] private Transform seat;
    [Tooltip("A pot that starts on this station (e.g. the pot in the same QR prefab). It isn't moved, just counted as here.")]
    [SerializeField] private Pot startingPot;
    [Tooltip("A pot dropped within this many screen pixels of the seat counts as on the station, " +
        "on top of anywhere over the station's own model.")]
    [SerializeField] private float snapRadius = 150f;

    public UnityEvent<Pot> onPotPlaced;
    public UnityEvent<Pot> onPotRemoved;

    public PotStationKind Kind => kind;
    public Pot Occupant { get; private set; }

    // Every enabled station; read only on a pot drop, so nothing searches the scene.
    public static readonly List<PotStation> All = new List<PotStation>();

    void OnEnable() => All.Add(this);
    void OnDisable() => All.Remove(this);

    // After Pot.Start (which clears the pot), so the starting pot keeps its station.
    void Start()
    {
        if (startingPot != null && Occupant == null)
        {
            Occupant = startingPot;
            startingPot.Station = this;
            onPotPlaced?.Invoke(startingPot);
        }
    }

    // The free station the pot lands on if dropped where it is now, or null.
    public static PotStation FindFor(Pot pot, Camera cam)
    {
        PotStation best = null;
        float bestDist = float.PositiveInfinity;
        Vector3 potPos = pot.transform.position;
        Vector3 potScreen = cam.WorldToScreenPoint(potPos);
        if (potScreen.z <= 0f) return null;

        foreach (var station in All)
        {
            if (station.Occupant != null) continue;
            Vector3 seatScreen = cam.WorldToScreenPoint(station.SeatPosition);
            float d = seatScreen.z > 0f ? Vector2.Distance(potScreen, seatScreen) : float.PositiveInfinity;
            bool over = d <= station.snapRadius || ScreenBounds.Covers(station.transform, potPos, cam, pot.transform);
            if (over && d < bestDist) { best = station; bestDist = d; }
        }
        return best;
    }

    public Vector3 SeatPosition => seat != null ? seat.position : transform.position;

    // Snaps the pot onto the seat. Its rotation is kept: it never turns while held, so it stays upright.
    public void Place(Pot pot)
    {
        pot.transform.position = SeatPosition;
        Occupant = pot;
        pot.Station = this;
        Debug.Log($"[PotStation] {pot.name} placed on {name} ({kind})");
        onPotPlaced?.Invoke(pot);
    }

    public void Remove(Pot pot)
    {
        if (Occupant != pot) return;
        Occupant = null;
        pot.Station = null;
        Debug.Log($"[PotStation] {pot.name} taken off {name} ({kind})");
        onPotRemoved?.Invoke(pot);
    }
}
