using System.Collections;
using UnityEngine;
using UnityEngine.SceneManagement;

// Time's up: saves the results, shows the TIME'S UP! banner in both eyes, then loads the End scene.
// One in the game scene (an empty GameObject named RoundEnd). Hook it up: RoundTimer > On Round Over (+) > RoundEnd.Finish.
public class RoundEnd : MonoBehaviour
{
    [Tooltip("Seconds the TIME'S UP! banner shows before the End scene loads.")]
    [SerializeField] private float bannerTime = 2f;
    [Tooltip("Name of the results scene, as it appears in the Build Profiles scene list.")]
    [SerializeField] private string endScene = "End";

    private bool finishing;

    public void Finish()
    {
        if (finishing) return;
        finishing = true;
        RoundResults.RecordRound();
        TimesUpBanner.ShowAll();
        StartCoroutine(LoadEndScene());
    }

    private IEnumerator LoadEndScene()
    {
        yield return new WaitForSecondsRealtime(bannerTime);
        SceneManager.LoadScene(endScene);
    }
}
