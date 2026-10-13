using UnityEngine;
using UnityEngine.Events;

public class SelectableMenuItem : MonoBehaviour
{
    public UnityEvent onSelect;

    public void Select()
    {
        onSelect?.Invoke();
    }
}