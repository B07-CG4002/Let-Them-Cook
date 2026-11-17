using System.Collections.Generic;
using UnityEngine;

// The order tickets, top-left of the HUD: oldest order on the left, a new one slides in from the right, and when
// one leaves the others slide along. Reads OrderManager in Update; each eye's HUD copy has its own row.
// Give it one more ticket than OrderManager's Slot Count (3 for 2 orders), so a served ticket can finish its exit
// while the next order arrives.
public class OrderTicketRow : MonoBehaviour
{
    [SerializeField] private HudIcons icons;
    [Tooltip("The tickets (children of this object). Their own positions don't matter; the row places them.")]
    [SerializeField] private OrderTicket[] tickets;
    [Tooltip("Position of the oldest ticket (its anchored position).")]
    [SerializeField] private Vector2 firstPosition = new Vector2(132f, -150f);
    [Tooltip("From one ticket to the next, in HUD pixels: ticket width + gap.")]
    [SerializeField] private float spacing = 288f;

    private readonly List<OrderManager.Order> live = new List<OrderManager.Order>();

    void Update()
    {
        live.Clear();
        var orders = OrderManager.Instance;
        if (orders != null)
            for (int i = 0; i < orders.SlotCount; i++)
            {
                var o = orders.Get(i);
                if (o != null) live.Add(o);
            }
        live.Sort((a, b) => a.CreatedAt.CompareTo(b.CreatedAt));

        // Orders that are gone: time left = served, none left = expired.
        foreach (var t in tickets)
            if (t != null && t.Order != null && !live.Contains(t.Order))
                t.Leave(t.Order.TimeLeft > 0f);

        for (int rank = 0; rank < live.Count; rank++)
        {
            var pos = firstPosition + new Vector2(spacing * rank, 0f);
            var ticket = TicketFor(live[rank]);
            if (ticket != null)
            {
                ticket.MoveTo(pos);
                continue;
            }
            ticket = FreeTicket();
            if (ticket != null) ticket.Show(live[rank], pos, icons); // none free: tried again next frame
        }
    }

    private OrderTicket TicketFor(OrderManager.Order order)
    {
        foreach (var t in tickets)
            if (t != null && t.Order == order) return t;
        return null;
    }

    private OrderTicket FreeTicket()
    {
        foreach (var t in tickets)
            if (t != null && t.Free) return t;
        return null;
    }
}
