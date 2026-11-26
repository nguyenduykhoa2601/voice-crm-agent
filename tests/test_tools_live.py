"""End-to-end tool workflows against the public EspoCRM demo.

These tests drive a real headless Chromium through the CRM UI, exactly as the
voice agent does. They need network access and take about a minute.

    uv run pytest -m live tests/test_tools_live.py -v

Set SKIP_LIVE=1 to skip them. Records are named with a unique run id so runs do
not collide on the shared demo, and are deleted afterwards.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import pytest_asyncio

from app.crm.browser import CrmBrowser
from app.crm.tools import (
    TOOL_DECLARATIONS,
    create_seed_opportunity,
    delete_by_name,
    delete_record,
    run_tool,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv("SKIP_LIVE") == "1", reason="SKIP_LIVE=1"),
]

live_async = pytest.mark.asyncio(loop_scope="module")

RID = f"{int(time.time()) % 1_000_000}{uuid.uuid4().hex[:3]}"


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def crm():
    browser = CrmBrowser(headless=True)
    frames: list[int] = []

    async def on_frame(jpeg: bytes) -> None:
        frames.append(len(jpeg))

    try:
        session = await browser.new_session(on_frame=on_frame)
        session.test_frames = frames  # type: ignore[attr-defined]
        yield session
    finally:
        await browser.close()


async def call(session, name, **args):
    result = await run_tool(session, name, args)
    print(f"[{name}] ok={result['ok']} {result['ms']}ms :: {result['observation'][:300]}")
    return result


def test_declarations_are_valid_for_genai():
    from google.genai import types

    tool = types.Tool(function_declarations=[types.FunctionDeclaration(**d) for d in TOOL_DECLARATIONS])
    names = {d.name for d in tool.function_declarations}
    assert names == {"get_screen", "open_create_form", "fill_fields", "save", "cancel",
                     "find_records", "open_record", "edit_record", "set_stage"}


@live_async
async def test_create_contact_with_correction(crm):
    first, wrong_last, last = "Ada", f"Lovlace{RID}", f"Lovelace{RID}"
    email = f"ada.{RID}@example.com"
    try:
        r = await call(crm, "open_create_form", entity="Contact")
        assert r["ok"] and r["page"] == "create"

        r = await call(crm, "fill_fields", fields=[
            {"field": "first name", "value": first},
            {"field": "lastName", "value": wrong_last},
            {"field": "email", "value": email},
            {"field": "phone", "value": "+1 555 010 0199"},
        ])
        assert r["ok"], r["observation"]
        values = {f["field"]: f["value"] for f in r["fields"]}
        assert values["lastName"] == wrong_last
        assert values["emailAddress"] == email

        # The user corrects the last name before saving.
        r = await call(crm, "fill_fields", fields=[{"field": "Last Name", "value": last}])
        assert r["ok"] and r["fields"][0]["value"] == last

        r = await call(crm, "save")
        assert r["ok"] and r["saved"] is True, r["observation"]
        assert r["id"] and f"#Contact/view/{r['id']}" in r["url"]
        assert r["fields"]["Name"] == f"{first} {last}"
        assert r["fields"]["Email"] == email
        assert wrong_last not in r["observation"]

        screen = await call(crm, "get_screen")
        assert screen["page"] == "detail" and screen["entity"] == "Contact"
        assert f"{first} {last}" in screen["observation"]
    finally:
        await delete_by_name(crm, "Contact", f"{first} {last}")
        await delete_by_name(crm, "Contact", f"{first} {wrong_last}")


@live_async
async def test_move_opportunity_stage(crm):
    seed = await create_seed_opportunity(crm, RID)
    try:
        r = await call(crm, "find_records", entity="Opportunity", query=seed["name"])
        assert r["ok"] and any(rec["id"] == seed["id"] for rec in r["records"]), r["observation"]

        r = await call(crm, "open_record", entity="Opportunity", record=seed["name"])
        assert r["ok"] and r["page"] == "detail" and r["id"] == seed["id"]
        assert r["form"]["fields"]["Stage"] == "Prospecting"

        bad = await call(crm, "set_stage", stage="done")
        assert not bad["ok"] and "Closed Won" in bad["observation"]

        r = await call(crm, "set_stage", stage="negotiation")
        assert r["ok"], r["observation"]
        assert r["stage"] == "Negotiation" and r["previous_stage"] == "Prospecting"

        # Re-open the record from scratch: the change must be persisted on the server.
        r = await call(crm, "open_record", entity="Opportunity", record=seed["id"])
        assert r["ok"] and r["form"]["fields"]["Stage"] == "Negotiation"
    finally:
        await delete_record(crm, "Opportunity", seed["id"])


@live_async
async def test_save_with_missing_required_field(crm):
    r = await call(crm, "open_create_form", entity="Contact")
    assert r["ok"]
    r = await call(crm, "fill_fields", fields={"firstName": f"NoLast{RID}"})
    assert r["ok"]
    assert "Last Name" in r["observation"]  # Still required

    r = await call(crm, "save")
    assert r["ok"] is False and r["saved"] is False
    assert "Last Name is required" in r["observation"]
    assert "Name" in r["errors"]["invalidFields"]

    screen = await call(crm, "get_screen")
    assert screen["page"] == "create"  # Still on the unsaved form

    r = await call(crm, "cancel")
    assert r["ok"] and r["page"] == "list"


@live_async
async def test_invalid_inputs_are_reported(crm):
    r = await call(crm, "open_create_form", entity="Opportunity")
    assert r["ok"]
    r = await call(crm, "fill_fields", fields=[
        {"field": "stage", "value": "Won it"},
        {"field": "favourite colour", "value": "blue"},
        {"field": "amount", "value": "$2,500"},
    ])
    assert r["ok"] is False
    by_field = {f["field"]: f for f in r["fields"]}
    assert "Options:" in by_field["stage"]["error"] and "Closed Won" in by_field["stage"]["error"]
    assert "Valid fields" in by_field["favourite colour"]["error"]
    assert by_field["amount"]["ok"]
    r = await call(crm, "cancel")
    assert r["ok"]
    assert crm.test_frames, "screencast delivered no frames"
