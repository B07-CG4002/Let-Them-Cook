using UnityEngine;
using System.Collections.Generic;

public class MeshHoverFeedback : MonoBehaviour
{
    [Tooltip("How dark a hovered object gets (1 = unchanged, 0 = black).")]
    [SerializeField, Range(0f, 1f)] private float dim = 0.6f;
    [Tooltip("Renderers that keep their own colour, e.g. the pot's SoupSurface (Pot recolours it, so the highlight mustn't put an old colour back).")]
    [SerializeField] private List<Renderer> skip = new List<Renderer>();

    private List<Renderer> renderers = new List<Renderer>();
    private List<Color> originalColors = new List<Color>();

    public float Dim { get => dim; set => dim = value; }

    void Awake() => RefreshRenderers();

    // Re-reads the child renderers and their colours. Call while not highlighted (e.g. after a chopped
    // ingredient's pieces change), or the dimmed colour is taken as the original.
    public void RefreshRenderers()
    {
        renderers.Clear();
        originalColors.Clear();
        foreach (var r in GetComponentsInChildren<Renderer>())
        {
            if (skip.Contains(r)) continue;
            renderers.Add(r);
            originalColors.Add(r.material.color); // r.material gives this object its own material copy, so other carrots aren't tinted
        }
    }

    public void Highlight()
    {
        for (int i = 0; i < renderers.Count; i++)
        {
            Color c = originalColors[i] * dim;
            c.a = originalColors[i].a;
            renderers[i].material.color = c;
        }
    }

    public void ResetColor()
    {
        for (int i = 0; i < renderers.Count; i++)
            renderers[i].material.color = originalColors[i];
    }
}
