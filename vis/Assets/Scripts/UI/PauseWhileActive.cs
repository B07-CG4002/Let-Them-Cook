using UnityEngine;

// Pauses the game while this object is active: put it on the in-game mode card, so the round clock, countdown, orders,
// cooking, burning and washing all stop while the card is open, and carry on when it closes (X, a mode button, or
// leaving the scene). Works through Time.timeScale = 0; DwellSelect uses unscaled time, so the card's buttons still work.
public class PauseWhileActive : MonoBehaviour
{
    void OnEnable() => Time.timeScale = 0f;

    // Also runs when the scene unloads (Main menu button), so the next scene isn't left paused.
    void OnDisable() => Time.timeScale = 1f;
}
