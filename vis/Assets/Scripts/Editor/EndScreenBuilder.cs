using System.Collections.Generic;
using TMPro;
using UnityEditor;
using UnityEditor.Events;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.UI;

// Editor only (must sit in a folder named Editor). Three menu items:
//   Tools > OvercookAR > Build End Screen Prefabs   makes Assets/Prefabs/TimesUpBanner.prefab (game scene, one per eye)
//                                                   and Assets/Prefabs/EndCard.prefab (End scene, on Canvas(buttons)).
//   Tools > OvercookAR > Add Time's Up Banner To Eyes   puts one banner under LeftDisplay and one under RightDisplay.
//   Tools > OvercookAR > Add End Card To Canvas         puts the card on the open scene's "Canvas(buttons)".
// Same look and sprites as HudPrefabBuilder (Assets/Art/HUD/*.png if you have them). Sizes are in HUD pixels
// (one eye = 1278 x 1179). Everything after the build is set in the Inspector on the prefabs.
public static class EndScreenBuilder
{
    const string ArtFolder = "Assets/Art/HUD";
    const string BannerPath = "Assets/Prefabs/TimesUpBanner.prefab";
    const string CardPath = "Assets/Prefabs/EndCard.prefab";
    const string ButtonsCanvas = "Canvas(buttons)";

    static readonly Color Butter = Hex("FCE79A");
    static readonly Color Ink = Hex("1F1B16");
    static readonly Color Gold = Hex("F5C542");
    static readonly Color Red = Hex("E5483D");
    static readonly Color DarkRed = Hex("A9302A");
    static readonly Color Grey = Hex("6B6259");
    static readonly Color Green = Hex("3FBF7F");
    static readonly Color StarMissing = Hex("E9D58A");
    static readonly Color ShadowColor = new Color(0.12f, 0.106f, 0.086f, 0.45f);

    static Sprite white, rounded, pill, circle;
    static TMP_FontAsset labelFont, numberFont;

    [MenuItem("Tools/OvercookAR/Build End Screen Prefabs")]
    static void Build()
    {
        if ((AssetDatabase.LoadAssetAtPath<GameObject>(BannerPath) != null || AssetDatabase.LoadAssetAtPath<GameObject>(CardPath) != null) &&
            !EditorUtility.DisplayDialog("End screen", "TimesUpBanner / EndCard prefabs already exist. Replace them? Changes made to the prefabs are lost.", "Replace", "Cancel"))
            return;

        LoadSprites();
        labelFont = FindFont("Fredoka");
        numberFont = FindFont("Archivo") ?? labelFont;
        EnsureFolder("Assets/Prefabs");

        var banner = BuildBanner();
        PrefabUtility.SaveAsPrefabAsset(banner.gameObject, BannerPath);
        Object.DestroyImmediate(banner.gameObject);

        var card = BuildCard();
        PrefabUtility.SaveAsPrefabAsset(card.gameObject, CardPath);
        Object.DestroyImmediate(card.gameObject);

        AssetDatabase.SaveAssets();
        Debug.Log($"[End] Built {BannerPath} and {CardPath}.");
        Selection.activeObject = AssetDatabase.LoadAssetAtPath<GameObject>(CardPath);
    }

    [MenuItem("Tools/OvercookAR/Add Time's Up Banner To Eyes")]
    static void AddBannerToEyes()
    {
        var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(BannerPath);
        if (prefab == null)
        {
            EditorUtility.DisplayDialog("End screen", "Build the prefabs first (Tools > OvercookAR > Build End Screen Prefabs).", "OK");
            return;
        }
        int added = 0;
        foreach (var display in Object.FindObjectsByType<RawImage>(FindObjectsInactive.Include))
        {
            if (display.name != "LeftDisplay" && display.name != "RightDisplay") continue;
            if (display.transform.Find(prefab.name) != null)
            {
                Debug.Log($"[End] {display.name} already has a {prefab.name}; skipped.");
                continue;
            }
            var banner = (GameObject)PrefabUtility.InstantiatePrefab(prefab, display.transform);
            ((RectTransform)banner.transform).anchoredPosition = Vector2.zero;
            banner.transform.SetAsLastSibling(); // drawn over the HUD
            Undo.RegisterCreatedObjectUndo(banner, "Add Time's Up Banner");
            added++;
        }
        if (added > 0) EditorSceneManager.MarkSceneDirty(EditorSceneManager.GetActiveScene());
        Debug.Log(added > 0 ? $"[End] Added {added} TimesUpBanner." : "[End] No LeftDisplay / RightDisplay RawImage without a banner found in the open scene.");
    }

