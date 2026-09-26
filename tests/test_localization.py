import pytest
import json
import re
from collections import Counter
from pathlib import Path

from app.localization import (
    INGREDIENT_SECTION_NAMES,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGE_CODES,
    UNTITLED_RECIPE_NAMES,
    ingredient_section_name,
    language_name,
    normalize_language,
    untitled_recipe_name,
)


_PARTIAL_ES_KEYS = {
    "Couldn't connect to the server. Check your connection and try again.",
    "Apple couldn't verify this sign-in. Please start Apple sign-in again.",
    "The service is temporarily unavailable. Please try again shortly.",
    "The server returned an invalid sign-in response. Please try again.",
    "We couldn't save your sign-in. Please try again.",
    "We couldn't create your account. Please try again.",
    "Only HTTPS links can be imported.",
    "This link can't be opened because it doesn't use HTTPS.",
}
_PARTIAL_ES_CA_KEYS = {
    "Retry",
    "Free plan: %d recipe(s) per year. Upgrade to Pro.",
    "Pro fair-use limit reached. Try again after %@.",
    "Imports are temporarily paused. Your delivery is saved; try again later.",
    "Rate limit reached. Your import is saved and will retry automatically.",
    "Your Pro access is syncing. Restore purchases or refresh access; your library remains available.",
    "Restore purchases / refresh access",
    "Keeps your saved recipes and pending import available.",
    "Purchases could not be restored. Check your Apple account and try again.",
    "Library refresh failed. Showing saved recipes; try again when connected.",
    "Free plan: %d recipe(s) per year. Limit resets %@. Upgrade to Pro.",
    "Saved recipes may be out of date.",
    "Last updated",
    "Your saved recipes remain available while refresh runs.",
    "Notify me when ready",
    "Notifications are off. Your recipe will still appear in your library.",
    "Open Settings",
    "Notifications on",
}
_PARTIAL_NOTIFICATIONS = {
    "Your recipe is ready",
    "Open ReciApp to find it in your library.",
}
from app.models import ExtractRequest
from app.localization import build_recipe_prompt


def test_allowlist_has_fifty_app_store_locales():
    expected = {
        "ar", "bn", "ca", "zh-Hans", "zh-Hant", "hr", "cs", "da", "nl",
        "en-AU", "en-CA", "en-GB", "en-US", "fi", "fr-FR", "fr-CA", "de", "el",
        "gu", "he", "hi", "hu", "id", "it", "ja", "kn", "ko", "ms", "ml", "mr",
        "nb", "or", "pl", "pt-BR", "pt-PT", "pa", "ro", "ru", "sk", "sl",
        "es-MX", "es-ES", "sv", "ta", "te", "th", "tr", "uk", "ur", "vi",
    }
    assert len(SUPPORTED_LANGUAGE_CODES) == 50
    assert len(set(SUPPORTED_LANGUAGE_CODES)) == 50
    assert set(SUPPORTED_LANGUAGE_CODES) == expected
    assert set(LANGUAGE_NAMES) == expected


def test_language_normalization_handles_regions_and_unknown_values():
    assert normalize_language("es-ES") == "es-ES"
    assert normalize_language("pt_br") == "pt-BR"
    assert normalize_language("pt-PT") == "pt-PT"
    assert normalize_language("zh-Hant-TW") == "zh-Hant"
    assert normalize_language("ar-SA") == "ar"
    assert normalize_language("de-DE") == "de"
    assert normalize_language("no") == "nb"
    assert normalize_language("en") == "en-US"
    assert normalize_language("not-a-language") == "en-US"
    assert language_name("es") == "Spanish"


def test_extract_request_carries_language():
    request = ExtractRequest(url="https://youtube.com/watch?v=abc", language="ja")
    assert request.language == "ja"


def test_recipe_prompt_requires_target_language():
    prompt = build_recipe_prompt("German", "{}")
    assert "in German" in prompt
    assert "title, description, ingredient names" in prompt
    assert "to taste" in prompt
    assert "Never use '-', '—'" in prompt
    assert "density_g_per_ml" in prompt
    assert "Split quantity and unit" in prompt
    assert "g, kg, ml, l, cup, tbsp, tsp, fl oz, oz, lb" in prompt


def test_fallback_recipe_copy_is_localized():
    from app.localization import OPTIONAL_SECTION_NAMES, TO_TASTE_QUANTITIES, to_taste_quantity

    assert untitled_recipe_name("ja") == "無題のレシピ"
    assert ingredient_section_name("fr-FR") == "Ingrédients"
    assert to_taste_quantity("es-ES") == "al gusto"
    assert to_taste_quantity("it") == "q.b."
    assert set(SUPPORTED_LANGUAGE_CODES) == set(UNTITLED_RECIPE_NAMES)
    assert set(SUPPORTED_LANGUAGE_CODES) == set(INGREDIENT_SECTION_NAMES)
    assert set(SUPPORTED_LANGUAGE_CODES) == set(OPTIONAL_SECTION_NAMES)
    assert set(SUPPORTED_LANGUAGE_CODES) == set(TO_TASTE_QUANTITIES)


