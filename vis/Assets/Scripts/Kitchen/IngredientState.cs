using UnityEngine;

public enum IngredientType { Carrot, Potato, Beef, CurryCube }

public class IngredientState : MonoBehaviour
{
    [Tooltip("Which ingredient this is. The pot uses it to work out the dish.")]
    public IngredientType Type = IngredientType.Carrot;
    [Tooltip("Material for the cut faces when this ingredient is chopped. Empty = GestureButtonController's Cross Section Material.")]
    public Material CrossSectionMaterial;
    [Tooltip("True only while resting on a chopping board. Set when a drop lands on a board, cleared on grab.")]
    public bool OnChoppingBoard = false;
    [HideInInspector] public ChoppingBoard Board; // which board it's resting on; null while held or off a board

    // Only the curry cube goes into the pot whole; everything else must be fully chopped first.
    public static bool NeedsChopping(IngredientType type) => type != IngredientType.CurryCube;
}