    [MenuItem("Tools/OvercookAR/Add End Card To Canvas")]
    static void AddCardToCanvas()
    {
        var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(CardPath);
        if (prefab == null)
        {
            EditorUtility.DisplayDialog("End screen", "Build the prefabs first (Tools > OvercookAR > Build End Screen Prefabs).", "OK");
            return;
        }
        Canvas target = null;
        foreach (var canvas in Object.FindObjectsByType<Canvas>(FindObjectsInactive.Include))
            if (canvas.name == ButtonsCanvas) target = canvas;
        if (target == null)
        {
            EditorUtility.DisplayDialog("End screen", $"No \"{ButtonsCanvas}\" in the open scene. Drag EndCard onto the Canvas that holds the menu buttons instead.", "OK");
            return;
        }
        var card = (GameObject)PrefabUtility.InstantiatePrefab(prefab, target.transform);
        Undo.RegisterCreatedObjectUndo(card, "Add End Card");
        EditorSceneManager.MarkSceneDirty(EditorSceneManager.GetActiveScene());
        Selection.activeObject = card;
    }

    // ---------- Time's up banner ----------

    static RectTransform BuildBanner()
    {
        var root = Rect("TimesUpBanner", null);
        root.anchorMin = root.anchorMax = root.pivot = new Vector2(0.5f, 0.5f);
        root.sizeDelta = new Vector2(1278f, 1179f);
        root.gameObject.AddComponent<HudScaler>();
        root.gameObject.AddComponent<CanvasGroup>().blocksRaycasts = false;

        var dim = Rect("Dim", root);
        Stretch(dim, 0f);
        AddImage(dim, white, new Color(0.055f, 0.047f, 0.04f, 0.35f));

        var border = Rect("Banner", root);
        Place(border, new Vector2(0.5f, 0.5f), new Vector2(0.5f, 0.5f), new Vector2(0f, 40f), new Vector2(820f, 170f));
        border.localEulerAngles = new Vector3(0f, 0f, 3f);
        AddShadow(AddImage(border, rounded, Red, 40f / 52f));
        var fill = Rect("Fill", border);
        Stretch(fill, 10f);
        AddImage(fill, rounded, Butter, 40f / 44f);
        var label = Rect("Label", fill);
        Stretch(label, 0f);
        AddText(label, "TIME'S UP!", numberFont, 100f, Ink, TextAlignmentOptions.Center);

        var comp = root.gameObject.AddComponent<TimesUpBanner>();
        var so = new SerializedObject(comp);
        so.FindProperty("banner").objectReferenceValue = border;
        so.ApplyModifiedPropertiesWithoutUndo();
        return root;
    }

    // ---------- results card ----------

