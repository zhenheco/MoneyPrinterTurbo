import ast
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import loomloom


ROOT_DIR = Path(__file__).parents[2]
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


def _function(tree, name):
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _widget_by_key(elements, key):
    return next(item for item in elements if str(getattr(item, "key", "")) == key)


def test_loomloom_webui_path_requires_quote_and_explicit_confirmation():
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    function = _function(tree, "_render_generation_controls")
    execute_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "LoomLoomConfirmedVideoRequest"
    ]

    assert len(execute_calls) == 1
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
        and node.func.attr == "checkbox"
        for node in ast.walk(_function(tree, "_render_loomloom_video_settings"))
    )


def test_loomloom_video_source_quotes_then_submits_secret_only_in_process_request():
    test_config = dict(
        config.app,
        llm_provider="openai",
        video_source="pexels",
        loomloom_base_url="https://example.test/loom/v1",
        loomloom_api_token="",
    )
    quote_result = loomloom.LoomLoomQuote(
        quote_id="video-quote-1",
        listing_version_id="video-version-1",
        currency="CNY",
        task_count=1,
        estimated_buyer_payable_t=1230000,
        estimated_buyer_payable_amount="0.123",
        input_rows=(),
    )

    with (
        patch.object(config, "app", test_config),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(
            loomloom.LoomLoomVideoBackend,
            "quote",
            side_effect=lambda batch: replace(
                quote_result,
                input_rows=batch.input_rows,
                task_count=len(batch.input_rows),
            ),
        ) as quote_call,
        patch("app.services.webui_task.submit_generation") as submit_generation,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=30)
        app.session_state["ui_language"] = "en"
        app.run()

        _widget_by_key(app.text_area, "video_subject").set_value("AI office").run()
        _widget_by_key(app.text_area, "video_script").set_value(
            "AI helps people work faster."
        ).run()
        _widget_by_key(app.text_area, "video_terms").set_value(
            "office worker, AI assistant"
        ).run()
        _widget_by_key(app.selectbox, "video_source_select_en").select(
            "loomloom"
        ).run()
        _widget_by_key(app.text_input, "loomloom_user_api_token").set_value(
            "session-user-token"
        ).run()
        _widget_by_key(app.number_input, "loomloom_budget_limit_t").set_value(
            2_000_000
        ).run()
        _widget_by_key(app.button, "loomloom_quote_videos").click().run()

        assert quote_call.call_count == 1
        _widget_by_key(app.checkbox, "loomloom_video_confirm_charge").check().run()
        _widget_by_key(app.button, "generate_video_button").click().run()

        assert submit_generation.call_count == 1
        submitted_params = submit_generation.call_args.kwargs["params"]
        video_request = submit_generation.call_args.kwargs["loomloom_video_request"]
        assert "session-user-token" not in submitted_params.model_dump_json()
        assert test_config["loomloom_api_token"] == ""
        assert video_request.settings.api_token == "session-user-token"
        assert "session-user-token" not in repr(video_request)
        assert video_request.listing_version_id == "video-version-1"
        assert video_request.client_request_id.startswith("mpt-video-")
        assert app.session_state["loomloom_video_quote"] is None
        assert [str(item.value) for item in app.exception] == []
