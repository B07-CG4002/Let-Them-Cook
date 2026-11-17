using System.Collections.Generic;
using TMPro;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.UI;

// Editor only (must sit in a folder named Editor). Two menu items:
//   Tools > OvercookAR > Build HUD Prefab   makes Assets/Prefabs/EyeHUD.prefab with every HUD piece wired up,
//                                           and Assets/Art/HUD/HudIcons.asset (empty sprite slots for your drawings).
//   Tools > OvercookAR > Add HUD To Eyes    puts one EyeHUD under LeftDisplay and one under RightDisplay in the open scene.
// Sizes are in HUD pixels: one eye at full size is 1278 x 1179 (the UI mockups x 2). Everything after the build
// (sprites, colours, fonts, positions) is set in the Inspector on the prefab.
public static class HudPrefabBuilder
{
    const string ArtFolder = "Assets/Art/HUD";
    const string PrefabPath = "Assets/Prefabs/EyeHUD.prefab";
    const string IconsPath = ArtFolder + "/HudIcons.asset";

    static readonly Color Butter = Hex("FCE79A");
    static readonly Color Ink = Hex("1F1B16");
    static readonly Color Gold = Hex("F5C542");
    static readonly Color Red = Hex("E5483D");
    static readonly Color Green = Hex("3FBF7F");
    static readonly Color Track = Hex("F1EEE6");
    static readonly Color ShadowColor = new Color(0.12f, 0.106f, 0.086f, 0.45f);

    static Sprite white, rounded, pill, circle;
    static TMP_FontAsset labelFont, numberFont;

    [MenuItem("Tools/OvercookAR/Build HUD Prefab")]
    static void Build()
    {
        if (AssetDatabase.LoadAssetAtPath<GameObject>(PrefabPath) != null &&
            !EditorUtility.DisplayDialog("EyeHUD", PrefabPath + " already exists. Replace it? Changes made to the prefab are lost (the copies in the scene keep their overrides).", "Replace", "Cancel"))
            return;

        LoadSprites();
        labelFont = FindFont("Fredoka");
        numberFont = FindFont("Archivo") ?? labelFont;
        var icons = BuildIcons();

        var root = Rect("EyeHUD", null);
        root.anchorMin = root.anchorMax = root.pivot = new Vector2(0.5f, 0.5f);
        root.sizeDelta = new Vector2(1278f, 1179f);
        root.gameObject.AddComponent<HudScaler>();

        var safe = Rect("SafeArea", root);
        safe.anchorMin = Vector2.zero;
        safe.anchorMax = Vector2.one;
        safe.offsetMin = new Vector2(100f, 90f);
        safe.offsetMax = new Vector2(-100f, -90f);

        BuildOrders(safe, icons);
        BuildWarning(safe);
        BuildScore(safe);
        BuildGesture(safe);
        BuildTimer(safe);

        EnsureFolder("Assets/Prefabs");
        PrefabUtility.SaveAsPrefabAsset(root.gameObject, PrefabPath);
        Object.DestroyImmediate(root.gameObject);
        AssetDatabase.SaveAssets();
        Debug.Log($"[HUD] Built {PrefabPath}. Next: Tools > OvercookAR > Add HUD To Eyes.");
        Selection.activeObject = AssetDatabase.LoadAssetAtPath<GameObject>(PrefabPath);
    }