    static RectTransform BuildCard()
    {
        // Fills the whole Canvas with the dim; the card sits in the middle at one eye's HUD size (mockup board 7b x 2).
        var root = Rect("EndCard", null);
        Stretch(root, 0f);
        AddImage(root, white, new Color(0.055f, 0.047f, 0.04f, 0.6f));
        var loader = root.gameObject.AddComponent<SceneLoader>();

        var card = Rect("Card", root);
        Place(card, new Vector2(0.5f, 0.5f), new Vector2(0.5f, 0.5f), Vector2.zero, new Vector2(800f, 1020f));
        AddShadow(AddImage(card, rounded, Butter, 40f / 48f));
        var layout = card.gameObject.AddComponent<VerticalLayoutGroup>();
        layout.padding = new RectOffset(44, 44, 32, 36);
        layout.spacing = 20f;
        layout.childAlignment = TextAnchor.UpperCenter;
        layout.childControlWidth = layout.childControlHeight = true;
        layout.childForceExpandWidth = layout.childForceExpandHeight = false;

        // Stars: the middle one bigger, bottoms lined up.
        var starRow = Row("Stars", card, 12f, TextAnchor.LowerCenter);
        var stars = new List<Object>();
        foreach (float size in new[] { 100f, 124f, 100f })
        {
            var star = Rect("Star" + stars.Count, starRow);
            Prefer(star, size, size);
            var image = AddImage(star, circle, StarMissing);
            image.preserveAspect = true;
            stars.Add(image);
        }

        // Lines on a white panel.
        var lines = Rect("Lines", card);
        AddImage(lines, rounded, Color.white, 40f / 28f);
        var linesLayout = lines.gameObject.AddComponent<VerticalLayoutGroup>();
        linesLayout.padding = new RectOffset(28, 28, 16, 16);
        linesLayout.spacing = 8f;
        linesLayout.childControlWidth = linesLayout.childControlHeight = true;
        linesLayout.childForceExpandWidth = true;
        linesLayout.childForceExpandHeight = false;

        var served = Line(lines, "Served", "Dishes served x4", "+80", Ink, Hex("F5A13A"));
        var tips = Line(lines, "Tips", "Tips", "+16", Ink, Gold);
        var burnt = Line(lines, "Burnt", "Burnt x1", "-10", DarkRed, Hex("3A342D"));
        var missed = Line(lines, "Missed", "Orders missed x1", "0", Grey, Hex("E9E4D8"));

        // Total: coin, number, "New best!" pill.
        var totalRow = Row("Total", card, 24f, TextAnchor.MiddleCenter);
        var coin = Rect("Coin", totalRow);
        Prefer(coin, 92f, 92f);
        AddImage(coin, circle, Gold).preserveAspect = true;
        var number = Rect("Number", totalRow);
        var totalText = AddText(number, "86", numberFont, 96f, Ink, TextAlignmentOptions.MidlineLeft);
        var best = Rect("NewBest", totalRow);
        AddImage(best, pill, Green);
        var bestLayout = best.gameObject.AddComponent<HorizontalLayoutGroup>();
        bestLayout.padding = new RectOffset(20, 20, 8, 8);
        bestLayout.childControlWidth = bestLayout.childControlHeight = true;
        bestLayout.childForceExpandWidth = bestLayout.childForceExpandHeight = false;
        AddText(Rect("Label", best), "New best!", labelFont, 30f, Hex("0F2A1B"), TextAlignmentOptions.Center);

        // Pushes the buttons to the bottom.
        var spacer = Rect("Spacer", card);
        spacer.gameObject.AddComponent<LayoutElement>().flexibleHeight = 1f;

        var buttons = Row("Buttons", card, 24f, TextAnchor.MiddleCenter);
        var buttonsLayout = buttons.GetComponent<HorizontalLayoutGroup>();
        buttonsLayout.childForceExpandWidth = true;
        Button(buttons, "PlayAgain", "Play again", loader, "Game", 4f);
        Button(buttons, "Menu", "Menu", loader, "Menu", 3f);

        var comp = card.gameObject.AddComponent<EndScreen>();
        var so = new SerializedObject(comp);
        SetLine(so.FindProperty("served"), served);
        SetLine(so.FindProperty("tips"), tips);
        SetLine(so.FindProperty("burnt"), burnt);
        SetLine(so.FindProperty("missed"), missed);
        so.FindProperty("totalRow").objectReferenceValue = totalRow.gameObject;
        so.FindProperty("totalLabel").objectReferenceValue = totalText;
        so.FindProperty("newBest").objectReferenceValue = best.gameObject;
        SetArray(so.FindProperty("stars"), stars);
        so.ApplyModifiedPropertiesWithoutUndo();
        return root;
    }

    struct LineParts { public GameObject root; public TMP_Text label, points; }

