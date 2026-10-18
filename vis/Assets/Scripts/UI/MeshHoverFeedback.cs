using UnityEngine;
using System.Collections.Generic;

public class MeshHoverFeedback : MonoBehaviour
{
    [SerializeField] private Color highlightColor = Color.yellow;

    private List<Renderer> renderers = new List<Renderer>();
    private List<Color> originalColors = new List<Color>();

    void Awake()
    {
        renderers.AddRange(GetComponentsInChildren<Renderer>());
        foreach (var r in renderers)
            originalColors.Add(r.material.color);
    }

    public void Highlight()
    {
        foreach (var r in renderers)
            r.material.color = highlightColor;
    }

    public void ResetColor()
    {
        for (int i = 0; i < renderers.Count; i++)
            renderers[i].material.color = originalColors[i];
    }
}