    [MenuItem("Tools/OvercookAR/Add HUD To Eyes")]
    static void AddToEyes()
    {
        var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(PrefabPath);
        if (prefab == null)
        {
            EditorUtility.DisplayDialog("EyeHUD", "Build the prefab first (Tools > OvercookAR > Build HUD Prefab).", "OK");
            return;
        }
        int added = 0;
        foreach (var display in Object.FindObjectsByType<RawImage>(FindObjectsInactive.Include))
        {
            if (display.name != "LeftDisplay" && display.name != "RightDisplay") continue;
            if (display.transform.Find(prefab.name) != null)
            {
                Debug.Log($"[HUD] {display.name} already has an {prefab.name}; skipped.");
                continue;
            }
            var hud = (GameObject)PrefabUtility.InstantiatePrefab(prefab, display.transform);
            ((RectTransform)hud.transform).anchoredPosition = Vector2.zero;
            Undo.RegisterCreatedObjectUndo(hud, "Add HUD To Eyes");
            added++;
        }
        if (added > 0) EditorSceneManager.MarkSceneDirty(EditorSceneManager.GetActiveScene());
        Debug.Log(added > 0 ? $"[HUD] Added {added} EyeHUD." : "[HUD] No LeftDisplay / RightDisplay RawImage without a HUD found in the open scene.");
    }

    // ---------- pieces ----------

    static void BuildOrders(RectTransform parent, HudIcons icons)
    {
        var row = Rect("Orders", parent);
        Place(row, new Vector2(0f, 1f), new Vector2(0f, 1f), Vector2.zero, new Vector2(900f, 320f));
        var ticketRow = row.gameObject.AddComponent<OrderTicketRow>();

        var tickets = new List<Object>();
        for (int i = 0; i < 3; i++) tickets.Add(BuildTicket(row, i));

        var so = new SerializedObject(ticketRow);
        so.FindProperty("icons").objectReferenceValue = icons;
        SetArray(so.FindProperty("tickets"), tickets);
        so.ApplyModifiedPropertiesWithoutUndo();
    }

    static OrderTicket BuildTicket(RectTransform parent, int index)
    {
        // 264 x 272: border 6, timer bar 16, dish 148, ingredient strip 96, border 6.
        var t = Rect("Ticket" + index, parent);
        Place(t, new Vector2(0f, 1f), new Vector2(0.5f, 0.5f), new Vector2(132f + 288f * index, -150f), new Vector2(264f, 272f));
        t.gameObject.AddComponent<CanvasGroup>();
        var frame = AddImage(t, rounded, Butter, 40f / 28f);
        AddShadow(frame);

        var body = Rect("Body", t);
        Stretch(body, 6f);
        AddImage(body, rounded, Color.white, 40f / 22f);
        body.gameObject.AddComponent<Mask>().showMaskGraphic = true;

        var track = Rect("TimerTrack", body);
        Place(track, new Vector2(0f, 1f), new Vector2(0.5f, 1f), Vector2.zero, new Vector2(0f, 16f));
        track.anchorMax = new Vector2(1f, 1f);
        AddImage(track, white, Track);
        var fillRect = Rect("TimerFill", track);
        Stretch(fillRect, 0f);
        var fill = AddImage(fillRect, white, Green);
        fill.type = Image.Type.Filled;
        fill.fillMethod = Image.FillMethod.Horizontal;
        fill.fillOrigin = (int)Image.OriginHorizontal.Left;

        var dishRect = Rect("Dish", body);
        Place(dishRect, new Vector2(0.5f, 1f), new Vector2(0.5f, 0.5f), new Vector2(0f, -16f - 74f), new Vector2(156f, 104f));
        var dish = AddImage(dishRect, circle, Hex("F5A13A"));
        dish.preserveAspect = true;

        var strip = Rect("Ingredients", body);
        Place(strip, new Vector2(0f, 0f), new Vector2(0.5f, 0f), Vector2.zero, new Vector2(0f, 96f));
        strip.anchorMax = new Vector2(1f, 0f);
        AddImage(strip, white, Butter);
        var layout = strip.gameObject.AddComponent<HorizontalLayoutGroup>();
        layout.childAlignment = TextAnchor.MiddleCenter;
        layout.spacing = 12f;
        layout.childControlWidth = layout.childControlHeight = false;
        layout.childForceExpandWidth = layout.childForceExpandHeight = false;

        var slots = new List<Object>();
        var iconImages = new List<Object>();
        for (int i = 0; i < 3; i++)
        {
            var slot = Rect("Slot" + i, strip);
            slot.sizeDelta = new Vector2(68f, 68f);
            AddImage(slot, circle, Color.white);
            var icon = Rect("Icon", slot);
            Place(icon, new Vector2(0.5f, 0.5f), new Vector2(0.5f, 0.5f), Vector2.zero, new Vector2(44f, 44f));
            var iconImage = AddImage(icon, circle, Color.gray);
            iconImage.preserveAspect = true;
            slots.Add(slot.gameObject);
            iconImages.Add(iconImage);
        }

        var ticket = t.gameObject.AddComponent<OrderTicket>();
        var so = new SerializedObject(ticket);
        so.FindProperty("frame").objectReferenceValue = frame;
        so.FindProperty("timerFill").objectReferenceValue = fill;
        so.FindProperty("dishImage").objectReferenceValue = dish;
        SetArray(so.FindProperty("ingredientSlots"), slots);
        SetArray(so.FindProperty("ingredientIcons"), iconImages);
        so.ApplyModifiedPropertiesWithoutUndo();
        return ticket;
    }

