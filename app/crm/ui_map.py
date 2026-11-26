"""Load and query the crawled UI map (data/ui_map.json).

The map lists, per entity, the fields that are actually rendered on the create
form: their on-screen label, type, widget, required flag and enum options. This
module resolves the loose names a user or model may use ("mobile", "surname",
"company", "deal") to the real field names.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

UI_MAP_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "ui_map.json"

ENTITY_SYNONYMS: dict[str, list[str]] = {
    "Contact": ["contact", "contacts", "person", "people", "customer contact"],
    "Opportunity": ["opportunity", "opportunities", "deal", "deals", "opp", "opps", "pipeline"],
    "Account": ["account", "accounts", "company", "companies", "organization", "organisation", "business"],
    "Lead": ["lead", "leads", "prospect", "prospects"],
}

# Synonym → candidate field names (first one present on the entity wins).
FIELD_SYNONYMS: dict[str, list[str]] = {
    "phone": ["phoneNumber"], "mobile": ["phoneNumber"], "mobile number": ["phoneNumber"],
    "cell": ["phoneNumber"], "cell phone": ["phoneNumber"], "telephone": ["phoneNumber"],
    "phone number": ["phoneNumber"], "tel": ["phoneNumber"], "number": ["phoneNumber"],
    "email": ["emailAddress"], "e-mail": ["emailAddress"], "mail": ["emailAddress"],
    "email address": ["emailAddress"],
    "first name": ["firstName"], "first": ["firstName"], "given name": ["firstName"],
    "forename": ["firstName"],
    "last name": ["lastName"], "last": ["lastName"], "surname": ["lastName"],
    "family name": ["lastName"],
    "salutation": ["salutationName"], "prefix": ["salutationName"],
    "company": ["account", "accounts", "accountName"], "account": ["account", "accounts", "accountName"],
    "account name": ["accountName", "account", "accounts"], "organization": ["account", "accounts", "accountName"],
    "organisation": ["account", "accounts", "accountName"], "company name": ["accountName", "account", "accounts", "name"],
    "street": ["addressStreet", "billingAddressStreet"], "address": ["addressStreet", "billingAddressStreet"],
    "street address": ["addressStreet", "billingAddressStreet"],
    "city": ["addressCity", "billingAddressCity"], "town": ["addressCity", "billingAddressCity"],
    "state": ["addressState", "billingAddressState"], "province": ["addressState", "billingAddressState"],
    "region": ["addressState", "billingAddressState"],
    "zip": ["addressPostalCode", "billingAddressPostalCode"], "zip code": ["addressPostalCode", "billingAddressPostalCode"],
    "postcode": ["addressPostalCode", "billingAddressPostalCode"],
    "postal code": ["addressPostalCode", "billingAddressPostalCode"],
    "country": ["addressCountry", "billingAddressCountry"],
    "birthday": ["birthday"], "date of birth": ["birthday"], "dob": ["birthday"], "birth date": ["birthday"],
    "description": ["description"], "notes": ["description"], "note": ["description"],
    "comment": ["description"], "comments": ["description"], "details": ["description"],
    "deal name": ["name"], "opportunity name": ["name"], "title": ["title", "name"],
    "job title": ["title"], "position": ["title"], "role": ["title"],
    "amount": ["amount", "opportunityAmount"], "value": ["amount", "opportunityAmount"],
    "deal value": ["amount"], "deal size": ["amount"], "price": ["amount"], "revenue": ["amount", "opportunityAmount"],
    "close date": ["closeDate"], "closing date": ["closeDate"], "expected close": ["closeDate"],
    "expected close date": ["closeDate"], "due date": ["closeDate"],
    "stage": ["stage"], "pipeline stage": ["stage"], "sales stage": ["stage"], "phase": ["stage"],
    "status": ["status", "stage"],
    "source": ["source", "leadSource"], "lead source": ["leadSource", "source"],
    "probability": ["probability"], "chance": ["probability"], "likelihood": ["probability"],
    "owner": ["assignedUser"], "assigned to": ["assignedUser"], "assignee": ["assignedUser"],
    "assigned user": ["assignedUser"], "user": ["assignedUser"],
    "team": ["teams"], "teams": ["teams"],
    "contact": ["contacts"], "contacts": ["contacts"],
    "website": ["website"], "web site": ["website"], "url": ["website"], "site": ["website"],
    "industry": ["industry"], "type": ["type"], "campaign": ["campaign"],
}

# Used only when data/ui_map.json is missing.
STATIC_FALLBACK: dict[str, Any] = {
    "source": "static-fallback",
    "dateFormat": "MM/DD/YYYY",
    "entities": {
        "Contact": {
            "label": "Contact", "labelPlural": "Contacts",
            "routes": {"list": "#Contact", "create": "#Contact/create", "detail": "#Contact/view/{id}"},
            "requiredFields": ["lastName"],
            "fields": {
                "salutationName": {"label": "Salutation", "type": "enum", "widget": "selectize", "cell": "name",
                                   "options": [{"value": v, "label": v} for v in ["", "Mr.", "Ms.", "Mrs.", "Dr."]]},
                "firstName": {"label": "First Name", "type": "varchar", "widget": "input", "cell": "name"},
                "lastName": {"label": "Last Name", "type": "varchar", "widget": "input", "cell": "name",
                             "required": True},
                "accounts": {"label": "Accounts", "type": "linkMultiple", "widget": "autocomplete-multiple",
                             "cell": "accounts", "entity": "Account"},
                "emailAddress": {"label": "Email", "type": "email", "widget": "input", "cell": "emailAddress"},
                "phoneNumber": {"label": "Phone", "type": "phone", "widget": "input", "cell": "phoneNumber"},
                "description": {"label": "Description", "type": "text", "widget": "textarea", "cell": "description"},
            },
        },
        "Opportunity": {
            "label": "Opportunity", "labelPlural": "Opportunities",
            "routes": {"list": "#Opportunity", "create": "#Opportunity/create",
                       "detail": "#Opportunity/view/{id}"},
            "requiredFields": ["name", "amount", "closeDate"],
            "fields": {
                "name": {"label": "Name", "type": "varchar", "widget": "input", "cell": "name", "required": True},
                "account": {"label": "Account", "type": "link", "widget": "autocomplete", "cell": "account",
                            "entity": "Account"},
                "stage": {"label": "Stage", "type": "enum", "widget": "selectize", "cell": "stage",
                          "options": [{"value": v, "label": v} for v in [
                              "Prospecting", "Qualification", "Proposal", "Negotiation",
                              "Closed Won", "Closed Lost"]]},
                "amount": {"label": "Amount", "type": "currency", "widget": "input", "cell": "amount",
                           "required": True},
                "closeDate": {"label": "Close Date", "type": "date", "widget": "datepicker", "cell": "closeDate",
                              "required": True},
                "description": {"label": "Description", "type": "text", "widget": "textarea", "cell": "description"},
            },
        },
    },
}


def norm(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace; also splits camelCase."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(text))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


class UiMap:
    def __init__(self, data: dict):
        self.data = data
        self.entities: dict[str, dict] = data.get("entities", {})
        self.date_format: str = data.get("dateFormat") or "MM/DD/YYYY"

    # ------------------------------------------------------------------ entities
    @property
    def entity_names(self) -> list[str]:
        return list(self.entities)

    def resolve_entity(self, name: str) -> Optional[str]:
        if not name:
            return None
        if name in self.entities:
            return name
        n = norm(name)
        for ent, spec in self.entities.items():
            if n in (norm(ent), norm(spec.get("label", "")), norm(spec.get("labelPlural", ""))):
                return ent
        for ent, syns in ENTITY_SYNONYMS.items():
            if ent in self.entities and n in syns:
                return ent
        return None

    def entity(self, name: str) -> Optional[dict]:
        ent = self.resolve_entity(name)
        return self.entities.get(ent) if ent else None

    # ------------------------------------------------------------------ fields
    def fields(self, entity: str) -> dict[str, dict]:
        spec = self.entity(entity)
        return spec.get("fields", {}) if spec else {}

    def resolve_field(self, entity: str, key: str) -> Optional[tuple[str, dict]]:
        """Map a field name, on-screen label or synonym to (fieldName, spec)."""
        fields = self.fields(entity)
        if not fields or not key:
            return None
        if key in fields:
            return key, fields[key]
        n = norm(key)
        for fname, spec in fields.items():
            if n == norm(fname) or n == norm(spec.get("label", "")):
                return fname, spec
        for cand in FIELD_SYNONYMS.get(n, []):
            if cand in fields:
                return cand, fields[cand]
        # Loose match: "phone num" → "Phone", "last" → "Last Name".
        n_compact = n.replace(" ", "")
        for fname, spec in fields.items():
            label = norm(spec.get("label", "")).replace(" ", "")
            if n_compact and (label.startswith(n_compact) or n_compact.startswith(label) and len(label) > 3):
                return fname, spec
        return None

    def options(self, entity: str, field: str) -> list[dict]:
        spec = self.fields(entity).get(field, {})
        return spec.get("options") or []

    def match_option(self, entity: str, field: str, value: str) -> Optional[str]:
        """Return the exact option value matching `value` (by value or label), else None."""
        opts = self.options(entity, field)
        if not opts:
            return None
        n = norm(value)
        for o in opts:
            if value == o["value"] or n == norm(o["value"]) or n == norm(o.get("label", "")):
                return o["value"]
        # Unambiguous prefix ("closed won" vs "won", "nego" → "Negotiation").
        hits = [o["value"] for o in opts if o["value"] and (
            norm(o["value"]).startswith(n) or norm(o.get("label", "")).startswith(n)
            or (len(n) > 2 and n in norm(o["value"]).split()))]
        return hits[0] if len(hits) == 1 else None

    def option_labels(self, entity: str, field: str) -> list[str]:
        return [o.get("label") or o["value"] for o in self.options(entity, field) if o["value"]]

    def required_fields(self, entity: str) -> list[str]:
        spec = self.entity(entity)
        return list(spec.get("requiredFields", [])) if spec else []

    # ------------------------------------------------------------------ prompt help
    def describe(self, entities: Optional[list[str]] = None, max_options: int = 12) -> str:
        """Compact, model-friendly description of entities and fields."""
        lines = []
        for ent in entities or self.entity_names:
            spec = self.entity(ent)
            if not spec:
                continue
            lines.append(f"{ent} ({spec.get('labelPlural', ent)}):")
            for fname, f in spec.get("fields", {}).items():
                bits = [f"{fname} \"{f.get('label', fname)}\" {f.get('type', '')}"]
                if f.get("required"):
                    bits.append("REQUIRED")
                opts = [o.get("label") or o["value"] for o in f.get("options") or [] if o["value"]]
                if opts:
                    more = f" (+{len(opts) - max_options} more)" if len(opts) > max_options else ""
                    bits.append("options: " + ", ".join(opts[:max_options]) + more)
                lines.append("  - " + "; ".join(bits))
        return "\n".join(lines)


def load_ui_map(path: Optional[Path] = None) -> UiMap:
    p = Path(path) if path else UI_MAP_PATH
    try:
        data = json.loads(p.read_text())
        if not data.get("entities"):
            raise ValueError("no entities")
        return UiMap(data)
    except (OSError, ValueError) as exc:
        log.warning("ui_map not usable (%s); using static fallback", exc)
        return UiMap(STATIC_FALLBACK)


@lru_cache(maxsize=1)
def get_ui_map() -> UiMap:
    """Process-wide cached UI map."""
    return load_ui_map()
