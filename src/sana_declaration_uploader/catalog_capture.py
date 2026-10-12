"""Read-only capture of SANA location and matching court-unit catalogs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .live_browser import LiveBrowserError


_CAPTURE_SCRIPT = r"""async ({settleMs, courtWaitMs}) => {
    const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
    const container = name => document.querySelector(
        `.ui-select-container[name="${name}"]`
    );
    const selectScope = name => {
        const node = container(name);
        if (!node) throw new Error(`lookup_container_missing:${name}`);
        const scope = angular.element(node).scope();
        if (!scope || !scope.$select) {
            throw new Error(`lookup_scope_missing:${name}`);
        }
        return scope;
    };
    const pageScope = selectScope('state');
    const viewModel = pageScope.viewModel;
    if (!viewModel || !viewModel.currentPetitionPerson) {
        throw new Error('recipient_model_missing');
    }
    const person = viewModel.currentPetitionPerson;
    const responseCourts = [];
    const harvest = value => {
        if (!value) return;
        if (Array.isArray(value)) {
            value.forEach(harvest);
            return;
        }
        if (typeof value !== 'object') return;
        const code = String(value.Code || '').trim();
        const title = String(value.UnitName || value.Title || '').trim();
        const locationId = String(value.GeoLocationId || '').trim();
        if (code && title && locationId) {
            responseCourts.push({code, title, locationId});
        }
        Object.values(value).forEach(harvest);
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__catalogCaptureUrl = String(url || '');
        return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        if (this.__catalogCaptureUrl.includes('GetCMSOrganization')) {
            this.addEventListener('load', () => {
                try { harvest(JSON.parse(this.responseText)); } catch (_) {}
            }, {once: true});
        }
        return originalSend.apply(this, arguments);
    };
    const original = {
        BasicAvenue: person.BasicAvenue,
        StateId: person.StateId,
        StateTitle: person.StateTitle,
        ProvinceId: person.ProvinceId,
        ProvinceTitle: person.ProvinceTitle,
        CityId: person.CityId,
        CityTitle: person.CityTitle,
        NotifyOwnerOrganizationCode: person.NotifyOwnerOrganizationCode,
        NotifyOwnerOrganizationTitle: person.NotifyOwnerOrganizationTitle,
    };
    const waitFor = async (predicate, code, timeoutMs = 30000) => {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const value = predicate();
            if (value) return value;
            await delay(100);
        }
        throw new Error(code);
    };
    const activate = async name => {
        const scope = selectScope(name);
        const select = scope.$select;
        scope.$apply(() => select.activate());
        if (typeof select.refresh === 'function') {
            const result = select.refresh('');
            if (result && typeof result.then === 'function') await result;
        }
        return await waitFor(
            () => select.items && select.items.length ? [...select.items] : null,
            `lookup_items_missing:${name}`
        );
    };
    const choose = async (name, item) => {
        const scope = selectScope(name);
        scope.$apply(() => scope.$select.select(item, false));
        await delay(settleMs);
    };
    const waitChildren = async (field, timeoutMs = 30000) => await waitFor(() => {
        const items = viewModel[field];
        if (!Array.isArray(items) || !items.length) return null;
        return [...items];
    }, `lookup_children_missing:${field}`, timeoutMs);

    const locations = [];
    const courts = [];
    const issues = [];
    const collectCurrentCourt = locationId => {
        const code = String(person.NotifyOwnerOrganizationCode || '').trim();
        const title = String(person.NotifyOwnerOrganizationTitle || '').trim();
        if (code && title) courts.push({code, title, locationId});
    };
    try {
        person.BasicAvenue = person.BasicAvenue || ' ';
        const states = await activate('state');
        for (const [stateIndex, state] of states.entries()) {
            console.info(
                `[SANA_CAPTURE] استان ${stateIndex + 1}/${states.length}: ` +
                String(state.LocationName || '')
            );
            viewModel.theProvinceTitleList = [];
            await choose('state', state);
            locations.push({...state, ParentId: ''});
            const counties = await waitChildren('theProvinceTitleList');
            for (const county of counties) {
                viewModel.theCityTitleList = [];
                await choose('province', county);
                locations.push({...county, ParentId: state.Id});
                collectCurrentCourt(String(county.Id || ''));
                const cities = await waitChildren(
                    'theCityTitleList', 5000
                ).catch(() => {
                    issues.push({
                        code: 'city_list_empty',
                        parentId: String(county.Id || ''),
                        parentCode: String(county.LocationCode || ''),
                        parentName: String(county.LocationName || ''),
                    });
                    return [];
                });
                for (const city of cities) {
                    person.NotifyOwnerOrganizationCode = '';
                    person.NotifyOwnerOrganizationTitle = '';
                    await choose('city', city);
                    locations.push({...city, ParentId: county.Id});
                    const court = await waitFor(() => {
                        const code = String(
                            person.NotifyOwnerOrganizationCode || ''
                        ).trim();
                        const title = String(
                            person.NotifyOwnerOrganizationTitle || ''
                        ).trim();
                        return code && title ? {code, title} : null;
                    }, 'court_not_resolved', courtWaitMs).catch(() => null);
                    if (court) {
                        courts.push({
                            ...court,
                            locationId: String(city.Id || ''),
                        });
                    }
                }
            }
        }
        return {locations, courts: [...courts, ...responseCourts], issues};
    } finally {
        XMLHttpRequest.prototype.open = originalOpen;
        XMLHttpRequest.prototype.send = originalSend;
        pageScope.$apply(() => Object.assign(person, original));
    }
}"""


def capture_catalogs_from_page(
    page: object,
    output_dir: Path,
    *,
    settle_ms: int = 250,
    court_wait_ms: int = 3_000,
) -> dict[str, int]:
    """Capture catalogs from an already-open recipient address page."""

    try:
        raw = page.evaluate(
            _CAPTURE_SCRIPT,
            {"settleMs": settle_ms, "courtWaitMs": court_wait_ms},
        )
    except Exception as exc:
        raise LiveBrowserError("lookup_catalog_capture_failed") from exc
    locations, courts = normalize_capture_payload(raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "locations.captured.json", locations)
    _write_json(output_dir / "court_units.captured.json", courts)
    issues = raw.get("issues", []) if isinstance(raw, Mapping) else []
    if not isinstance(issues, list):
        raise LiveBrowserError("lookup_catalog_payload_invalid")
    summary = {
        "locations": len(locations),
        "court_units": len(courts),
        "unresolved_branches": len(issues),
    }
    _write_json(
        output_dir / "capture_report.json",
        {"counts": summary, "issues": issues},
    )
    return summary


def normalize_capture_payload(
    raw: object,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not isinstance(raw, Mapping):
        raise LiveBrowserError("lookup_catalog_payload_invalid")
    raw_locations = raw.get("locations")
    raw_courts = raw.get("courts")
    if not isinstance(raw_locations, list) or not isinstance(raw_courts, list):
        raise LiveBrowserError("lookup_catalog_payload_invalid")

    locations: dict[str, dict[str, str]] = {}
    type_names = {2: "province", 3: "county", 5: "city"}
    for item in raw_locations:
        if not isinstance(item, Mapping):
            raise LiveBrowserError("lookup_catalog_location_invalid")
        location_type = _integer(item.get("LocationType"))
        entry_type = type_names.get(location_type)
        key = _text(item.get("Id"))
        code = _text(item.get("LocationCode"))
        name = _text(item.get("LocationName"))
        if not entry_type or not key or not code or not name:
            raise LiveBrowserError("lookup_catalog_location_invalid")
        label = _location_label(name, entry_type)
        entry = {
            "key": key,
            "label": label,
            "type": entry_type,
            "parent_key": _text(item.get("ParentId")),
            "id": key,
            "code": code,
            "source": "authenticated SANA address combos",
        }
        previous = locations.get(key)
        if previous is not None and previous != entry:
            raise LiveBrowserError("lookup_catalog_location_conflict")
        locations[key] = entry

    if not locations or not any(
        entry["type"] == "city" for entry in locations.values()
    ):
        raise LiveBrowserError("lookup_catalog_capture_incomplete")
    for entry in locations.values():
        if entry["type"] != "province" and entry["parent_key"] not in locations:
            raise LiveBrowserError("lookup_catalog_parent_missing")

    courts: dict[tuple[str, str], dict[str, str]] = {}
    for item in raw_courts:
        if not isinstance(item, Mapping):
            raise LiveBrowserError("lookup_catalog_court_invalid")
        code = _text(item.get("code"))
        label = _text(item.get("title"))
        location_id = _text(item.get("locationId"))
        if not code or not label or location_id not in locations:
            raise LiveBrowserError("lookup_catalog_court_invalid")
        identity = (code, location_id)
        courts[identity] = {
            "key": f"{code}:{location_id}",
            "label": label,
            "code": code,
            "location_id": location_id,
            "source": "authenticated SANA address selection",
        }

    if not courts:
        raise LiveBrowserError("lookup_catalog_capture_incomplete")

    location_rows = sorted(
        locations.values(), key=lambda entry: (entry["code"], entry["id"])
    )
    court_rows = sorted(
        courts.values(), key=lambda entry: (entry["code"], entry["location_id"])
    )
    return location_rows, court_rows


def run_interactive_catalog_capture(
    output_dir: Path,
    *,
    navigation_timeout_ms: int = 15 * 60 * 1_000,
) -> dict[str, int]:
    """Open Chrome and wait for the operator to reach the address-combo page."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveBrowserError(
            "playwright_not_installed: install the project with [browser]"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto("about:blank")
            page.on(
                "console",
                lambda message: print(message.text)
                if message.text.startswith("[SANA_CAPTURE]") else None,
            )
            page.locator('.ui-select-container[name="state"]').wait_for(
                state="attached", timeout=navigation_timeout_ms
            )
            return capture_catalogs_from_page(page, output_dir)
        except LiveBrowserError:
            raise
        except Exception as exc:
            raise LiveBrowserError("lookup_catalog_page_not_reached") from exc
        finally:
            context.close()
            browser.close()


def _location_label(name: str, entry_type: str) -> str:
    if entry_type == "province" and not name.startswith("استان "):
        return f"استان {name}"
    if entry_type == "county" and not name.startswith("شهرستان "):
        return f"شهرستان {name}"
    return name


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _integer(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