    static void BuildWarning(RectTransform parent)
    {
        // Red pill (the flashing border) holding a butter pill with a flame placeholder and the text.
        var border = Rect("Warning", parent);
        Place(border, new Vector2(1f, 1f), new Vector2(1f, 1f), new Vector2(0f, -12f), new Vector2(360f, 80f));
        border.gameObject.AddComponent<CanvasGroup>();
        var borderImage = AddImage(border, pill, Red);
        AddShadow(borderImage);
        var outer = border.gameObject.AddComponent<HorizontalLayoutGroup>();
        outer.padding = new RectOffset(6, 6, 6, 6);
        SetControl(outer);
        Fit(border);

        var inner = Rect("Fill", border);
        AddImage(inner, pill, Butter);
        var layout = inner.gameObject.AddComponent<HorizontalLayoutGroup>();
        layout.padding = new RectOffset(28, 32, 14, 14);
        layout.spacing = 14f;
        layout.childAlignment = TextAnchor.MiddleLeft;
        SetControl(layout);

        var flame = Rect("Icon", inner);
        AddImage(flame, circle, Red).preserveAspect = true;
        Prefer(flame, 40f, 40f);

        var label = AddText(Rect("Label", inner), "Pot burns in 4 s", labelFont, 34f, Ink, TextAlignmentOptions.MidlineLeft);

        var pillComp = border.gameObject.AddComponent<WarningPill>();
        var so = new SerializedObject(pillComp);
        so.FindProperty("label").objectReferenceValue = label;
        so.FindProperty("border").objectReferenceValue = borderImage;
        so.ApplyModifiedPropertiesWithoutUndo();
    }

    static void BuildScore(RectTransform parent)
    {
        var score = Rect("Score", parent);
        Place(score, Vector2.zero, Vector2.zero, Vector2.zero, new Vector2(320f, 128f));

        var panel = Rect("Panel", score);
        Place(panel, Vector2.zero, Vector2.zero, Vector2.zero, new Vector2(240f, 128f));
        AddShadow(AddImage(panel, rounded, Butter, 40f / 36f));
        var layout = panel.gameObject.AddComponent<HorizontalLayoutGroup>();
        layout.padding = new RectOffset(20, 44, 0, 0);
        layout.spacing = 24f;
        layout.childAlignment = TextAnchor.MiddleLeft;
        layout.childControlWidth = true;
        layout.childControlHeight = false;
        layout.childForceExpandWidth = layout.childForceExpandHeight = false;
        panel.gameObject.AddComponent<ContentSizeFitter>().horizontalFit = ContentSizeFitter.FitMode.PreferredSize;

        var coin = Rect("Coin", panel);
        coin.sizeDelta = new Vector2(92f, 92f);
        AddImage(coin, circle, Gold).preserveAspect = true;
        Prefer(coin, 92f, 92f);

        var number = Rect("Number", panel);
        number.sizeDelta = new Vector2(120f, 80f);
        var scoreText = AddText(number, "0", numberFont, 60f, Ink, TextAlignmentOptions.MidlineLeft);

        var popup = Rect("ServePopup", score);
        Place(popup, Vector2.zero, Vector2.zero, new Vector2(24f, 144f), new Vector2(200f, 50f));
        var popupGroup = popup.gameObject.AddComponent<CanvasGroup>();
        AddShadow(AddImage(popup, pill, Green));
        var popupLayout = popup.gameObject.AddComponent<HorizontalLayoutGroup>();
        popupLayout.padding = new RectOffset(22, 22, 8, 8);
        SetControl(popupLayout);
        Fit(popup);
        var popupText = AddText(Rect("Label", popup), "+20  +8 tip", labelFont, 30f, Hex("0F2A1B"), TextAlignmentOptions.Center);

        var comp = score.gameObject.AddComponent<ScorePanel>();
        var so = new SerializedObject(comp);
        so.FindProperty("scoreLabel").objectReferenceValue = scoreText;
        so.FindProperty("servePopup").objectReferenceValue = popupGroup;
        so.FindProperty("servePopupLabel").objectReferenceValue = popupText;
        so.ApplyModifiedPropertiesWithoutUndo();
    }

