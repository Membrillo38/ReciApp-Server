import re
from pathlib import Path


def _normalize_path(path: str) -> str:
    path = re.sub(r"\\\([^)]*\)", "{}", path)
    path = re.sub(r"\{[^}]+\}", "{}", path)
    return path if path.startswith("/") else "/" + path


def test_ios_api_routes_exist_in_fastapi(ios_root: Path):
    from fastapi.routing import APIRoute
    from app.main import app as fastapi_app

    server_routes = {
        (method.upper(), _normalize_path(route.path)): route
        for route in fastapi_app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }

    client_routes = set()
    api = (ios_root / "ReciApp/Services/APIClient.swift").read_text(encoding="utf-8")
    auth = (ios_root / "ReciApp/Services/AuthService.swift").read_text(encoding="utf-8")

    for path, arguments in re.findall(r'request\("([^"]+)"([^\n]*)', api):
        method = re.search(r'method:\s*"([A-Z]+)"', arguments)
        client_routes.add((method.group(1) if method else "GET", _normalize_path(path)))
    for path in re.findall(r'post\(\s*"([^"]+)"', auth):
        client_routes.add(("POST", _normalize_path(path)))

    assert len(client_routes) == 16
    assert client_routes <= server_routes.keys()
    assert all(server_routes[contract].response_model is not None for contract in client_routes)


def test_ios_storekit_catalog_matches_server_restore_allowlist(ios_root: Path):
    from app.apple_notifications import _RECIAPP_SUBSCRIPTION_PRODUCTS

    pricing = (ios_root / "ReciApp/Services/PricingExperiment/PricingExperimentManager.swift").read_text(
        encoding="utf-8"
    )

    def string_array(name: str) -> set[str]:
        match = re.search(rf"static let {name} = \[(.*?)\]", pricing, re.DOTALL)
        assert match, f"missing PricingCatalog.{name}"
        return set(re.findall(r'"([^"]+)"', match.group(1)))

    weekly_products = string_array("weeklyProductIDs")
    annual_base_products = string_array("annualBaseProductIDs")
    legacy_products = string_array("legacySubscriptionProductIDs")
    suffixes = re.search(
        r'let suffix = offer == \.trial3Days \? "([^"]+)" : "([^"]+)"',
        pricing,
    )
    assert suffixes, "missing annual offer suffixes"
    ios_products = weekly_products | {
        f"{base}{suffix}"
        for base in annual_base_products
        for suffix in suffixes.groups()
    }
    ios_products |= legacy_products

    assert ios_products == _RECIAPP_SUBSCRIPTION_PRODUCTS
