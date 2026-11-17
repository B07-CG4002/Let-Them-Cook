using System.Collections.Generic;
using UnityEngine;

// The dish rack's display plates: plain plate meshes placed by hand in the rack slots. The first N are shown and the
// rest hidden (never destroyed), so the rack always shows how many clean plates it has.
public class PlatePile : MonoBehaviour
{
    [Tooltip("Plate meshes (no scripts), shown in this order: Element 0 first, the last one hides first.")]
    [SerializeField] private List<GameObject> slots = new List<GameObject>();

    public void Show(int count)
    {
        for (int i = 0; i < slots.Count; i++)
            if (slots[i] != null) slots[i].SetActive(i < count); // more plates than slots still count
    }
}
