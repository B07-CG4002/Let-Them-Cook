using System;
using System.Collections.Generic;
using UnityEngine;

// The pictures the order tickets use: one per ingredient, one per dish. A ScriptableObject (Assets > Create >
// OvercookAR > HUD Icons); the HUD prefab builder makes one at Assets/Art/HUD/HudIcons.asset.
// Until a sprite is set, the ticket keeps its placeholder shape tinted with Placeholder Color, so orders can
// still be told apart while testing. Once a sprite is set it is shown untinted.
[CreateAssetMenu(menuName = "OvercookAR/HUD Icons", fileName = "HudIcons")]
public class HudIcons : ScriptableObject
{
    [Serializable]
    public class IngredientIcon
    {
        public IngredientType type;
        public Sprite sprite;
        public Color placeholderColor = Color.gray;
    }

    [Serializable]
    public class DishIcon
    {
        [Tooltip("Exactly as typed in the Pot's Variations.")]
        public string dish;
        public Sprite sprite;
    }

    public List<IngredientIcon> ingredients = new List<IngredientIcon>();
    [Tooltip("A picture per dish. A dish not listed here uses Soup Sprite or Curry Sprite.")]
    public List<DishIcon> dishes = new List<DishIcon>();
    public Sprite soupSprite;
    public Sprite currySprite;
    public Color soupPlaceholderColor = new Color32(0xF5, 0xA1, 0x3A, 0xFF);
    public Color curryPlaceholderColor = new Color32(0x8A, 0x4E, 0x1C, 0xFF);

    public Sprite Ingredient(IngredientType type)
    {
        foreach (var i in ingredients)
            if (i.type == type) return i.sprite;
        return null;
    }

    public Color IngredientPlaceholder(IngredientType type)
    {
        foreach (var i in ingredients)
            if (i.type == type) return i.placeholderColor;
        return Color.gray;
    }

    public Color DishPlaceholder(Pot.Recipe recipe) =>
        recipe != null && recipe.type == Pot.RecipeType.Curry ? curryPlaceholderColor : soupPlaceholderColor;

    public Sprite Dish(Pot.Recipe recipe)
    {
        if (recipe == null) return null;
        foreach (var d in dishes)
            if (d.dish == recipe.name && d.sprite != null) return d.sprite;
        return recipe.type == Pot.RecipeType.Curry ? currySprite : soupSprite;
    }
}
