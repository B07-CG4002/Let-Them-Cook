using UnityEngine;

// One chopped (or being-chopped) ingredient: the parent of all its pieces, so they're grabbed, carried and dropped
// as one. Made by GestureButtonController on the first chop, so a half-chopped ingredient can already be picked up
// and finished later on any board. Nothing to set up.
// The pot only takes it once IsFullyChopped, and reads Type to know what it received.
public class ChoppedIngredient : MonoBehaviour
{
    public int Chops { get; private set; }
    public int MaxChops { get; private set; }
    public IngredientType Type { get; private set; }
    public bool IsFullyChopped => Chops >= MaxChops;

    public void Init(int maxChops, IngredientType type)
    {
        MaxChops = maxChops;
        Type = type;
    }
    public void AddChop() => Chops++;
}
