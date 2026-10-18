"""Read-only capture of SANA location and matching court-unit catalogs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .live_browser import LiveBrowserError


_CAPTURE_SCRIPT = r"""async ({settleMs}) => {
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
    let pendingLocationRequests = 0;
    let locationRequestCount = 0;
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this.__catalogLocationRequest = /GetGeoLocation|GetCMSOrganization|GetLocationForAddress|GetUnits/.test(String(url || ''));
        return originalOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function() {
        if (this.__catalogLocationRequest) {
            pendingLocationRequests += 1;
            locationRequestCount += 1;
            this.addEventListener('loadend', () => {
                pendingLocationRequests = Math.max(0, pendingLocationRequests - 1);
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
        NotifyOwnerOrganizationId: person.NotifyOwnerOrganizationId,
        NotifyOwnerOrganizationCode: person.NotifyOwnerOrganizationCode,
        NotifyOwnerOrganizationTitle: person.NotifyOwnerOrganizationTitle,
        GetLocationForAddressId: person.GetLocationForAddressId,
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
        const previousRequestCount = locationRequestCount;
        scope.$apply(() => scope.$select.select(item, false));
        await delay(100);
        if (locationRequestCount > previousRequestCount) {
            await waitFor(
                () => pendingLocationRequests === 0 ? true : null,
                `lookup_request_pending:${name}`
            );
        }
        await delay(settleMs);
    };
    const waitChildren = async (field, timeoutMs = 30000) => await waitFor(() => {
        const items = viewModel[field];
        if (!Array.isArray(items) || !items.length) return null;
        return [...items];
    }, `lookup_children_missing:${field}`, timeoutMs);

    const locations = [];
    const courts = [];
    const jurisdictions = [];
    const issues = [];
    const collectCurrentCourt = location => {
        const locationId = String(location.Id || '').trim();
        const organizationId = String(person.NotifyOwnerOrganizationId || '').trim();
        const code = String(person.NotifyOwnerOrganizationCode || '').trim();
        const title = String(person.NotifyOwnerOrganizationTitle || '').trim();
        if (!organizationId || !code || !title) {
            issues.push({
                code: 'jurisdiction_not_resolved',
                locationId,
                locationCode: String(location.LocationCode || ''),
                locationName: String(location.LocationName || ''),
            });
            return;
        }
        courts.push({code, title, locationId});
        jurisdictions.push({
            locationId,
            locationType: Number(location.LocationType),
            locationCode: String(location.LocationCode || '').trim(),
            locationName: String(location.LocationName || '').trim(),
            parentLocationId: String(location.ParentId || '').trim(),
            getLocationForAddressId: String(person.GetLocationForAddressId || '').trim(),
            organizationId,
            organizationCode: code,
            organizationTitle: title,
        });
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
            collectCurrentCourt({...state, ParentId: ''});
            const counties = await waitChildren('theProvinceTitleList');
            for (const county of counties) {
                viewModel.theCityTitleList = [];
                await choose('province', county);
                locations.push({...county, ParentId: state.Id});
                collectCurrentCourt({...county, ParentId: state.Id});
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
                    await choose('city', city);
                    locations.push({...city, ParentId: county.Id});
                    collectCurrentCourt({...city, ParentId: county.Id});
                }
            }
        }
        return {
            locations,
            courts,
            jurisdictions,
            issues,
        };
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
    extracted_jurisdictions = raw.get("jurisdictions", [])
    if not isinstance(extracted_jurisdictions, list):
        raise LiveBrowserError("lookup_catalog_payload_invalid")
    _write_json(
        output_dir / "jurisdictions.captured.json", extracted_jurisdictions
    )
    issues = raw.get("issues", []) if isinstance(raw, Mapping) else []
    if not isinstance(issues, list):
        raise LiveBrowserError("lookup_catalog_payload_invalid")
    summary = {
        "locations": len(locations),
        "court_units": len(courts),
        "jurisdictions": len(extracted_jurisdictions),
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
    """Wait for the operator to reach the address page, then leave Chrome open."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveBrowserError(
            "playwright_not_installed: install the project with [browser]"
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=False,
            # proxy={"server": "http://127.0.0.1:8080"},
        )
        context = browser.new_context(
            ignore_https_errors=True,
        )
        page = None
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
            summary = capture_catalogs_from_page(page, output_dir)
            print("برداشت تمام شد؛ پس از بررسی، پنجرهٔ Chrome را خودتان ببندید.")
            # Keep the Playwright process alive; closing the browser is the operator's choice.
            page.wait_for_event("close", timeout=0)
            return summary
        except LiveBrowserError:
            if page is not None and not page.is_closed():
                print("خطا رخ داد؛ صفحه برای بررسی باز می‌ماند. مرورگر را خودتان ببندید.")
                page.wait_for_event("close", timeout=0)
            raise
        except Exception as exc:
            if page is not None and not page.is_closed():
                print("خطا رخ داد؛ صفحه برای بررسی باز می‌ماند. مرورگر را خودتان ببندید.")
                page.wait_for_event("close", timeout=0)
            raise LiveBrowserError("lookup_catalog_page_not_reached") from exc


def promote_captured_catalogs(
    capture_dir: Path, lookup_dir: Path
) -> dict[str, int]:
    """Promote verified rows while excluding unresolved capture branches."""

    try:
        locations = json.loads(
            (capture_dir / "locations.captured.json").read_text(encoding="utf-8")
        )
        courts = json.loads(
            (capture_dir / "court_units.captured.json").read_text(encoding="utf-8")
        )
        report = json.loads(
            (capture_dir / "capture_report.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveBrowserError("lookup_catalog_capture_files_invalid") from exc
    if not isinstance(locations, list) or not isinstance(courts, list):
        raise LiveBrowserError("lookup_catalog_capture_files_invalid")
    if not isinstance(report, Mapping) or not isinstance(report.get("issues"), list):
        raise LiveBrowserError("lookup_catalog_capture_report_invalid")
    counts = report.get("counts")
    if not isinstance(counts, Mapping) or (
        counts.get("locations") != len(locations)
        or counts.get("court_units") != len(courts)
    ):
        raise LiveBrowserError("lookup_catalog_capture_count_mismatch")

    excluded_ids = {
        _text(issue.get("parentId"))
        for issue in report["issues"]
        if isinstance(issue, Mapping) and _text(issue.get("parentId"))
    }
    promoted_locations = [
        entry for entry in locations
        if isinstance(entry, Mapping) and _text(entry.get("id")) not in excluded_ids
    ]
    promoted_ids = {_text(entry.get("id")) for entry in promoted_locations}
    if not promoted_locations or any(
        not _valid_promoted_location(entry, promoted_ids)
        for entry in promoted_locations
    ):
        raise LiveBrowserError("lookup_catalog_promotion_location_invalid")
    try:
        preserved_courts = json.loads(
            (lookup_dir / "court_units.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveBrowserError("lookup_catalog_existing_courts_invalid") from exc
    if not isinstance(preserved_courts, list) or not preserved_courts:
        raise LiveBrowserError("lookup_catalog_existing_courts_invalid")

    lookup_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(lookup_dir / "locations.json", promoted_locations)
    return {
        "locations": len(promoted_locations),
        "court_units": len(preserved_courts),
        "excluded_branches": len(excluded_ids),
    }


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


def _valid_promoted_location(
    entry: Mapping[object, object], known_ids: set[str]
) -> bool:
    entry_type = _text(entry.get("type"))
    code = _text(entry.get("code"))
    identity = _text(entry.get("id"))
    parent = _text(entry.get("parent_key"))
    return (
        entry_type in {"province", "county", "city"}
        and bool(identity and code.isdigit() and _text(entry.get("label")))
        and (entry_type == "province" and not parent or parent in known_ids)
    )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    _write_json(temporary, value)
    temporary.replace(path)
