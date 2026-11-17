// Which of the player's hands. Each hand holds and acts on its own (GestureButtonController's hand slots),
// and each glove's device_id maps to one (CommsBridge). With Two Hands unticked on HandTrackingManager,
// everything uses Right, like the old single "primary hand".
public enum Hand { Left, Right }

public static class HandExtensions
{
    public static readonly Hand[] Both = { Hand.Left, Hand.Right };

    public static Hand Other(this Hand hand) => hand == Hand.Left ? Hand.Right : Hand.Left;
}