def test_ios_catalog_covers_all_supported_locales_and_placeholders(ios_root: Path):
    catalog_path = ios_root / "ReciApp" / "Localizable.xcstrings"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["strings"]

    source_only_keys = {
        key for key, entry in catalog["strings"].items()
        if not entry.get("localizations")
    }
    assert source_only_keys == {
        "→ cups / oz",
        "→ g / ml",
        "Apple still remembers the deleted account. Open Settings, tap your name, tap Sign in with Apple, select ReciApp, then tap Delete. Return here and try again.",
        "Convert to cups / oz",
        "Convert to grams / ml",
        "OK",
        "Reset Sign in with Apple",
        "View Apple instructions",
    }

    token_pattern = re.compile(r"%(?:\d+\$)?(?:lld|d|@|%)")

    for key, entry in catalog["strings"].items():
        source_tokens = [re.sub(r"%(?:\d+\$)?", "%", token) for token in token_pattern.findall(key)]
        localizations = entry.get("localizations", {})
        locales = set(localizations)
        if not locales:
            continue
        expected_partial_locales = None
        if key in _PARTIAL_ES_KEYS:
            expected_partial_locales = {"es-ES", "es-MX"}
        elif key in _PARTIAL_ES_CA_KEYS:
            expected_partial_locales = {"ca", "es-ES", "es-MX"}
        elif key in _PARTIAL_NOTIFICATIONS:
            expected_partial_locales = set(SUPPORTED_LANGUAGE_CODES) - {"en-AU", "en-CA", "en-GB", "en-US"}
        assert (
            locales == set(SUPPORTED_LANGUAGE_CODES)
            or locales == {"en"}
            or locales == expected_partial_locales
        ), key
        for locale in locales:
            value = localizations[locale]["stringUnit"]["value"]
            localized_tokens = [re.sub(r"%(?:\d+\$)?", "%", token) for token in token_pattern.findall(value)]
            assert Counter(localized_tokens) == Counter(source_tokens), (locale, key, value)


def test_ios_folder_move_supports_multiple_selection(ios_root: Path):
    home = (ios_root / "ReciApp" / "Views" / "HomeView.swift").read_text(encoding="utf-8")
    catalog = json.loads((ios_root / "ReciApp" / "Localizable.xcstrings").read_text(encoding="utf-8"))
    assert "moveRecipes(withIDs:" in home
    assert "selectedIDs" in home
    assert "FolderSelectionBar" in home
    assert "Select" in catalog["strings"]
    assert "Select All" in catalog["strings"]
    assert "Favorite" in catalog["strings"]
    assert "Tags" in catalog["strings"]
    assert "toggleFavorite(" in home
    assert "RecipeCoverTags" in home


def test_share_extension_uses_shared_localization_resources(ios_root: Path):
    share_controller = (ios_root / "ReciAppShare" / "ShareViewController.swift").read_text(encoding="utf-8")
    project = (ios_root / "ReciApp.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
    localization = (ios_root / "ReciApp" / "Services" / "Localization.swift").read_text(encoding="utf-8")
    app_model = (ios_root / "ReciApp" / "ViewModels" / "AppViewModel.swift").read_text(encoding="utf-8")
    app_entitlements = (ios_root / "ReciApp" / "ReciApp.entitlements").read_text(encoding="utf-8")
    share_entitlements = (ios_root / "ReciAppShare" / "ReciAppShare.entitlements").read_text(encoding="utf-8")
    assert "ReciLocalization.string(\"Opening ReciApp…\")" in share_controller
    assert "No se encontró un enlace compatible" not in share_controller
    assert "Localizable.xcstrings in Resources" in project
    assert "Localization.swift" in project
    assert project.count("Localizable.xcstrings in Resources") >= 2
    assert 'appGroupIdentifier = "group.com.membri.reciapp"' in localization
    assert "group.com.membri.reciapp" in app_entitlements
    assert "group.com.membri.reciapp" in share_entitlements
    assert "localizedCategoryName" in app_model
    app_entry = (ios_root / "ReciApp" / "ReciAppApp.swift").read_text(encoding="utf-8")
    assert ".environmentObject(app)" in app_entry
    recipe_detail = (ios_root / "ReciApp" / "Views" / "RecipeDetailView.swift").read_text(encoding="utf-8")
    assert "ReciLocalization.string(" in recipe_detail
    models = (ios_root / "ReciApp" / "Models" / "Models.swift").read_text(encoding="utf-8")
    assert "localizedServerMessage" in models
    profile = (ios_root / "ReciApp" / "Views" / "ProfileView.swift").read_text(encoding="utf-8")
    assert "AppLanguageStore.selection = newValue" in profile
