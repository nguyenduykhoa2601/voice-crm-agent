"""Pure-logic tests: metrics, prompts, model ranking, Live config."""

from __future__ import annotations

import pytest
from google import genai
from google.genai import _common, _live_converters, types

from app import prompts
from app.live import (
    _parse_duration_s,
    build_live_config,
    rank_live_models,
    uses_realtime_text,
)
from app.metrics import MetricsStore, percentile, summarize


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([5], 90) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile(range(1, 101), 90) == pytest.approx(90.1)


def test_metrics_store_and_summary(tmp_path):
    store = MetricsStore(tmp_path / "sub" / "m.jsonl")
    store.record("session_start", session_id="a", model="m")
    for ms in (400, 600, 800, 1000):
        store.record("turn", session_id="a", first_audio_ms=ms, turn_ms=ms * 3, tool_ms=0)
    store.record("tool", session_id="a", name="save", ms=900, ok=True)
    store.record("tool", session_id="a", name="save", ms=1100, ok=False)
    store.record("outcome", session_id="a", completed=True, saves=1, corrections=2,
                 interventions=2, turns=4)
    store.record("outcome", session_id="b", completed=False, saves=0, corrections=0,
                 interventions=1, turns=2)
    store.record("outcome", session_id="c", completed=False, saves=0, turns=0)  # never used
    s = store.summary()
    assert s["sessions"] == 1 and s["turns"] == 4
    assert s["first_audio_ms"]["p50"] == 700
    assert s["first_audio_ms"]["p95"] is not None
    assert s["tools"]["save"]["calls"] == 2 and s["tools"]["save"]["ok_rate"] == 50
    assert s["task_completion"] == {"attempted": 2, "completed": 1, "rate": 50.0}
    assert s["interventions_per_session"] == 1.5


def test_summary_empty():
    s = summarize([])
    assert s["task_completion"]["rate"] is None and s["first_audio_ms"]["p50"] is None


def test_prompt_contains_policy_and_fallback_fields():
    text = prompts.build_system_instruction({})
    for phrase in ("Shall I", "Never say", "fill_fields", "Closed Won", "lastName"):
        assert phrase in text


def test_describe_ui_map_accepts_crawled_shape():
    ui_map = {
        "entities": {
            "Contact": {
                "fields": {
                    "lastName": {"label": "Last Name", "type": "varchar", "required": True},
                    "assignedUser": {"label": "Assigned User", "type": "link"},
                    "salutationName": {"label": "Salutation", "type": "enum",
                                       "options": [{"value": "", "label": ""},
                                                   {"value": "Mr.", "label": "Mr."}]},
                }
            },
            "Opportunity": {"fields": {"name": {"label": "Name", "required": True}}},
        }
    }
    text = prompts.describe_ui_map(ui_map)
    assert "Last Name (lastName)*" in text
    assert "Salutation (salutationName) [Mr.]" in text
    assert "assignedUser" not in text
    # Required fallback fields fill gaps in the crawled map (stage options).
    assert "Stage (stage)* [Prospecting" in text


def test_describe_ui_map_list_shape():
    ui_map = {"entities": [{"name": "Lead", "fields": [{"name": "lastName", "required": True}]}]}
    assert "- Lead: Last Name (lastName)*" in prompts.describe_ui_map(ui_map)


class _M:
    def __init__(self, name, actions=("bidiGenerateContent",)):
        self.name = "models/" + name
        self.supported_actions = list(actions)


def test_rank_live_models_prefers_newest_native_audio():
    models = [
        _M("gemini-2.5-flash", ("generateContent",)),
        _M("gemini-2.0-flash-exp"),
        _M("gemini-2.5-flash-native-audio-preview-09-2025"),
        _M("gemini-2.5-flash-native-audio-preview-12-2025"),
        _M("gemini-3.1-flash-live-preview"),
        _M("gemini-3.8-live-extended-thinking"),
        _M("gemini-3.5-live-translate-preview"),
        _M("gemini-robotics-er-2-streaming-preview"),
    ]
    assert rank_live_models(models) == [
        "gemini-3.1-flash-live-preview",
        "gemini-2.5-flash-native-audio-preview-12-2025",
        "gemini-2.5-flash-native-audio-preview-09-2025",
        "gemini-3.8-live-extended-thinking",
        "gemini-2.0-flash-exp",
    ]


def test_text_mode_selection():
    assert uses_realtime_text("gemini-3.1-flash-live-preview")
    assert not uses_realtime_text("gemini-2.5-flash-native-audio-latest")
    assert uses_realtime_text("gemini-2.5-flash-native-audio-latest", "realtime")
    assert not uses_realtime_text("gemini-3.1-flash-live-preview", "client_content")


def test_parse_duration():
    assert _parse_duration_s("12s") == 12.0
    assert _parse_duration_s("1.5s") == 1.5
    assert _parse_duration_s(None) is None
    assert _parse_duration_s("bad") is None


def test_live_config_converts_for_gemini_api():
    """The config must survive the SDK's Gemini-API (mldev) converter."""
    cfg = build_live_config(
        system_instruction="hi",
        tool_declarations=[{"name": "save", "description": "Save.",
                            "parameters": {"type": "OBJECT", "properties": {}}}],
        resumption_handle="abc",
        non_blocking_tools=True,
    )
    client = genai.Client(api_key="x", http_options=types.HttpOptions(api_version="v1beta"))
    # Same conversion path as AsyncLive.connect() for API-key (non-Vertex) clients.
    request = _common.convert_to_dict(
        _live_converters._LiveConnectParameters_to_mldev(
            api_client=client._api_client,
            from_object=types.LiveConnectParameters(
                model="models/gemini-3.1-flash-live-preview", config=cfg
            ).model_dump(exclude_none=True),
        )
    )
    setup = request["setup"]
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["sessionResumption"] == {"handle": "abc"}
    assert "inputAudioTranscription" in setup and "outputAudioTranscription" in setup
    # Nested keys stay snake_case (accepted by the proto JSON parser on the server).
    aad = setup["realtimeInputConfig"]["automatic_activity_detection"]
    assert aad["start_of_speech_sensitivity"] == "START_SENSITIVITY_HIGH"
    assert setup["realtimeInputConfig"]["activity_handling"] == "START_OF_ACTIVITY_INTERRUPTS"
    assert setup["contextWindowCompression"]["sliding_window"] == {}
    fd = setup["tools"][0]["functionDeclarations"][0]
    assert fd["name"] == "save" and fd["behavior"] == "NON_BLOCKING"


def test_friendly_live_error_only_flags_real_quota_errors():
    from app.session import _friendly_live_error

    limit = "The voice service is at its free-tier limit. Please retry in a minute."
    assert _friendly_live_error(RuntimeError("429 RESOURCE_EXHAUSTED")) == limit
    assert _friendly_live_error(RuntimeError("1011 You exceeded your current quota")) == limit
    assert _friendly_live_error(RuntimeError("rate limit reached")) == limit
    wrong_model = "models/x is not supported for bidiGenerateContent"
    assert _friendly_live_error(RuntimeError(wrong_model)) != limit
    assert _friendly_live_error(RuntimeError("operation was separated")) != limit


def test_fill_items_accepts_all_arg_shapes():
    from app.session import fill_items

    expected = [("lastName", "Smith")]
    assert fill_items({"fields": [{"field": "lastName", "value": "Smith"}]}) == expected
    assert fill_items({"fields": {"lastName": "Smith"}}) == expected
    assert fill_items({"lastName": "Smith"}) == expected
