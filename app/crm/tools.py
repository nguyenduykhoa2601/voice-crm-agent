"""Verified CRM actions exposed to the voice model as function calls.

Every tool acts through the visible UI (so the reviewer can follow along in the
live view) and then reads the result back from the DOM. The returned
`observation` describes what is actually on screen after the action; the model
must rely on it and never assume success.

Public API:
    TOOL_DECLARATIONS               list of google-genai FunctionDeclaration kwargs
    await run_tool(session, name, args) -> {"ok": bool, "observation": str, ...}

Helpers for the eval harness (not exposed to the model):
    create_seed_opportunity, find_ids_by_name, delete_record, delete_by_name
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser import ACTION_TIMEOUT_MS, CrmError, CrmSession
from .ui_map import UiMap, get_ui_map, norm

log = logging.getLogger(__name__)

TOOL_TIMEOUT_S = 45.0  # Hard guard per tool call; individual steps time out after 15 s.
SAVE_TIMEOUT_S = 15.0
RECORD = "#main .record"
ID_RE = re.compile(r"^[0-9a-f]{17}$")

# --------------------------------------------------------------------------- declarations

_ENTITY_ENUM = ["Contact", "Opportunity", "Account", "Lead"]

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_screen",
        "description": (
            "Read what is currently on the CRM screen: page kind (list/detail/edit/create), entity, "
            "record name, visible field values, validation errors, open dialogs. Use it whenever you are "
            "unsure of the current state, after an error, or before answering questions about the screen."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "open_create_form",
        "description": (
            "Open the empty 'create' form for a new record of the given entity. Returns the form's fields "
            "and which are required. Unsaved changes on a form that is currently open are discarded. "
            "If the user already gave values, pass them in `fields` so the form is opened and filled in "
            "one step (same format as fill_fields; values are read back)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "entity": {"type": "STRING", "enum": _ENTITY_ENUM,
                           "description": "Record type to create."},
                "fields": {
                    "type": "ARRAY",
                    "description": "Optional values the user already gave, filled right after the form opens.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "field": {"type": "STRING", "description": "Field name or label, e.g. lastName."},
                            "value": {"type": "STRING", "description": "Value to enter."},
                        },
                        "required": ["field", "value"],
                    },
                },
            },
            "required": ["entity"],
        },
    },
    {
        "name": "fill_fields",
        "description": (
            "Type values into fields of the form that is open now (create or edit). Fields can be given by "
            "field name or on-screen label, e.g. firstName, lastName, emailAddress, phoneNumber, account, "
            "stage, amount, closeDate. Use it again to correct a value. Returns the value read back from "
            "each field; check them. Dates may be given as YYYY-MM-DD."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "fields": {
                    "type": "ARRAY",
                    "description": "Fields to fill, in order.",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "field": {"type": "STRING", "description": "Field name or label, e.g. lastName or 'Last Name'."},
                            "value": {"type": "STRING", "description": "Value to enter. Empty string clears the field."},
                        },
                        "required": ["field", "value"],
                    },
                },
            },
            "required": ["fields"],
        },
    },
    {
        "name": "save",
        "description": (
            "Click Save on the open form, then wait for the result. Returns ok=false with the CRM's "
            "validation messages if the form is not valid, or if the CRM warns about a possible duplicate. "
            "On success returns the values read back from the saved record's detail view and its id/URL. "
            "Never claim the record is saved unless this returns ok=true."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "confirm_duplicate": {
                    "type": "BOOLEAN",
                    "description": "Set true only after the user confirmed creating the record despite a duplicate warning.",
                },
            },
        },
    },
    {
        "name": "cancel",
        "description": "Cancel the open create/edit form without saving (discards unsaved changes).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "find_records",
        "description": (
            "Search records of an entity by name using the list view search box. Returns up to 10 matches "
            "with id and name (and stage for opportunities)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "entity": {"type": "STRING", "enum": _ENTITY_ENUM},
                "query": {"type": "STRING", "description": "Name or beginning of the name to search for."},
            },
            "required": ["entity", "query"],
        },
    },
    {
        "name": "open_record",
        "description": "Open the detail view of one record, by id or by name. Returns its field values.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "entity": {"type": "STRING", "enum": _ENTITY_ENUM},
                "record": {"type": "STRING", "description": "Record id, or its exact name."},
            },
            "required": ["entity", "record"],
        },
    },
    {
        "name": "edit_record",
        "description": "Switch the record currently shown in detail view into edit mode (clicks Edit).",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "set_stage",
        "description": (
            "Move an opportunity to another pipeline stage and save it, verified on the detail view. "
            "Works on the opportunity currently open, or opens the one given in 'opportunity' first. "
            "Stages: Prospecting, Qualification, Proposal, Negotiation, Closed Won, Closed Lost."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "stage": {"type": "STRING", "description": "Target stage."},
                "opportunity": {"type": "STRING", "description": "Optional opportunity id or exact name to open first."},
            },
            "required": ["stage"],
        },
    },
]

TOOL_NAMES = [d["name"] for d in TOOL_DECLARATIONS]

# --------------------------------------------------------------------------- page reading

JS_STATE = r"""
() => {
  const vis = el => !!el && el.getClientRects().length > 0 && !el.closest('[data-vca-stale]');
  const hash = decodeURIComponent(location.hash.replace(/^#/, ''));
  const parts = hash.split('/');
  const entity = parts[0] || '';
  const saveVisible = [...document.querySelectorAll('#main .record button[data-action="save"]')].some(vis);
  const hasRecord = [...document.querySelectorAll('#main .record')].some(vis);
  const hasList = [...document.querySelectorAll('#main .list-container, #main .list-kanban')].some(vis);
  let kind = 'other';
  if (!entity || entity === 'Home') kind = 'home';
  else if (parts[1] === 'create') kind = 'create';
  else if (parts[1] === 'view' || parts[1] === 'edit') kind = saveVisible ? 'edit' : 'detail';
  else if (!parts[1] || parts[1] === 'list') kind = hasList ? 'list' : 'other';
  if ((kind === 'create' || kind === 'edit' || kind === 'detail') && !hasRecord) kind = 'loading';
  const crumbs = [...document.querySelectorAll('#main .header-breadcrumbs .breadcrumb-item')].filter(vis);
  const title = crumbs.length ? crumbs[crumbs.length - 1].textContent.trim() : '';
  const modal = [...document.querySelectorAll('.modal-dialog')].filter(vis).map(m => {
    const body = m.querySelector('.modal-body');
    const head = m.querySelector('.modal-header, .modal-title');
    return {title: head ? head.textContent.trim() : '',
            text: (body ? body.innerText : m.innerText).replace(/\s+/g, ' ').trim().slice(0, 400),
            buttons: [...m.querySelectorAll('footer button, .modal-footer button')].map(b => b.dataset.name || b.textContent.trim())};
  })[0] || null;
  return {hash, entity, kind, id: (parts[1] === 'view' || parts[1] === 'edit') ? (parts[2] || '') : '',
          title, modal, url: location.href, loginPage: vis(document.querySelector('#btn-login'))};
}
"""

JS_ERRORS = r"""
() => {
  const vis = el => !!el && el.getClientRects().length > 0 && !el.closest('[data-vca-stale]');
  const msgs = [];
  document.querySelectorAll('.popover').forEach(p => {
    if (vis(p)) { const t = p.innerText.replace(/\s+/g, ' ').trim(); if (t) msgs.push(t); }
  });
  const invalid = [...document.querySelectorAll('#main .record .cell.has-error, #main .record .has-error[data-name]')]
    .filter(vis).map(c => {
      const l = c.querySelector('label .label-text') || c.querySelector('label');
      return l ? l.textContent.replace(/\*/g, '').trim() : (c.dataset.name || '');
    }).filter(Boolean);
  const alerts = [...document.querySelectorAll('#notification .alert, .alert-danger, .alert-warning')]
    .filter(vis).map(a => a.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean);
  return {messages: [...new Set(msgs)], invalidFields: [...new Set(invalid)], alerts: [...new Set(alerts)]};
}
"""

# Reads all field cells of the record view. Works in both edit and detail mode.
JS_READ_CELLS = r"""
() => {
  const vis = el => !!el && el.getClientRects().length > 0 && !el.closest('[data-vca-stale]');
  const record = [...document.querySelectorAll('#main .record')].find(vis);
  if (!record) return [];
  const cells = [...record.querySelectorAll('.middle .cell.form-group[data-name], .side .cell.form-group[data-name]')].filter(vis);
  return cells.map(cell => {
    const labelEl = cell.querySelector('label .label-text') || cell.querySelector('label');
    const label = labelEl ? labelEl.textContent.replace(/\*/g, '').trim() : cell.dataset.name;
    const field = cell.querySelector('.field');
    let value = '';
    const editable = field && field.querySelector('input:not([type="hidden"]), select, textarea');
    if (editable) {
      const parts = [];
      const name = cell.dataset.name;
      if (cell.querySelector('input.email-address, input.phone-number')) {
        cell.querySelectorAll('input.email-address, input.phone-number').forEach(i => { if (i.value) parts.push(i.value); });
      } else {
        field.querySelectorAll('.link-container .list-group-item, .link-container .link-item').forEach(li => {
          const t = li.textContent.replace(/\s+/g, ' ').trim(); if (t) parts.push(t);
        });
        field.querySelectorAll('input, select, textarea').forEach(el => {
          if (el.closest('.selectize-input') || el.type === 'hidden') return;
          if (el.classList.contains('main-element') && el.closest('.add-team, .input-group') &&
              field.querySelector('.link-container')) return;  // link-multiple search box
          if (el.type === 'checkbox') { if (el.checked) parts.push('yes'); return; }
          if (el.tagName === 'SELECT') {
            const o = el.options[el.selectedIndex];
            if (o && o.value && el.dataset.propertyType !== 'type') parts.push(o.textContent.trim());
            return;
          }
          if (el.value) parts.push(el.value);
        });
      }
      value = parts.join(' ');
    } else if (field) {
      value = field.innerText.replace(/\s+/g, ' ').trim();
      if (value === 'None') value = '';
    }
    return {name: cell.dataset.name, label, value,
            required: !!cell.querySelector('label .required-sign'),
            error: cell.classList.contains('has-error')};
  });
}
"""

JS_LIST_ROWS = r"""
(entity) => {
  const vis = el => !!el && el.getClientRects().length > 0 && !el.closest('[data-vca-stale]');
  const seen = new Set(); const rows = [];
  document.querySelectorAll(`#main .list-container a.link[href^="#${entity}/view/"], #main .list-kanban a.link[href^="#${entity}/view/"]`).forEach(a => {
    if (!vis(a)) return;
    const id = a.dataset.id || a.getAttribute('href').split('/').pop();
    if (seen.has(id)) return; seen.add(id);
    const row = {id, name: (a.getAttribute('title') || a.textContent).trim()};
    const col = a.closest('.group-column');
    if (col) row.stage = col.dataset.name;
    const tr = a.closest('tr');
    if (tr) {
      const extra = [...tr.querySelectorAll('td.cell[data-name]')]
        .filter(td => !['name', 'buttons', 'r-checkbox'].includes(td.dataset.name))
        .map(td => td.innerText.replace(/\s+/g, ' ').trim()).filter(Boolean);
      if (extra.length) row.details = extra.join(' | ');
    }
    rows.push(row);
  });
  return rows;
}
"""


async def _state(session: CrmSession) -> dict:
    return await session.page.evaluate(JS_STATE)


async def _errors(session: CrmSession) -> dict:
    return await session.page.evaluate(JS_ERRORS)


async def _cells(session: CrmSession) -> list[dict]:
    return await session.page.evaluate(JS_READ_CELLS)


def _fmt_cells(cells: list[dict], only_filled: bool = False, limit: int = 18) -> str:
    out = []
    for c in cells:
        if only_filled and not c["value"] and not c["required"]:
            continue
        label = c["label"] + ("*" if c["required"] else "")
        out.append(f"{label}: {c['value'] or '(empty)'}")
    return "; ".join(out[:limit])


def _error_text(errs: dict) -> str:
    parts = []
    if errs["messages"]:
        parts.append("; ".join(errs["messages"]))
    if errs["invalidFields"]:
        parts.append("Fields marked invalid: " + ", ".join(errs["invalidFields"]))
    alerts = [a for a in errs["alerts"] if a not in errs["messages"]]
    if alerts:
        parts.append("Notification: " + " ".join(alerts))
    return ". ".join(parts)


def _form_snapshot(state: dict, cells: list[dict]) -> dict:
    return {"entity": state.get("entity"), "kind": state.get("kind"), "url": state.get("url"),
            "fields": {c["label"]: c["value"] for c in cells}}


async def _wait_state(session: CrmSession, predicate, timeout_s: float = SAVE_TIMEOUT_S,
                      interval: float = 0.2) -> Optional[dict]:
    deadline = time.monotonic() + timeout_s
    while True:
        st = await _state(session)
        if predicate(st):
            return st
        if time.monotonic() > deadline:
            return None
        await asyncio.sleep(interval)


async def _visible(page: Page, selector: str) -> Optional[Locator]:
    loc = page.locator(selector)
    for i in range(await loc.count()):
        item = loc.nth(i)
        try:
            if await item.is_visible():
                return item
        except PlaywrightError:
            continue
    return None


async def _click(session: CrmSession, loc: Locator, pause_ms: int = 250) -> None:
    await session.highlight(loc)
    await session.page.wait_for_timeout(pause_ms)  # Let the viewer see the highlight.
    await loc.click(timeout=ACTION_TIMEOUT_MS)


async def _type(session: CrmSession, loc: Locator, value: str) -> None:
    """Clear and type like a human: visible keystrokes, total time capped."""
    await session.highlight(loc, 1500)
    await loc.click(timeout=ACTION_TIMEOUT_MS)
    await loc.fill("")
    if value:
        delay = max(5, min(28, int(2500 / max(len(value), 1))))
        await loc.press_sequentially(value, delay=delay)
    await loc.evaluate("el => { el.dispatchEvent(new Event('change', {bubbles: true})); el.blur(); }")


async def _dismiss_modal(session: CrmSession, prefer: tuple[str, ...]) -> Optional[str]:
    """Click the first matching button of a visible modal; returns its name."""
    for name in prefer:
        btn = await _visible(session.page, f".modal-dialog footer button[data-name='{name}'], "
                                           f".modal-dialog .modal-footer button[data-name='{name}']")
        if btn:
            await _click(session, btn, 150)
            await session.page.wait_for_timeout(400)
            return name
    return None


async def _describe_screen(session: CrmSession, uimap: UiMap, max_rows: int = 8) -> dict:
    st = await _state(session)
    kind, entity = st["kind"], st["entity"]
    result: dict[str, Any] = {"ok": True, "page": kind, "entity": entity, "url": st["url"]}
    if st.get("loginPage"):
        result.update(observation="The CRM login page is shown.")
        return result
    parts: list[str] = []
    if kind in ("create", "edit", "detail"):
        cells = await _cells(session)
        result["form"] = _form_snapshot(st, cells)
        if st.get("id"):
            result["id"] = st["id"]
        label = {"create": f"New {entity} form (not saved yet)",
                 "edit": f"Editing {entity} '{st['title']}' (unsaved)",
                 "detail": f"{entity} detail view of '{st['title']}' (saved record)"}[kind]
        parts.append(label + ".")
        parts.append("Fields: " + (_fmt_cells(cells, only_filled=(kind == "detail")) or "none"))
        if kind in ("create", "edit"):
            missing = await _missing_required(session, uimap, entity)
            if missing:
                parts.append("Required but empty: " + ", ".join(missing) + ".")
    elif kind == "list":
        try:
            await session.page.wait_for_selector(
                f"#main a.link[href^='#{entity}/view/'], #main .no-data", timeout=5000)
        except PlaywrightTimeout:
            pass
        rows = await session.page.evaluate(JS_LIST_ROWS, entity)
        result["records"] = rows[:max_rows]
        names = ", ".join(r["name"] + (f" [{r['stage']}]" if r.get("stage") else "") for r in rows[:max_rows])
        parts.append(f"{entity} list view, {len(rows)} records visible: {names or 'none'}.")
    elif kind == "loading":
        parts.append(f"The {entity} page is still loading.")
    else:
        parts.append(f"Page: {st['hash'] or 'home'}.")
    errs = await _errors(session)
    err_text = _error_text(errs)
    if err_text:
        parts.append("Errors on screen: " + err_text + ".")
        result["errors"] = errs
    if st.get("modal"):
        parts.append(f"Dialog open: {st['modal']['text']} (buttons: {', '.join(st['modal']['buttons'])}).")
        result["modal"] = st["modal"]
    result["observation"] = " ".join(parts)
    return result


async def _leave_form_if_open(session: CrmSession) -> Optional[str]:
    """Cancel an open create/edit form so navigation is not blocked by a confirm dialog."""
    st = await _state(session)
    if st["kind"] not in ("create", "edit"):
        return None
    cancel = await _visible(session.page, f"{RECORD} button[data-action='cancel'], "
                                          f"{RECORD} button[data-action='cancelEdit']")
    if cancel:
        await _click(session, cancel, 100)
        await session.page.wait_for_timeout(300)
        await _dismiss_modal(session, ("confirm", "yes", "ok"))
    return "An unsaved form was discarded first."


# --------------------------------------------------------------------------- value helpers

def _to_date(value: str, fmt: str) -> Optional[str]:
    """Parse common spoken/ISO date forms into the CRM display format."""
    v = value.strip()
    today = date.today()
    lower = v.lower()
    parsed: Optional[date] = None
    if lower == "today":
        parsed = today
    elif lower == "tomorrow":
        parsed = today + timedelta(days=1)
    else:
        m = re.match(r"^in (\d+) (day|week|month)s?$", lower)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            parsed = today + timedelta(days=n * {"day": 1, "week": 7, "month": 30}[unit])
    if parsed is None:
        candidates = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                      "%d %B %Y", "%d %b %Y", "%B %d", "%b %d", "%d.%m.%Y", "%Y/%m/%d"]
        cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", v)
        for c in candidates:
            try:
                d = datetime.strptime(cleaned, c).date()
                if "%Y" not in c and "%y" not in c:
                    d = d.replace(year=today.year)
                    if d < today:
                        d = d.replace(year=today.year + 1)
                parsed = d
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    py_fmt = fmt.replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d")
    return parsed.strftime(py_fmt)


def _clean_number(value: str) -> str:
    v = value.strip().lower().replace(",", "").replace("$", "").replace("€", "").replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)(k|m)?$", v)
    if not m:
        return value.strip()
    num = float(m.group(1)) * {"k": 1_000, "m": 1_000_000, None: 1}[m.group(2)]
    return str(int(num)) if num == int(num) else str(num)


def _same(a: str, b: str) -> bool:
    if norm(a) == norm(b):
        return True
    try:  # Numbers are re-formatted by the CRM ("2500" -> "2,500.00").
        return float(a.replace(",", "")) == float(b.replace(",", ""))
    except ValueError:
        pass
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    return bool(da) and da == db and not re.search(r"[A-Za-z]", a + b)


def _normalize_fields_arg(raw: Any) -> list[tuple[str, str]]:
    """Accept {"field": value} dicts or [{"field": f, "value": v}] lists."""
    items: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        if "field" in raw and "value" in raw and len(raw) == 2:
            items.append((str(raw["field"]), "" if raw["value"] is None else str(raw["value"])))
        else:
            items.extend((str(k), "" if v is None else str(v)) for k, v in raw.items())
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, dict):
                f = it.get("field") or it.get("name") or it.get("key")
                if f is not None:
                    v = it.get("value")
                    items.append((str(f), "" if v is None else str(v)))
    return items


# --------------------------------------------------------------------------- field filling

def _cell(spec: dict, fname: str) -> str:
    return f"{RECORD} .cell[data-name='{spec.get('cell', fname)}']"


async def _read_field(session: CrmSession, fname: str, spec: dict) -> str:
    cell = _cell(spec, fname)
    widget, ftype = spec.get("widget", "input"), spec.get("type", "varchar")
    page = session.page
    js = """([cell, fname, widget, ftype]) => {
      const vis = el => !!el && el.getClientRects().length > 0 && !el.closest('[data-vca-stale]');
      const c = [...document.querySelectorAll(cell)].find(vis);
      if (!c) return null;
      if (ftype === 'email') { const i = c.querySelector('input.email-address'); return i ? i.value : ''; }
      if (ftype === 'phone') { const i = c.querySelector('input.phone-number'); return i ? i.value : ''; }
      if (widget === 'autocomplete') { const i = c.querySelector(`input[data-name="${fname}Name"]`); return i ? i.value : ''; }
      if (widget === 'autocomplete-multiple') {
        return [...c.querySelectorAll('.link-container .list-group-item, .link-container .link-item')]
          .map(e => e.textContent.replace(/\\s+/g, ' ').trim()).filter(Boolean).join(', ');
      }
      const el = c.querySelector(`[data-name="${fname}"]:not(label):not(.field):not(.cell)`) || c.querySelector('.main-element');
      if (!el) return '';
      if (el.type === 'checkbox') return el.checked ? 'true' : 'false';
      if (el.tagName === 'SELECT') return el.value;
      return el.value;
    }"""
    return await page.evaluate(js, [cell, fname, widget, ftype])


async def _input_locator(session: CrmSession, fname: str, spec: dict) -> Optional[Locator]:
    cell = _cell(spec, fname)
    ftype, widget = spec.get("type"), spec.get("widget")
    if ftype == "email":
        sel = f"{cell} input.email-address"
    elif ftype == "phone":
        sel = f"{cell} input.phone-number"
    elif widget == "autocomplete":
        sel = f"{cell} input[data-name='{fname}Name']"
    elif widget == "autocomplete-multiple":
        sel = f"{cell} input.main-element"
    else:
        sel = f"{cell} input[data-name='{fname}'], {cell} textarea[data-name='{fname}']"
    loc = None
    for _ in range(15):  # Edit-mode widgets render asynchronously; wait up to ~3 s.
        loc = await _visible(session.page, sel)
        if loc is not None:
            break
        await session.page.wait_for_timeout(200)
    if loc is None and widget not in ("autocomplete", "autocomplete-multiple"):
        loc = await _visible(session.page, f"{cell} .main-element")
    return loc


async def _fill_enum(session: CrmSession, fname: str, spec: dict, value: str, label: str) -> Optional[str]:
    """Pick an option in a selectize (or native) select. Returns an error string or None."""
    page = session.page
    cell = _cell(spec, fname)
    select_sel = f"{cell} select[data-name='{fname}']"
    try:
        await page.wait_for_selector(select_sel, state="attached", timeout=5000)
    except PlaywrightTimeout:
        return "the dropdown is not on screen"
    control = page.locator(f"{select_sel} + .selectize-control .selectize-input").first
    try:
        if value and await control.count() and await control.is_visible():
            await session.highlight(control, 1500)
            await control.click(timeout=ACTION_TIMEOUT_MS)
            await page.keyboard.type(label, delay=25)
            opt = page.locator(f"{select_sel} + .selectize-control .selectize-dropdown "
                               f".option[data-value=\"{value}\"]").first
            await opt.wait_for(state="visible", timeout=3000)
            await opt.click(timeout=ACTION_TIMEOUT_MS)
            await page.wait_for_timeout(150)
        elif await page.locator(select_sel).is_visible():
            await page.locator(select_sel).select_option(value)
    except (PlaywrightTimeout, PlaywrightError) as exc:
        log.info("selectize UI path failed for %s: %s", fname, exc)
    current = await page.eval_on_selector(select_sel, "el => el.value")
    if current != value:
        # Fallback: use the widget's own API, then verify again.
        await page.eval_on_selector(select_sel, """(el, v) => {
            if (el.selectize) { el.selectize.setValue(v); el.selectize.blur(); }
            else { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); }
        }""", value)
        await page.wait_for_timeout(150)
    try:
        # Leave the widget so its dropdown closes (no Escape: it cancels edit mode).
        await page.evaluate("() => { const a = document.activeElement; if (a && a.closest('.selectize-input')) a.blur(); }")
    except PlaywrightError:
        pass
    return None


async def _fill_link(session: CrmSession, fname: str, spec: dict, value: str) -> Optional[str]:
    """Type into an autocomplete link field and pick the best suggestion."""
    page = session.page
    loc = await _input_locator(session, fname, spec)
    if loc is None:
        return "the field is not on screen"
    target = spec.get("entity") or "record"
    if not value:
        clear = await _visible(page, f"{_cell(spec, fname)} button[data-action='clearLink']")
        if clear:
            await _click(session, clear, 100)
        return None
    await _type_no_blur(session, loc, value)
    sugg_sel = ".autocomplete-suggestions .autocomplete-suggestion"
    try:
        await page.wait_for_function(
            "(s) => [...document.querySelectorAll(s)].some(e => e.getClientRects().length > 0)",
            arg=sugg_sel, timeout=6000, polling=150)
    except PlaywrightTimeout:
        await loc.fill("")
        await loc.evaluate("el => el.blur()")
        return f"no {target} named '{value}' exists in the CRM"
    texts = await page.evaluate(
        "(s) => [...document.querySelectorAll(s)].filter(e => e.getClientRects().length > 0).map(e => e.textContent.trim())",
        sugg_sel)
    idx = next((i for i, t in enumerate(texts) if norm(t) == norm(value)), None)
    if idx is None:
        idx = next((i for i, t in enumerate(texts) if norm(t).startswith(norm(value))), 0)
    sugg = page.locator(f"{sugg_sel}:visible").nth(idx)
    await session.highlight(sugg, 800)
    await sugg.click(timeout=ACTION_TIMEOUT_MS)
    await page.wait_for_timeout(250)
    return None


async def _type_no_blur(session: CrmSession, loc: Locator, value: str) -> None:
    await session.highlight(loc, 1500)
    await loc.click(timeout=ACTION_TIMEOUT_MS)
    await loc.fill("")
    await loc.press_sequentially(value, delay=30)


async def _fill_one(session: CrmSession, uimap: UiMap, entity: str, key: str, raw_value: str) -> dict:
    resolved = uimap.resolve_field(entity, key)
    if resolved is None:
        valid = ", ".join(f"{n} ({f.get('label', n)})" for n, f in uimap.fields(entity).items())
        return {"field": key, "ok": False, "error": f"Unknown field '{key}' for {entity}. Valid fields: {valid}"}
    fname, spec = resolved
    label = spec.get("label", fname)
    ftype, widget = spec.get("type", "varchar"), spec.get("widget", "input")
    value = raw_value.strip()
    expected = value
    res: dict[str, Any] = {"field": fname, "label": label}

    if ftype == "enum":
        opt = uimap.match_option(entity, fname, value) if value else ""
        if opt is None:
            opts = ", ".join(uimap.option_labels(entity, fname))
            return {**res, "ok": False, "error": f"'{value}' is not a valid {label}. Options: {opts}."}
        err = await _fill_enum(session, fname, spec, opt, next(
            (o.get("label") or o["value"] for o in uimap.options(entity, fname) if o["value"] == opt), opt))
        expected = opt
    elif widget in ("autocomplete", "autocomplete-multiple"):
        err = await _fill_link(session, fname, spec, value)
    else:
        if ftype in ("date", "datetime", "datetimeOptional") and value:
            converted = _to_date(value, uimap.date_format)
            if converted is None:
                return {**res, "ok": False,
                        "error": f"Could not understand the date '{value}'. Use YYYY-MM-DD."}
            value = expected = converted
        elif ftype in ("currency", "int", "float") and value:
            value = expected = _clean_number(value)
        loc = await _input_locator(session, fname, spec)
        if loc is None:
            return {**res, "ok": False, "error": f"The {label} field is not on screen."}
        if ftype == "bool":
            want = norm(value) in ("yes", "true", "1", "on", "checked")
            await session.highlight(loc)
            await loc.set_checked(want)
            expected = "true" if want else "false"
            err = None
        else:
            await _type(session, loc, value)
            err = None
            if ftype in ("date", "datetime", "datetimeOptional"):
                # Close a date picker that may still be open.
                await session.page.evaluate(
                    "() => document.querySelectorAll('.datepicker-dropdown').forEach(d => d.style.display = 'none')")

    if err:
        return {**res, "ok": False, "error": f"{label}: {err}."}
    actual = await _read_field(session, fname, spec)
    res["value"] = actual
    if actual is None:
        res.update(ok=False, error=f"The {label} field is not on screen.")
    elif widget == "autocomplete-multiple":
        res["ok"] = norm(value) in norm(actual) or not value and not actual
        if not res["ok"]:
            res["error"] = f"{label} shows '{actual}' instead of '{value}'."
    elif widget == "autocomplete":
        res["ok"] = bool(actual) if value else not actual
        if not res["ok"]:
            res["error"] = f"{label} could not be set to '{value}'."
        elif actual and norm(value) not in norm(actual):
            res["note"] = f"picked closest match '{actual}'"
    else:
        res["ok"] = _same(actual, expected) or actual == expected
        if not res["ok"]:
            res["error"] = f"{label} shows '{actual}' instead of '{expected}'."
    return res


async def _missing_required(session: CrmSession, uimap: UiMap, entity: str) -> list[str]:
    """Labels of required fields that are still empty on the open form."""
    missing = []
    for fname in uimap.required_fields(entity):
        spec = uimap.fields(entity).get(fname, {})
        try:
            value = await _read_field(session, fname, spec)
        except PlaywrightError:
            continue
        if value is not None and not str(value).strip():
            missing.append(spec.get("label", fname))
    return missing


def _expand_full_name(uimap: UiMap, entity: str, items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Split "name"/"full name" into firstName + lastName for person entities."""
    fields = uimap.fields(entity)
    if "name" in fields or not ("firstName" in fields and "lastName" in fields):
        return items
    out = []
    for key, value in items:
        if norm(key) in ("name", "full name", "contact name", "lead name", "person"):
            bits = value.strip().split()
            if len(bits) >= 2:
                out += [("firstName", " ".join(bits[:-1])), ("lastName", bits[-1])]
            else:
                out.append(("lastName", value.strip()))
        else:
            out.append((key, value))
    return out


# --------------------------------------------------------------------------- tools

async def tool_get_screen(session: CrmSession, uimap: UiMap) -> dict:
    return await _describe_screen(session, uimap)


async def tool_open_create_form(session: CrmSession, uimap: UiMap, entity: str, fields: Any = None) -> dict:
    opened = await _open_create_form(session, uimap, entity)
    if not opened["ok"] or not _normalize_fields_arg(fields):
        return opened
    filled = await tool_fill_fields(session, uimap, fields)
    # Keep it short (the values matter now) and drop "empty": the form is no longer empty.
    head = opened["observation"].split(" Fields:")[0].replace("(empty, not saved)", "(not saved)")
    return {**opened, **filled, "observation": head + " " + filled["observation"]}


async def _open_create_form(session: CrmSession, uimap: UiMap, entity: str) -> dict:
    ent = uimap.resolve_entity(entity)
    if not ent:
        return {"ok": False, "observation": f"Unknown entity '{entity}'. Supported: {', '.join(uimap.entity_names)}."}
    note = await _leave_form_if_open(session)
    await session.goto(f"#{ent}/create", wait_selector=f"{RECORD} .cell[data-name]")
    await session.page.wait_for_timeout(300)
    screen = await _describe_screen(session, uimap)
    if screen["page"] != "create" or screen["entity"] != ent:
        return {**screen, "ok": False, "observation": "The create form did not open. " + screen["observation"]}
    required = [uimap.fields(ent)[f].get("label", f) for f in uimap.required_fields(ent)]
    fields = ", ".join(f.get("label", n) for n, f in uimap.fields(ent).items())
    obs = f"The new {ent} form is open (empty, not saved). Required: {', '.join(required) or 'none'}. Fields: {fields}."
    if note:
        obs = note + " " + obs
    return {**screen, "observation": obs}


async def tool_fill_fields(session: CrmSession, uimap: UiMap, fields: Any) -> dict:
    items = _normalize_fields_arg(fields)
    if not items:
        return {"ok": False, "observation": "No fields given. Pass fields as [{field, value}]."}
    st = await _state(session)
    if st["kind"] not in ("create", "edit"):
        return {"ok": False, "observation": f"Nothing was filled: no form is open (current page: {st['kind']} "
                                            f"{st['entity']}). To create a record, call open_create_form now with "
                                            "these values in its fields; to change a saved record, open it and "
                                            "call edit_record first."}
    entity = st["entity"]
    results = []
    for key, value in _expand_full_name(uimap, entity, items):
        try:
            results.append(await _fill_one(session, uimap, entity, key, value))
        except (PlaywrightTimeout, PlaywrightError) as exc:
            results.append({"field": key, "ok": False, "error": f"Could not fill {key}: {exc.__class__.__name__}"})
    cells = await _cells(session)
    ok = all(r["ok"] for r in results)
    lines = []
    for r in results:
        if r["ok"]:
            lines.append(f"{r.get('label', r['field'])} = '{r.get('value', '')}'" +
                         (f" ({r['note']})" if r.get("note") else ""))
        else:
            lines.append("FAILED " + r["error"])
    missing = await _missing_required(session, uimap, entity)
    obs = "Form now shows: " + "; ".join(lines) + "."
    if missing:
        obs += " Still required: " + ", ".join(missing) + "."
    obs += " Not saved yet."
    return {"ok": ok, "observation": obs, "fields": results, "form": _form_snapshot(st, cells)}


async def tool_save(session: CrmSession, uimap: UiMap, confirm_duplicate: bool = False) -> dict:
    page = session.page
    st = await _state(session)
    writes: list[tuple[str, int]] = []  # (method, status) of record writes seen during the save

    def _on_response(resp) -> None:
        if resp.request.method in ("POST", "PUT", "PATCH") and f"/api/v1/{st['entity']}" in resp.url:
            writes.append((resp.request.method, resp.status))

    page.on("response", _on_response)
    try:
        return await _save(session, uimap, st, confirm_duplicate, writes)
    finally:
        page.remove_listener("response", _on_response)


async def _save(session: CrmSession, uimap: UiMap, st: dict, confirm_duplicate: bool,
                writes: list[tuple[str, int]]) -> dict:
    page = session.page
    if st.get("modal") and confirm_duplicate and "save" in st["modal"]["buttons"]:
        # The duplicate warning is already open and the user confirmed.
        await _dismiss_modal(session, ("save",))
    else:
        if st["kind"] not in ("create", "edit"):
            return {"ok": False, "observation": f"There is no form to save (current page: {st['kind']} {st['entity']})."}
        btn = await _visible(page, f"{RECORD} button[data-action='save']")
        if btn is None:
            return {"ok": False, "observation": "The Save button is not visible."}
        await _click(session, btn, 200)

    deadline = time.monotonic() + SAVE_TIMEOUT_S
    entity = st["entity"]
    while time.monotonic() < deadline:
        await asyncio.sleep(0.25)
        cur = await _state(session)
        errs = await _errors(session)
        if errs["messages"] or errs["invalidFields"] or any(
                "not valid" in a.lower() or "error" in a.lower() for a in errs["alerts"]):
            cells = await _cells(session)
            return {"ok": False, "saved": False, "errors": errs,
                    "observation": "Not saved. The CRM shows a validation error: " + _error_text(errs) + ".",
                    "form": _form_snapshot(cur, cells)}
        if cur.get("modal"):
            modal = cur["modal"]
            if "save" in modal["buttons"] and "exist" in modal["text"].lower():
                if confirm_duplicate:
                    await _dismiss_modal(session, ("save",))
                    continue
                return {"ok": False, "saved": False, "duplicate": True, "modal": modal,
                        "observation": "Not saved yet. The CRM warns: " + modal["text"] +
                                       ". Ask the user whether to create it anyway (then call save with "
                                       "confirm_duplicate=true) or cancel."}
        failed = [w for w in writes if w[1] >= 400 and w[1] != 409]
        if failed:
            cells = await _cells(session)
            return {"ok": False, "saved": False, "form": _form_snapshot(cur, cells),
                    "observation": f"Not saved: the CRM server rejected the save (HTTP {failed[-1][1]}). "
                                   + (_error_text(errs) or "")}
        if cur["kind"] == "detail" and cur["id"] and any(200 <= w[1] < 300 for w in writes):
            break
    else:
        cur = await _state(session)
        return {"ok": False, "saved": "unknown",
                "observation": f"Save did not finish within {int(SAVE_TIMEOUT_S)} s; the screen shows "
                               f"{cur['kind']} {cur['entity']}. Do NOT click Save again; check with get_screen "
                               "or find_records whether the record was created."}

    await page.wait_for_timeout(400)  # Let the detail view finish rendering.
    cur = await _state(session)
    cells = await _cells(session)
    filled = _fmt_cells(cells, only_filled=True)
    return {"ok": True, "saved": True, "entity": entity, "id": cur["id"], "url": cur["url"],
            "name": cur["title"], "form": _form_snapshot(cur, cells),
            "fields": {c["label"]: c["value"] for c in cells if c["value"]},
            "observation": f"Saved. The {entity} '{cur['title']}' detail view now shows: {filled}."}


async def tool_cancel(session: CrmSession, uimap: UiMap) -> dict:
    st = await _state(session)
    if st["kind"] not in ("create", "edit"):
        return {"ok": False, "observation": f"No form is open to cancel (current page: {st['kind']})."}
    btn = await _visible(session.page, f"{RECORD} button[data-action='cancel'], "
                                       f"{RECORD} button[data-action='cancelEdit']")
    if btn is None:
        return {"ok": False, "observation": "The Cancel button is not visible."}
    await _click(session, btn, 150)
    await session.page.wait_for_timeout(400)
    await _dismiss_modal(session, ("confirm", "yes", "ok"))
    after = await _wait_state(session, lambda s: s["kind"] not in ("create", "edit", "loading"), 8)
    screen = await _describe_screen(session, uimap)
    if after is None:
        return {**screen, "ok": False, "observation": "Cancel did not close the form. " + screen["observation"]}
    return {**screen, "observation": "Cancelled; nothing was saved. " + screen["observation"]}


async def tool_find_records(session: CrmSession, uimap: UiMap, entity: str, query: str) -> dict:
    ent = uimap.resolve_entity(entity)
    if not ent:
        return {"ok": False, "observation": f"Unknown entity '{entity}'. Supported: {', '.join(uimap.entity_names)}."}
    note = await _leave_form_if_open(session)
    page = session.page
    st = await _state(session)
    if not (st["kind"] == "list" and st["entity"] == ent):
        await session.goto(f"#{ent}", wait_selector="#main input[data-name='textFilter']")
    box = page.locator("#main input[data-name='textFilter']").first
    query = (query or "").strip()
    try:
        async with page.expect_response(
                lambda r: (f"/api/v1/{ent}?" in r.url or f"/api/v1/Kanban/{ent}?" in r.url)
                and r.request.method == "GET", timeout=8000):
            await _type_no_blur(session, box, query) if query else await box.fill("")
            await page.keyboard.press("Enter")
    except PlaywrightTimeout:
        pass
    await page.wait_for_timeout(500)
    rows = await page.evaluate(JS_LIST_ROWS, ent)
    via = "list search"
    if not rows and query:
        # The list search matches name prefixes only; fall back to a "contains" lookup.
        rows = await _api_contains(session, ent, query)
        via = "contains lookup (not shown in the list)"
    rows = rows[:10]
    if not rows:
        obs = f"No {ent} records match '{query}'."
    else:
        listing = "; ".join(f"{r['name']} (id {r['id']}" + (f", stage {r['stage']}" if r.get("stage") else "")
                            + ")" for r in rows)
        obs = f"{len(rows)} {ent} match(es) for '{query}' via {via}: {listing}."
    if note:
        obs = note + " " + obs
    return {"ok": True, "observation": obs, "records": rows, "entity": ent}


async def _api_contains(session: CrmSession, entity: str, query: str) -> list[dict]:
    from urllib.parse import quote
    attrs = "name,stage" if entity == "Opportunity" else "name"
    path = (f"{entity}?maxSize=10&select={attrs}&orderBy=modifiedAt&order=desc"
            f"&where[0][type]=contains&where[0][attribute]=name&where[0][value]={quote(query)}")
    try:
        data = await session.api("GET", path)
    except CrmError as exc:
        log.info("contains lookup failed: %s", exc)
        return []
    rows = []
    for item in (data or {}).get("list", []):
        row = {"id": item["id"], "name": item.get("name", "")}
        if item.get("stage"):
            row["stage"] = item["stage"]
        rows.append(row)
    return rows


async def tool_open_record(session: CrmSession, uimap: UiMap, entity: str, record: str) -> dict:
    ent = uimap.resolve_entity(entity)
    if not ent:
        return {"ok": False, "observation": f"Unknown entity '{entity}'. Supported: {', '.join(uimap.entity_names)}."}
    record = (record or "").strip()
    rid = record if ID_RE.match(record) else None
    note = None
    if rid is None:
        found = await tool_find_records(session, uimap, ent, record)
        rows = found.get("records", [])
        exact = [r for r in rows if norm(r["name"]) == norm(record)]
        pick = exact[0] if len(exact) >= 1 else (rows[0] if len(rows) == 1 else None)
        if pick is None:
            if not rows:
                return {"ok": False, "observation": f"No {ent} named '{record}' was found."}
            names = "; ".join(f"{r['name']} (id {r['id']})" for r in rows[:5])
            return {"ok": False, "records": rows,
                    "observation": f"Several {ent} records match '{record}': {names}. Ask which one."}
        rid = pick["id"]
        if len(exact) > 1:
            note = f"{len(exact)} records share this name; opened the first one listed."
        elif not exact:
            note = (f"No record is named exactly '{record}'; opened the only close match "
                    f"'{pick['name']}'. Confirm with the user that this is the right record before changing it.")
        link = await _visible(session.page, f"#main a.link[href='#{ent}/view/{rid}']")
        if link is not None:
            await session.mark_stale()
            await _click(session, link, 250)
            try:
                await session.wait_fresh(f"{RECORD} .cell[data-name]")
            except PlaywrightTimeout:
                await session.goto(f"#{ent}/view/{rid}", wait_selector=f"{RECORD} .cell[data-name]")
        else:
            await session.goto(f"#{ent}/view/{rid}", wait_selector=f"{RECORD} .cell[data-name]")
    else:
        await _leave_form_if_open(session)
        await session.goto(f"#{ent}/view/{rid}", wait_selector=f"{RECORD} .cell[data-name]")
    await session.page.wait_for_timeout(300)
    screen = await _describe_screen(session, uimap)
    if screen["page"] != "detail" or screen.get("id") != rid:
        return {**screen, "ok": False, "observation": "The record did not open. " + screen["observation"]}
    if note:
        screen["observation"] = note + " " + screen["observation"]
    return screen


async def tool_edit_record(session: CrmSession, uimap: UiMap) -> dict:
    st = await _state(session)
    if st["kind"] == "edit":
        return await _describe_screen(session, uimap)
    if st["kind"] != "detail":
        return {"ok": False, "observation": f"No record is open in detail view (current page: {st['kind']})."}
    btn = await _visible(session.page, f"{RECORD} button[data-action='edit']")
    if btn is None:
        return {"ok": False, "observation": "The Edit button is not visible."}
    await _click(session, btn, 200)
    after = await _wait_state(session, lambda s: s["kind"] == "edit", 10)
    if after is not None:
        try:  # Wait until the edit widgets are rendered.
            await session.page.wait_for_selector(
                f"{RECORD} .middle .cell .field input:visible, {RECORD} .middle .cell .field textarea:visible",
                timeout=5000)
        except PlaywrightTimeout:
            pass
    screen = await _describe_screen(session, uimap)
    if after is None:
        return {**screen, "ok": False, "observation": "Edit mode did not open. " + screen["observation"]}
    return screen


async def tool_set_stage(session: CrmSession, uimap: UiMap, stage: str,
                         opportunity: Optional[str] = None) -> dict:
    target = uimap.match_option("Opportunity", "stage", stage)
    if not target:
        opts = ", ".join(uimap.option_labels("Opportunity", "stage"))
        return {"ok": False, "observation": f"'{stage}' is not a valid stage. Stages: {opts}."}
    if opportunity:
        opportunity = opportunity.strip()
        if not ID_RE.match(opportunity):
            # Writes need an exact name (or id): never change a record picked by a fuzzy match.
            found = await tool_find_records(session, uimap, "Opportunity", opportunity)
            rows = found.get("records", [])
            if not any(norm(r["name"]) == norm(opportunity) for r in rows):
                if not rows:
                    return {"ok": False, "observation": f"No Opportunity named '{opportunity}' was found. "
                                                        "Nothing was changed."}
                cands = "; ".join(f"{r['name']} (id {r['id']}"
                                  + (f", stage {r['stage']}" if r.get("stage") else "") + ")" for r in rows[:3])
                return {"ok": False, "records": rows[:3],
                        "observation": f"No Opportunity is named exactly '{opportunity}'. Closest matches: "
                                       f"{cands}. Nothing was changed. Ask the user to confirm which record, "
                                       "then call set_stage with its exact name or id."}
        opened = await tool_open_record(session, uimap, "Opportunity", opportunity)
        if not opened["ok"]:
            return opened
    st = await _state(session)
    if st["entity"] != "Opportunity" or st["kind"] not in ("detail", "edit"):
        return {"ok": False, "observation": "No opportunity is open. Open one first (find_records / open_record)."}
    before = {c["name"]: c["value"] for c in await _cells(session)}.get("stage", "")
    if st["kind"] == "detail":
        if norm(before) == norm(target):
            return {"ok": True, "stage": target, "id": st["id"],
                    "observation": f"'{st['title']}' is already in stage {target}; nothing changed."}
        edit = await tool_edit_record(session, uimap)
        if not edit["ok"]:
            return edit
    filled = await tool_fill_fields(session, uimap, [{"field": "stage", "value": target}])
    if not filled["ok"]:
        return filled
    saved = await tool_save(session, uimap)
    if not saved["ok"]:
        return saved
    now = {c["name"]: c["value"] for c in await _cells(session)}
    shown = now.get("stage", "")
    ok = norm(shown) == norm(target)
    prob = now.get("probability")
    obs = (f"Stage of '{saved.get('name')}' changed from {before or 'unknown'} to {shown} and saved"
           + (f"; probability is now {prob}%" if prob else "") + "." if ok else
           f"Saved, but the detail view shows stage '{shown}' instead of '{target}'.")
    return {**saved, "ok": ok, "stage": shown, "previous_stage": before, "observation": obs}


_DISPATCH = {
    "get_screen": tool_get_screen,
    "open_create_form": tool_open_create_form,
    "fill_fields": tool_fill_fields,
    "save": tool_save,
    "cancel": tool_cancel,
    "find_records": tool_find_records,
    "open_record": tool_open_record,
    "edit_record": tool_edit_record,
    "set_stage": tool_set_stage,
}

_ALLOWED_ARGS = {
    "get_screen": set(), "edit_record": set(), "cancel": set(),
    "open_create_form": {"entity", "fields"}, "fill_fields": {"fields"}, "save": {"confirm_duplicate"},
    "find_records": {"entity", "query"}, "open_record": {"entity", "record"},
    "set_stage": {"stage", "opportunity"},
}


async def run_tool(session: CrmSession, name: str, args: Optional[dict] = None,
                   timeout_s: float = TOOL_TIMEOUT_S) -> dict:
    """Run one tool by name. Never raises; always returns {"ok", "observation", "ms", ...}."""
    started = time.monotonic()
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "observation": f"Unknown tool '{name}'. Available: {', '.join(_DISPATCH)}.", "ms": 0}
    args = dict(args or {})
    kwargs = {k: v for k, v in args.items() if k in _ALLOWED_ARGS[name]}
    if name == "fill_fields" and "fields" not in kwargs:
        kwargs["fields"] = {k: v for k, v in args.items()}  # Tolerate flat {field: value} args.
    uimap = get_ui_map()
    async with session.lock:
        try:
            relogged = await session.ensure_logged_in()
            result = await asyncio.wait_for(fn(session, uimap, **kwargs), timeout=timeout_s)
            if relogged:
                result["observation"] = ("The CRM session had expired and was renewed; any unsaved form was lost. "
                                         + result.get("observation", ""))
        except asyncio.TimeoutError:
            result = {"ok": False, "observation": f"{name} timed out after {int(timeout_s)} s (the demo is slow). "
                                                  "Check the screen with get_screen before retrying."}
        except TypeError as exc:
            result = {"ok": False, "observation": f"Bad arguments for {name}: {exc}."}
        except (CrmError, PlaywrightTimeout, PlaywrightError) as exc:
            log.warning("tool %s failed: %s", name, exc)
            msg = str(exc).splitlines()[0][:200]
            result = {"ok": False, "observation": f"{name} failed: {msg}. Check the screen with get_screen."}
        except Exception as exc:  # noqa: BLE001 - tools must never crash the session
            log.exception("tool %s crashed", name)
            result = {"ok": False, "observation": f"{name} failed unexpectedly: {exc.__class__.__name__}."}
    result.setdefault("ok", False)
    result.setdefault("observation", "")
    result["ms"] = int((time.monotonic() - started) * 1000)
    return result


