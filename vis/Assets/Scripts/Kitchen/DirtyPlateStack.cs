using System.Collections.Generic;
using UnityEngine;

// The dirty plates on the serving window's DirtyPlateSeat, grouped under one parent so ONE Grab carries them all,
// like a chopped carrot's pieces under its ChoppedIngredient. ServeStation makes it (with HandInteractable and
// Grabbable) and adds each returning plate; dropping it over the sink hands the plates to the sink and the parent goes.
public class DirtyPlateStack : MonoBehaviour
{
    private readonly List<Plate> plates = new List<Plate>();
    public IReadOnlyList<Plate> Plates => plates;
    public int Count => plates.Count;

    // Puts the plate on top of the stack, stackStep above the one below. Its own HandInteractable is switched off,
    // so a Grab takes the whole stack, never one plate out of it.
    public void Add(Plate plate, float stackStep)
    {
        plate.transform.SetParent(transform, worldPositionStays: true);
        plate.transform.position = transform.position + Vector3.up * (stackStep * plates.Count);
        if (plate.TryGetComponent(out HandInteractable touch)) touch.enabled = false;
        plate.gameObject.SetActive(true);
        plates.Add(plate);
    }

    // Empties the stack (the plates are unparented); the caller then destroys this object.
    public List<Plate> TakeAll()
    {
        var all = new List<Plate>(plates);
        foreach (var p in all) p.transform.SetParent(null, worldPositionStays: true);
        plates.Clear();
        return all;
    }
}
