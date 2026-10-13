using UnityEngine;

public enum GrabbableType { Knife, Ingredient }

public class Grabbable : MonoBehaviour
{
    [SerializeField] private GrabbableType itemType;
    // Settable so code-built grabbables (e.g. a chopped ingredient bundle) can pick their type.
    public GrabbableType ItemType { get => itemType; set => itemType = value; }
    [HideInInspector] public bool isHeld;
}