    static void BuildGesture(RectTransform parent)
    {
        var pillRect = Rect("Gesture", parent);
        Place(pillRect, new Vector2(0.5f, 0f), new Vector2(0.5f, 0f), new Vector2(0f, 12f), new Vector2(180f, 76f));
        pillRect.gameObject.AddComponent<CanvasGroup>();
        AddShadow(AddImage(pillRect, pill, Butter));
        var layout = pillRect.gameObject.AddComponent<HorizontalLayoutGroup>();
        layout.padding = new RectOffset(36, 36, 18, 18);
        SetControl(layout);
        Fit(pillRect);
        var label = AddText(Rect("Label", pillRect), "Chop", labelFont, 34f, Ink, TextAlignmentOptions.Center);

        var comp = pillRect.gameObject.AddComponent<GesturePill>();
        var so = new SerializedObject(comp);
        so.FindProperty("label").objectReferenceValue = label;
        so.ApplyModifiedPropertiesWithoutUndo();
    }

    static void BuildTimer(RectTransform parent)
    {
        var panel = Rect("Timer", parent);
        Place(panel, new Vector2(1f, 0f), new Vector2(1f, 0f), Vector2.zero, new Vector2(260f, 128f));
        AddShadow(AddImage(panel, rounded, Butter, 40f / 36f));
        var layout = panel.gameObject.AddComponent<HorizontalLayoutGroup>();
        layout.padding = new RectOffset(24, 40, 0, 0);
        layout.spacing = 20f;
        layout.childAlignment = TextAnchor.MiddleLeft;
        layout.childControlWidth = true;
        layout.childControlHeight = false;
        layout.childForceExpandWidth = layout.childForceExpandHeight = false;
        panel.gameObject.AddComponent<ContentSizeFitter>().horizontalFit = ContentSizeFitter.FitMode.PreferredSize;

        // Placeholder stopwatch: dark ring, white face, yellow wedge that shrinks with the round.
        var watch = Rect("Stopwatch", panel);
        watch.sizeDelta = new Vector2(88f, 88f);
        Prefer(watch, 88f, 88f);
        AddImage(watch, circle, Ink);
        var face = Rect("Face", watch);
        Stretch(face, 6f);
        AddImage(face, circle, Color.white);
        var wedgeRect = Rect("Wedge", watch);
        Stretch(wedgeRect, 6f);
        var wedge = AddImage(wedgeRect, circle, Gold);
        wedge.type = Image.Type.Filled;
        wedge.fillMethod = Image.FillMethod.Radial360;
        wedge.fillOrigin = (int)Image.Origin360.Top;
        wedge.fillClockwise = true;

        var time = Rect("Time", panel);
        time.sizeDelta = new Vector2(130f, 80f);
        var timeText = AddText(time, "3:00", numberFont, 60f, Ink, TextAlignmentOptions.MidlineLeft);

        var comp = panel.gameObject.AddComponent<TimerPanel>();
        var so = new SerializedObject(comp);
        so.FindProperty("timeLabel").objectReferenceValue = timeText;
        so.FindProperty("wedge").objectReferenceValue = wedge;
        so.ApplyModifiedPropertiesWithoutUndo();
    }

