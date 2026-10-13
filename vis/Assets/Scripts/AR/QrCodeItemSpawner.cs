using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR.ARFoundation;

public class QrCodeItemSpawner : MonoBehaviour
{
    [System.Serializable]
    public class ImagePrefabPair
    {
        public string imageName; // must exactly match the entry's Name in your Reference Image Library
        public GameObject prefab;
        public Vector3 localOffset;
        public Vector3 localScale = Vector3.one;
    }

    [SerializeField] private ARTrackedImageManager trackedImageManager;
    [SerializeField] private List<ImagePrefabPair> imagePrefabs;

    private readonly Dictionary<string, GameObject> spawnedObjects = new Dictionary<string, GameObject>();

    void OnEnable()
    {
        trackedImageManager.trackablesChanged.AddListener(OnTrackablesChanged);
    }

    void OnDisable()
    {
        trackedImageManager.trackablesChanged.RemoveListener(OnTrackablesChanged);
    }

    private ImagePrefabPair GetPairFor(string imageName)
    {
        foreach (var pair in imagePrefabs)
        {
            if (pair.imageName == imageName)
                return pair;
        }
        return null;
    }

    private void OnTrackablesChanged(ARTrackablesChangedEventArgs<ARTrackedImage> args)
    {
        foreach (var trackedImage in args.added)
        {
            string imageName = trackedImage.referenceImage.name;
            var pair = GetPairFor(imageName);
            if (pair == null || spawnedObjects.ContainsKey(imageName))
                continue;

            var obj = Instantiate(pair.prefab, trackedImage.transform.position, trackedImage.transform.rotation * pair.prefab.transform.rotation);
            obj.transform.SetParent(trackedImage.transform, worldPositionStays: true);
            obj.transform.localPosition = pair.localOffset;
            obj.transform.localScale = pair.localScale;
            spawnedObjects[imageName] = obj;
        }

        foreach (var trackedImage in args.removed)
        {
            string imageName = trackedImage.Value.referenceImage.name;
            if (spawnedObjects.TryGetValue(imageName, out var obj))
            {
                Destroy(obj);
                spawnedObjects.Remove(imageName);
            }
        }
    }
}