    static LineParts Line(RectTransform parent, string name, string text, string points, Color color, Color iconColor)
    {
        var row = Row(name, parent, 20f, TextAnchor.MiddleLeft);
        Prefer(row, -1f, 60f);
        var icon = Rect("Icon", row);
        Prefer(icon, 48f, 48f);
        AddImage(icon, circle, iconColor).preserveAspect = true;
        var label = Rect("Label", row);
        label.gameObject.AddComponent<LayoutElement>().flexibleWidth = 1f;
        var labelText = AddText(label, text, labelFont, 34f, color, TextAlignmentOptions.MidlineLeft);
        var pts = Rect("Points", row);
        var pointsText = AddText(pts, points, numberFont, 36f, color, TextAlignmentOptions.MidlineRight);
        return new LineParts { root = row.gameObject, label = labelText, points = pointsText };
    }

    static void Button(RectTransform parent, string name, string text, SceneLoader loader, string scene, float borderWidth)
    {
        // The button's own Image is the white face, so DwellSelect's fill (a copy of it) draws over it, under the label.
        var rt = Rect(name, parent);
        Prefer(rt, -1f, 124f);
        var face = AddImage(rt, rounded, Color.white, 40f / 36f);
        var outline = rt.gameObject.AddComponent<Outline>();
        outline.effectColor = Ink;
        outline.effectDistance = new Vector2(borderWidth * 2f, -borderWidth * 2f);
        AddShadow(face);
        var label = Rect("Label", rt);
        Stretch(label, 0f);
        AddText(label, text, labelFont, 44f, Ink, TextAlignmentOptions.Center);

        // Same parts as the menu's Play button: hover darkens it, holding the fingertip on it fills it, then it loads the scene.
        var hover = rt.gameObject.AddComponent<ButtonHoverFeedback>();
        var interactable = rt.gameObject.AddComponent<HandInteractable>();
        // A component added from an Editor script has its UnityEvent fields still null until Unity serializes it.
        if (interactable.onHandEnter == null) interactable.onHandEnter = new UnityEngine.Events.UnityEvent();
        if (interactable.onHandExit == null) interactable.onHandExit = new UnityEngine.Events.UnityEvent();
        UnityEventTools.AddPersistentListener(interactable.onHandEnter, hover.Darken);
        UnityEventTools.AddPersistentListener(interactable.onHandExit, hover.ResetColor);
        var selectable = rt.gameObject.AddComponent<SelectableMenuItem>();
        if (selectable.onSelect == null) selectable.onSelect = new UnityEngine.Events.UnityEvent();
        UnityEventTools.AddStringPersistentListener(selectable.onSelect, loader.LoadScene, scene);
        var dwell = rt.gameObject.AddComponent<DwellSelect>();
        var so = new SerializedObject(dwell);
        so.FindProperty("fillColor").colorValue = Gold;
        so.ApplyModifiedPropertiesWithoutUndo();
    }

    static void SetLine(SerializedProperty prop, LineParts parts)
    {
        prop.FindPropertyRelative("root").objectReferenceValue = parts.root;
        prop.FindPropertyRelative("label").objectReferenceValue = parts.label;
        prop.FindPropertyRelative("points").objectReferenceValue = parts.points;
    }

    // ---------- helpers (same as HudPrefabBuilder) ----------

    static RectTransform Row(string name, RectTransform parent, float spacing, TextAnchor alignment)
    {
        var rt = Rect(name, parent);
        var layout = rt.gameObject.AddComponent<HorizontalLayoutGroup>();
        layout.spacing = spacing;
        layout.childAlignment = alignment;
        layout.childControlWidth = layout.childControlHeight = true;
        layout.childForceExpandWidth = layout.childForceExpandHeight = false;
        return rt;
    }

    static void LoadSprites()
    {
        white = LoadSprite("white", Vector4.zero);
        rounded = LoadSprite("rounded", new Vector4(44f, 44f, 44f, 44f));
        pill = LoadSprite("pill", new Vector4(63f, 63f, 63f, 63f));
        circle = LoadSprite("circle", Vector4.zero);
        var builtinSliced = AssetDatabase.GetBuiltinExtraResource<Sprite>("UI/Skin/UISprite.psd");
        var builtinCircle = AssetDatabase.GetBuiltinExtraResource<Sprite>("UI/Skin/Knob.psd");
        if (rounded == null) rounded = builtinSliced;
        if (pill == null) pill = builtinSliced;
        if (circle == null) circle = builtinCircle;
        if (white == null) white = builtinSliced;
    }

