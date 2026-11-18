using UnityEngine;

// Pot is last so existing prefabs keep their Knife/Ingredient setting.
public enum GrabbableType { Knife, Ingredient, Pot, Plate }

public class Grabbable : MonoBehaviour
{
    [SerializeField] private GrabbableType itemType;
    // Settable so code-built grabbables (e.g. a chopped ingredient bundle) can pick their type.
    public GrabbableType ItemType { get => itemType; set => itemType = value; }
    [Tooltip("Extra metres the item sits behind the hand while held. For big items like the pot (about its radius), " +
             "so its near side is at your hand instead of its middle. 0 = centred on the hand.")]
    public float holdBehindHand = 0f;
    [Tooltip("Closest the held item may come to the camera, in metres (0 = the controller's own minimum).")]
    public float minHoldDistance = 0f;
    [HideInInspector] public bool isHeld;
}
