using System.Collections.Generic;
using UnityEngine;

// A short, timestamped record of what each hand was doing, so a late glove gesture can be matched to the moment it
// was made instead of the moment it arrived.
//
// Why: the camera sees the hand now, but a glove gesture reaches the game some time after the hand did it (IMU window,
// Ultra96 inference, network). Two producers (the gloves) and two more (the camera, each hand) feed one consumer (the
// game), and they don't agree on time. Checking "what is the hand touching now" when the gesture arrives is a
// time-of-check / time-of-use race: by then the hand has moved off the knife, or the other hand has taken it.
//
// Fix: GestureButtonController writes one Sample per hand every frame into a fixed-size ring buffer (oldest entries are
// overwritten, nothing grows). When a gesture finds nothing at the hand's current position, it walks back through the
// last Lookback seconds, newest first, and uses the latest moment the action would have worked, after checking the
// target is still free (not destroyed, not in the other hand). Only the main thread reads and writes it, so no lock.
public class HandHistory
{
    public struct Sample
    {
        public float time;              // Time.time of the frame
        public bool tracked;            // the hand was seen this frame
        public Vector2 center;          // palm centre on screen (valid if tracked)
        public HandInteractable target; // what Grab would have picked that frame (closest touched, not held), or null
        public Pot pot;                 // a pot this hand touched that frame, or null
        public Transform held;          // the item in this hand that frame, or null
        public Vector3 heldPosition;    // where that item was (world)
    }

    // 4 s at 60 fps, more than any sensible Lookback. Fixed size, so recording never allocates.
    private const int Capacity = 240;

    private readonly Sample[][] buffers = { new Sample[Capacity], new Sample[Capacity] }; // indexed by Hand
    private readonly int[] next = new int[2];   // where the next sample goes
    private readonly int[] count = new int[2];  // how many are filled

    public void Record(Hand hand, Sample sample)
    {
        int h = (int)hand;
        buffers[h][next[h]] = sample;
        next[h] = (next[h] + 1) % Capacity;
        if (count[h] < Capacity) count[h]++;
    }

    // This hand's samples from the last `window` seconds, newest first.
    public IEnumerable<Sample> Recent(Hand hand, float window)
    {
        int h = (int)hand;
        float oldest = Time.time - window;
        for (int i = 1; i <= count[h]; i++)
        {
            var s = buffers[h][(next[h] - i + Capacity) % Capacity];
            if (s.time < oldest) yield break;
            yield return s;
        }
    }

    public void Clear()
    {
        for (int h = 0; h < 2; h++) { next[h] = 0; count[h] = 0; }
    }
}