    static Sprite LoadSprite(string name, Vector4 border)
    {
        string path = $"{ArtFolder}/{name}.png";
        if (AssetImporter.GetAtPath(path) == null) return null; // HudPrefabBuilder already warns about missing ones
        return AssetDatabase.LoadAssetAtPath<Sprite>(path);
    }

    static TMP_FontAsset FindFont(string nameContains)
    {
        foreach (var guid in AssetDatabase.FindAssets(nameContains + " t:TMP_FontAsset"))
        {
            var font = AssetDatabase.LoadAssetAtPath<TMP_FontAsset>(AssetDatabase.GUIDToAssetPath(guid));
            if (font != null) return font;
        }
        return null; // TMP's default font
    }

    static RectTransform Rect(string name, Transform parent)
    {
        var go = new GameObject(name, typeof(RectTransform));
        go.layer = LayerMask.NameToLayer("UI");
        if (parent != null) go.transform.SetParent(parent, false);
        return (RectTransform)go.transform;
    }

    static void Place(RectTransform rt, Vector2 anchor, Vector2 pivot, Vector2 position, Vector2 size)
    {
        rt.anchorMin = rt.anchorMax = anchor;
        rt.pivot = pivot;
        rt.anchoredPosition = position;
        rt.sizeDelta = size;
    }

    static void Stretch(RectTransform rt, float inset)
    {
        rt.anchorMin = Vector2.zero;
        rt.anchorMax = Vector2.one;
        rt.offsetMin = new Vector2(inset, inset);
        rt.offsetMax = new Vector2(-inset, -inset);
    }

    static Image AddImage(RectTransform rt, Sprite sprite, Color color, float pixelsPerUnitMultiplier = 1f)
    {
        var image = rt.gameObject.AddComponent<Image>();
        image.sprite = sprite;
        image.color = color;
        image.raycastTarget = false;
        if (sprite != null && sprite.border != Vector4.zero)
        {
            image.type = Image.Type.Sliced;
            image.pixelsPerUnitMultiplier = pixelsPerUnitMultiplier;
        }
        return image;
    }

    static TextMeshProUGUI AddText(RectTransform rt, string text, TMP_FontAsset font, float size, Color color, TextAlignmentOptions alignment)
    {
        var tmp = rt.gameObject.AddComponent<TextMeshProUGUI>();
        if (font != null) tmp.font = font;
        tmp.text = text;
        tmp.fontSize = size;
        tmp.color = color;
        tmp.alignment = alignment;
        tmp.raycastTarget = false;
        return tmp;
    }

    static void AddShadow(Graphic graphic)
    {
        var shadow = graphic.gameObject.AddComponent<Shadow>();
        shadow.effectColor = ShadowColor;
        shadow.effectDistance = new Vector2(0f, -8f);
    }

    // Negative = leave that size to the layout.
    static void Prefer(RectTransform rt, float width, float height)
    {
        var element = rt.gameObject.AddComponent<LayoutElement>();
        if (width >= 0f) element.preferredWidth = element.minWidth = width;
        if (height >= 0f) element.preferredHeight = element.minHeight = height;
    }

    static void SetArray(SerializedProperty array, List<Object> items)
    {
        array.arraySize = items.Count;
        for (int i = 0; i < items.Count; i++) array.GetArrayElementAtIndex(i).objectReferenceValue = items[i];
    }

    static void EnsureFolder(string path)
    {
        if (AssetDatabase.IsValidFolder(path)) return;
        int slash = path.LastIndexOf('/');
        AssetDatabase.CreateFolder(path.Substring(0, slash), path.Substring(slash + 1));
    }

    static Color Hex(string hex)
    {
        ColorUtility.TryParseHtmlString("#" + hex, out var c);
        return c;
    }
}
