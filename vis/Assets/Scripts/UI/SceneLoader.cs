using UnityEngine;
using UnityEngine.SceneManagement;

// Menu buttons: Play, God mode and Tutorial all load the game scene, each in its own mode (RoundTimer.Mode).
public class SceneLoader : MonoBehaviour
{
    // Menu Play, End scene Play again / Menu, in-game Menu: a timed round next time the game scene loads.
    public void LoadScene(string sceneName) => Load(sceneName, RoundTimer.GameMode.Play);

    // Menu God mode: no clock, orders never expire, food never burns.
    public void LoadGodMode(string sceneName) => Load(sceneName, RoundTimer.GameMode.God);

    // Menu Tutorial: God mode rules plus the How to play card and step prompt.
    public void LoadTutorial(string sceneName) => Load(sceneName, RoundTimer.GameMode.Tutorial);

    private static void Load(string sceneName, RoundTimer.GameMode mode)
    {
        RoundTimer.Mode = mode;
        SceneManager.LoadScene(sceneName);
    }
}
