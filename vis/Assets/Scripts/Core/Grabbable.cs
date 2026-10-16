using UnityEngine;

// Pot is last so existing prefabs keep their Knife/Ingredient setting.
public enum GrabbableType { Knife, Ingredient, Pot }

public class Grabbable : MonoBehaviour
{
    [SerializeField] private GrabbableType itemType;
    // Settable so code-built grabbables (e.g. a chopped ingredient bundle) can pick their type.
    public GrabbableType ItemType { get => itemType; set => itemType = value; }
    [HideInInspector] public bool isHeld;
}
