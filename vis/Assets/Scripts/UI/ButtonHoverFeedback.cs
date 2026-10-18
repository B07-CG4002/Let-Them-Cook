using UnityEngine;
using UnityEngine.UI;

[RequireComponent(typeof(Image))]
public class ButtonHoverFeedback : MonoBehaviour
{
    [SerializeField] private float darkenAmount = 0.2f;

    private Image buttonImage;
    private Color originalColor;

    void Awake()
    {
        buttonImage = GetComponent<Image>();
        originalColor = buttonImage.color;
    }

    public void Darken()
    {
        buttonImage.color = originalColor * (1f - darkenAmount);
        buttonImage.color = new Color(buttonImage.color.r, buttonImage.color.g, buttonImage.color.b, originalColor.a);
    }

    public void ResetColor()
    {
        buttonImage.color = originalColor;
    }
}