    static HudIcons BuildIcons()
    {
        EnsureFolder("Assets/Art");
        EnsureFolder(ArtFolder);
        var icons = AssetDatabase.LoadAssetAtPath<HudIcons>(IconsPath);
        if (icons != null) return icons; // keep the sprites you've already dropped in

        icons = ScriptableObject.CreateInstance<HudIcons>();
        icons.ingredients = new List<HudIcons.IngredientIcon>
        {
            new HudIcons.IngredientIcon { type = IngredientType.Carrot, placeholderColor = Hex("FF8A3D") },
            new HudIcons.IngredientIcon { type = IngredientType.Potato, placeholderColor = Hex("C99A5B") },
            new HudIcons.IngredientIcon { type = IngredientType.Beef, placeholderColor = Hex("C9473F") },
            new HudIcons.IngredientIcon { type = IngredientType.CurryCube, placeholderColor = Hex("5A3A1B") },
        };
        icons.dishes = new List<HudIcons.DishIcon>();
        foreach (var name in DishNames()) icons.dishes.Add(new HudIcons.DishIcon { dish = name });
        AssetDatabase.CreateAsset(icons, IconsPath);
        return icons;
    }

    // The dish names typed on the Pot (from the first prefab with a Pot), so the Dishes list matches them.
    static IEnumerable<string> DishNames()
    {
        foreach (var guid in AssetDatabase.FindAssets("t:Prefab"))
        {
            var go = AssetDatabase.LoadAssetAtPath<GameObject>(AssetDatabase.GUIDToAssetPath(guid));
            var pot = go != null ? go.GetComponentInChildren<Pot>(true) : null;
            if (pot == null) continue;
            var names = new List<string>();
            foreach (var r in pot.Variations) names.Add(r.name);
            return names;
        }
        return new List<string>();
    }

    // ---------- helpers ----------

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

    // Sets the PNG up as a UI sprite (with its 9-slice border) the first time.
    static Sprite LoadSprite(string name, Vector4 border)
    {
        string path = $"{ArtFolder}/{name}.png";
        var importer = AssetImporter.GetAtPath(path) as TextureImporter;
        if (importer == null)
        {
            Debug.LogWarning($"[HUD] {path} not found; using Unity's built-in UI sprite instead.");
            return null;
        }
        if (importer.textureType != TextureImporterType.Sprite || importer.spriteBorder != border)
        {
            importer.textureType = TextureImporterType.Sprite;
            importer.spriteImportMode = SpriteImportMode.Single;
            importer.spriteBorder = border;
            importer.mipmapEnabled = false;
            importer.alphaIsTransparency = true;
            importer.SaveAndReimport();
        }
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

    static void SetControl(HorizontalLayoutGroup layout)
    {
        layout.childControlWidth = layout.childControlHeight = true;
        layout.childForceExpandWidth = layout.childForceExpandHeight = false;
        if (layout.childAlignment == TextAnchor.UpperLeft) layout.childAlignment = TextAnchor.MiddleCenter;
    }

    static void Fit(RectTransform rt)
    {
        var fitter = rt.gameObject.AddComponent<ContentSizeFitter>();
        fitter.horizontalFit = ContentSizeFitter.FitMode.PreferredSize;
        fitter.verticalFit = ContentSizeFitter.FitMode.PreferredSize;
    }

    static void Prefer(RectTransform rt, float width, float height)
    {
        var element = rt.gameObject.AddComponent<LayoutElement>();
        element.preferredWidth = element.minWidth = width;
        element.preferredHeight = element.minHeight = height;
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
