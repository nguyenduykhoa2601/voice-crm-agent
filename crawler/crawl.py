"""Crawl the EspoCRM demo and write data/ui_map.json.

The crawler logs in like a user, opens the create form, list view and a detail
view of each entity, and records what is actually rendered: field cells, labels,
input widgets, required markers and action buttons. The DOM tells us *what is on
screen*; the app's own metadata (fetched with the logged-in session) adds field
types and the exact enum options, which are more reliable than scraping
dropdowns.

Usage:
    uv run python -m crawler.crawl [--entities Contact,Opportunity] [--out data/ui_map.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.crm.browser import CrmBrowser, CrmSession  # noqa: E402

log = logging.getLogger("crawler")

DEFAULT_ENTITIES = ["Contact", "Opportunity", "Account", "Lead"]

# Compound fields whose sub-inputs are filled individually.
COMPOUND_TYPES = {"personName", "address"}

# Reads every field cell of the current record view (create/edit/detail).
JS_READ_CELLS = r"""
() => {
  const record = document.querySelector('#main .record');
  if (!record) return [];
  const cells = record.querySelectorAll('.middle .cell.form-group[data-name], .side .cell.form-group[data-name]');
  return [...cells].filter(cell => cell.getClientRects().length > 0 && !cell.classList.contains('hidden-cell')).map(cell => {
    const label = cell.querySelector('label .label-text, label.control-label');
    const field = cell.querySelector('.field');
    const inputs = field ? [...field.querySelectorAll('input, select, textarea')]
      .filter(el => !el.closest('.selectize-input'))
      .map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        dataName: el.getAttribute('data-name') || '',
        cls: el.className,
        placeholder: el.getAttribute('placeholder') || '',
        selectized: el.classList.contains('selectized'),
      })) : [];
    return {
      name: cell.dataset.name,
      panel: cell.closest('.side') ? 'side' : 'main',
      label: label ? label.textContent.replace(/\s*\*\s*$/, '').trim() : '',
      required: !!cell.querySelector('label .required-sign') ||
                /\*\s*$/.test((cell.querySelector('label') || {}).textContent || ''),
      inputs,
    };
  });
}
"""

JS_READ_BUTTONS = r"""
() => {
  const out = [];
  document.querySelectorAll('#main .record .button-container button[data-action], #main .header-buttons [data-action], #main .record .button-container a[data-action]').forEach(el => {
    out.push({action: el.dataset.action, text: el.textContent.trim(),
              visible: el.offsetParent !== null || el.closest('.dropdown-menu') !== null,
              inMenu: !!el.closest('.dropdown-menu')});
  });
  return out;
}
"""

JS_READ_LIST = r"""
() => {
  const heads = [...document.querySelectorAll('#main .list-container table thead th')]
      .map(th => ({name: th.dataset.name || '', label: th.textContent.trim()}))
      .filter(h => h.name && h.label);
  const kanban = !!document.querySelector('#main .list-kanban');
  const groups = [...document.querySelectorAll('#main .kanban-head .kanban-group-label')].map(e => e.textContent.trim());
  const search = !!document.querySelector('#main input[data-name="textFilter"]');
  const firstLink = document.querySelector('#main .list-container a.link[href*="/view/"], #main .list-kanban a.link[href*="/view/"]');
  return {columns: heads, kanban, kanbanGroups: groups, hasTextSearch: search,
          firstRecordHref: firstLink ? firstLink.getAttribute('href') : null};
}
"""


def _widget(field_type: str, inputs: list[dict]) -> str:
    """Describe how a field is operated on screen."""
    if field_type == "enum":
        return "selectize" if any(i["selectized"] for i in inputs) else "select"
    if field_type == "link":
        return "autocomplete"
    if field_type in ("linkMultiple", "attachmentMultiple"):
        return "autocomplete-multiple"
    if field_type in ("text", "address") or any(i["tag"] == "textarea" for i in inputs):
        return "textarea" if field_type == "text" else "input"
    if field_type == "bool":
        return "checkbox"
    if field_type in ("date", "datetime", "datetimeOptional"):
        return "datepicker"
    return "input"


def _options(meta_field: dict, translations: dict, meta: dict | None = None,
             lang: dict | None = None) -> list[dict] | None:
    opts = meta_field.get("options")
    ref = meta_field.get("optionsReference")
    if not isinstance(opts, list) and ref and meta is not None and "." in ref:
        ref_entity, ref_field = ref.split(".", 1)
        ref_def = meta["entityDefs"].get(ref_entity, {}).get("fields", {}).get(ref_field, {})
        opts = ref_def.get("options")
        if lang is not None:
            translations = {**lang.get(ref_entity, {}).get("options", {}).get(ref_field, {}), **translations}
    if not isinstance(opts, list):
        return None
    return [{"value": o, "label": translations.get(o, o) if o else ""} for o in opts]


def build_entity_map(entity: str, cells: list[dict], meta: dict, lang: dict,
                     buttons: dict, listing: dict) -> dict:
    defs = meta["entityDefs"][entity]["fields"]
    tr = lang.get(entity, {})
    tr_fields = {**lang.get("Global", {}).get("fields", {}), **tr.get("fields", {})}
    tr_options = {**lang.get("Global", {}).get("options", {}), **tr.get("options", {})}
    fields: dict[str, dict] = {}

    for cell in cells:
        cname = cell["name"]
        cdef = defs.get(cname, {})
        ctype = cdef.get("type", "varchar")
        if ctype in COMPOUND_TYPES:
            # Expose each sub-input (firstName, lastName, addressCity, ...) as its own field.
            for inp in cell["inputs"]:
                sub = inp["dataName"]
                if not sub:
                    continue
                sdef = defs.get(sub, {})
                stype = sdef.get("type", "varchar")
                fields[sub] = {
                    "label": tr_fields.get(sub) or inp["placeholder"] or sub,
                    "type": stype,
                    "widget": _widget(stype, [inp]),
                    "required": bool(sdef.get("required")),
                    "cell": cname,
                    "cellLabel": cell["label"],
                    "panel": cell["panel"],
                    "selector": f'.cell[data-name="{cname}"] [data-name="{sub}"]',
                    "options": _options(sdef, tr_options.get(sub, {}), meta, lang),
                    "maxLength": sdef.get("maxLength"),
                }
            continue
        entry: dict[str, Any] = {
            "label": cell["label"] or tr_fields.get(cname, cname),
            "type": ctype,
            "widget": _widget(ctype, cell["inputs"]),
            "required": bool(cdef.get("required")) or cell["required"],
            "cell": cname,
            "cellLabel": cell["label"],
            "panel": cell["panel"],
            "selector": f'.cell[data-name="{cname}"]',
            "options": _options(cdef, tr_options.get(cname, {}), meta, lang),
            "maxLength": cdef.get("maxLength"),
        }
        if ctype == "currency":
            entry["currencyField"] = cname + "Currency"
        if ctype == "link":
            entry["entity"] = meta["entityDefs"][entity].get("links", {}).get(cname, {}).get("entity")
        if ctype == "linkMultiple":
            entry["entity"] = meta["entityDefs"][entity].get("links", {}).get(cname, {}).get("entity")
        if cdef.get("default") is not None:
            entry["default"] = cdef.get("default")
        fields[cname] = {k: v for k, v in entry.items() if v is not None}

    for f in fields.values():
        for k in [k for k, v in f.items() if v is None]:
            del f[k]

    scope_names = lang.get("Global", {}).get("scopeNames", {})
    scope_plural = lang.get("Global", {}).get("scopeNamesPlural", {})
    return {
        "label": scope_names.get(entity, entity),
        "labelPlural": scope_plural.get(entity, entity + "s"),
        "routes": {
            "list": f"#{entity}",
            "create": f"#{entity}/create",
            "detail": f"#{entity}/view/{{id}}",
        },
        "fields": fields,
        "requiredFields": [n for n, f in fields.items() if f.get("required")],
        "actions": buttons,
        "list": {
            "columns": [] if listing.get("kanban") else [
                c for c in listing.get("columns", []) if c["name"] != "r-checkbox"],
            "kanban": listing.get("kanban", False),
            "kanbanGroups": listing.get("kanbanGroups", []),
            "hasTextSearch": listing.get("hasTextSearch", False),
        },
    }


async def crawl_entity(session: CrmSession, entity: str, meta: dict, lang: dict) -> dict:
    page = session.page
    log.info("crawling %s create form", entity)
    await session.goto(f"#{entity}/create", wait_selector="#main .record .cell[data-name]")
    await page.wait_for_timeout(800)
    cells = await page.evaluate(JS_READ_CELLS)
    create_buttons = [b for b in await page.evaluate(JS_READ_BUTTONS) if b["visible"]]

    log.info("crawling %s list view", entity)
    await session.goto(f"#{entity}", wait_selector="#main input[data-name='textFilter']")
    try:
        await page.wait_for_selector("#main a.link[href*='/view/'], #main .no-data", timeout=15000)
    except Exception:  # noqa: BLE001 - empty list is fine
        pass
    listing = await page.evaluate(JS_READ_LIST)

    detail_buttons: list[dict] = []
    if listing.get("firstRecordHref"):
        log.info("crawling %s detail view", entity)
        await session.goto(listing["firstRecordHref"], wait_selector="#main .record .cell[data-name]")
        await page.wait_for_timeout(500)
        detail_buttons = [b for b in await page.evaluate(JS_READ_BUTTONS) if b["visible"]]

    def compact(btns: list[dict]) -> list[dict]:
        seen, out = set(), []
        for b in btns:
            if b["action"] in seen:
                continue
            seen.add(b["action"])
            out.append({"action": b["action"], "text": b["text"], "inMenu": b["inMenu"]})
        return out

    buttons = {"create": compact(create_buttons), "detail": compact(detail_buttons)}
    return build_entity_map(entity, cells, meta, lang, buttons, listing)


async def crawl(entities: list[str], out: Path) -> dict:
    started = time.monotonic()
    browser = CrmBrowser()
    try:
        session = await browser.new_session()
        meta = await session.api("GET", "Metadata")
        lang = await session.api("GET", "I18n?default=")
        settings = await session.api("GET", "Settings")
        result = {
            "source": browser.base_url,
            "crawledAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dateFormat": settings.get("dateFormat"),
            "timeFormat": settings.get("timeFormat"),
            "defaultCurrency": settings.get("defaultCurrency"),
            "currencyList": settings.get("currencyList"),
            "entities": {},
        }
        for entity in entities:
            result["entities"][entity] = await crawl_entity(session, entity, meta, lang)
            log.info("%s: %d fields", entity, len(result["entities"][entity]["fields"]))
    finally:
        await browser.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    log.info("wrote %s in %.1fs", out, time.monotonic() - started)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--entities", default=",".join(DEFAULT_ENTITIES))
    parser.add_argument("--out", default=str(ROOT / "data" / "ui_map.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(crawl([e.strip() for e in args.entities.split(",") if e.strip()], Path(args.out)))


if __name__ == "__main__":
    main()