# --------------------------------------------------------------------------- eval helpers

async def find_ids_by_name(session: CrmSession, entity: str, name: str) -> list[dict]:
    """Exact-name lookup through the REST API (for seeding/cleanup, not for the model)."""
    from urllib.parse import quote
    data = await session.api(
        "GET", f"{entity}?maxSize=50&select=name&where[0][type]=equals&where[0][attribute]=name"
               f"&where[0][value]={quote(name)}")
    return [{"id": r["id"], "name": r.get("name", "")} for r in (data or {}).get("list", [])]


async def delete_record(session: CrmSession, entity: str, record_id: str) -> bool:
    try:
        await session.api("DELETE", f"{entity}/{record_id}")
        return True
    except CrmError as exc:
        log.info("delete %s/%s failed: %s", entity, record_id, exc)
        return False


async def delete_by_name(session: CrmSession, entity: str, name: str) -> int:
    count = 0
    for rec in await find_ids_by_name(session, entity, name):
        count += await delete_record(session, entity, rec["id"])
    return count


async def create_seed_opportunity(session: CrmSession, rid: str, stage: str = "Prospecting",
                                  amount: float = 1000) -> dict:
    """Create "Stage Test {rid}" (any account, amount 1000, closes next month) via the API."""
    accounts = await session.api("GET", "Account?maxSize=1&select=name&orderBy=createdAt&order=asc")
    account = (accounts or {}).get("list", [{}])[0]
    close = (date.today() + timedelta(days=30)).isoformat()
    body = {"name": f"Stage Test {rid}", "stage": stage, "amount": amount, "amountCurrency": "USD",
            "closeDate": close, "probability": 10}
    if account.get("id"):
        body["accountId"] = account["id"]
    created = await session.api("POST", "Opportunity", body)
    return {"id": created["id"], "name": created["name"], "stage": created.get("stage")}
