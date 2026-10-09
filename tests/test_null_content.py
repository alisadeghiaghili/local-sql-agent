# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""A response with ``content: null`` is an empty answer, not the text ``"None"``.

A reasoning model cut off while it is still thinking comes back with
``finish_reason: "length"`` and ``"content": null`` (its thinking is in a
separate field). The transport used to render that as ``str(None)``, the
four characters ``None``. That looked like an answer, so
``is_truncated_empty_completion`` never fired and the ``LLM_OUTPUT_TRUNCATED``
message (which names ``LLM_NUM_PREDICT`` and ``LLM_EXTRA_BODY``) was replaced
by a parse failure about SQL that was never written.

Every test runs the non-streaming and the streaming path
(``LLM_STREAM_TIMINGS``), and compares ``null`` and a missing ``content``
with the empty string, which has always taken the truncation path.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from api.errors import InvalidSQLResponseError, TruncatedSQLResponseError
from config import override_settings
from llm.providers import MockBackend, OpenAIBackend, _answer_text
from llm.router import PromptSegments
from llm.sql_agent import SQLAgent
from observability.llm_status import (
    TRUNCATED_OUTPUT_ERROR_CODE,
    finish_reason_from_meta,
    is_truncated_empty_completion,
)

_USAGE = {"prompt_tokens": 4612, "completion_tokens": 512}
_THINKING = "Let me think about which tables hold the monthly totals"

#: how the answer text is (not) present in the cut-off message
CONTENT_VARIANTS = {
    "empty_string": {"content": ""},
    "null": {"content": None},
    "missing": {},
}


def _body(message_fields: dict[str, Any], finish: str = "length") -> dict[str, Any]:
    return {
        "id": "c1", "object": "chat.completion", "model": "m",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", **message_fields, "reasoning_content": _THINKING},
            "finish_reason": finish,
        }],
        "usage": _USAGE,
    }


def _stream_lines(message_fields: dict[str, Any], finish: str = "length") -> list[bytes]:
    """The streamed form of :func:`_body`: the thinking in pieces, then the finish."""
    def line(obj: dict[str, Any]) -> bytes:
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def chunk(delta: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        return {"id": "c1", "model": "m", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}

    role: dict[str, Any] = {"role": "assistant"}
    if "content" in message_fields:
        role["content"] = message_fields["content"]
    half = len(_THINKING) // 2
    return [
        line(chunk(role)),
        line(chunk({"reasoning_content": _THINKING[:half]})),
        line(chunk({"reasoning_content": _THINKING[half:]}, finish)),
        line({"id": "c1", "model": "m", "choices": [], "usage": _USAGE}),
        b"data: [DONE]",
    ]


class _StreamResponse:
    status_code = 200

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, **_kwargs: Any):
        yield from self._lines

    def close(self) -> None:
        return None


def _post(streaming: bool, variant: str, finish: str = "length"):
    """A ``requests.post`` stand-in returning the cut-off response."""
    fields = CONTENT_VARIANTS[variant]
    if streaming:
        return patch(
            "llm.providers.requests.post",
            side_effect=lambda *a, **k: _StreamResponse(_stream_lines(fields, finish)),
        )
    resp = MagicMock(status_code=200)
    resp.json.return_value = _body(fields, finish)
    return patch("llm.providers.requests.post", return_value=resp)


def _backend() -> OpenAIBackend:
    return OpenAIBackend(model="m", api_key="", base_url="http://localhost:8000/v1", retries=1)


PATHS = pytest.mark.parametrize("streaming", [False, True], ids=["non-streaming", "streaming"])
VARIANTS = pytest.mark.parametrize("variant", list(CONTENT_VARIANTS))


class TestTheTransport:
    @PATHS
    @VARIANTS
    def test_a_cut_off_reasoning_response_is_empty_text_not_the_word_none(self, streaming, variant):
        with override_settings(llm_stream_timings=streaming), _post(streaming, variant):
            text, meta = _backend().generate_with_meta("prompt")
        assert text == ""
        assert meta["finish_reason"] == "length"
        assert meta["reasoning_detected"] is True
        assert is_truncated_empty_completion(text, finish_reason_from_meta(meta))

    @PATHS
    @VARIANTS
    def test_the_reasoning_estimate_counts_the_whole_completion(self, streaming, variant):
        # nothing but reasoning was generated: every completion token is
        # reasoning, with no "None" counted as an answer.
        with override_settings(llm_stream_timings=streaming), _post(streaming, variant):
            _text, meta = _backend().generate_with_meta("prompt")
        assert meta["reasoning_tokens"] == _USAGE["completion_tokens"]
        assert meta["reasoning_tokens_estimated"] is True

    @VARIANTS
    def test_both_paths_agree_byte_for_byte(self, variant):
        results = []
        for streaming in (False, True):
            with override_settings(llm_stream_timings=streaming), _post(streaming, variant):
                results.append(_backend().generate_with_meta("prompt"))
        (plain_text, plain), (stream_text, streamed) = results
        assert stream_text == plain_text == ""
        for key in ("finish_reason", "reasoning_detected", "reasoning_tokens",
                    "reasoning_tokens_estimated", "endpoint_status", "attempts"):
            assert streamed[key] == plain[key], key

    @PATHS
    def test_a_normal_answer_is_untouched(self, streaming):
        answer = "SELECT TOP 5 N'تالار' FROM t"
        if streaming:
            lines = [
                b'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}).encode(),
                b'data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": f"  {answer}  "}, "finish_reason": "stop"}]},
                                       ensure_ascii=False).encode("utf-8"),
                b"data: [DONE]",
            ]
            patcher = patch("llm.providers.requests.post", return_value=_StreamResponse(lines))
        else:
            resp = MagicMock(status_code=200)
            resp.json.return_value = _body({"content": f"  {answer}  "}, finish="stop")
            patcher = patch("llm.providers.requests.post", return_value=resp)
        with override_settings(llm_stream_timings=streaming), patcher:
            text, meta = _backend().generate_with_meta("prompt")
        assert text == answer and meta["finish_reason"] == "stop"

    def test_the_helper(self):
        assert _answer_text({"content": "x"}) == "x"
        assert _answer_text({"content": None}) == ""
        assert _answer_text({}) == ""
        assert _answer_text(None) == ""
        assert _answer_text({"content": ["not", "text"]}) == ""

    def test_the_constrained_request_treats_null_as_empty_not_a_type_error(self):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "choices": [{"message": {"content": None}, "finish_reason": "length"}],
        }
        with patch("llm.providers.requests.post", return_value=resp):
            with pytest.raises(ValueError):  # json.JSONDecodeError, not TypeError
                _backend().generate_structured(PromptSegments(question="q"), {"type": "object"})


@pytest.fixture()
def audit_file(tmp_path):
    with patch("observability.audit._AUDIT_LOG_FILE", str(tmp_path / "audit_log.jsonl")):
        yield


@pytest.fixture(autouse=True)
def _clear_query_cache():
    from api.query_cache import query_cache

    query_cache.clear()
    yield
    query_cache.clear()


def _run(streaming: bool, variant: str, mode: str):
    """``run_query`` against a real ``OpenAIBackend`` whose HTTP call is faked.

    Returns ``(exception, number_of_model_calls)``.
    """
    from api.runner import run_query

    agent = SQLAgent(backend=_backend(), execute_fn=lambda sql: None, max_corrections=2)
    with override_settings(llm_stream_timings=streaming), _post(streaming, variant) as post, patch(
        "api.runner.agent", agent,
    ):
        with pytest.raises(Exception) as excinfo:
            run_query("سوال", system_prompt="sp", mode=mode, request_id="null-content")
    return excinfo.value, post.call_count


class TestThroughTheApi:
    """What an analyst is told: the same thing for ``null`` as for ``""``."""

    @PATHS
    @VARIANTS
    def test_sql_mode_names_the_token_limit_and_does_not_retry(self, streaming, variant, audit_file):
        exc, calls = _run(streaming, variant, "sql")
        assert isinstance(exc, TruncatedSQLResponseError)
        assert exc.error_code == TRUNCATED_OUTPUT_ERROR_CODE == "LLM_OUTPUT_TRUNCATED"
        assert "LLM_NUM_PREDICT" in str(exc)
        assert calls == 1

    @PATHS
    @VARIANTS
    def test_the_audit_record_says_length(self, streaming, variant, tmp_path):
        path = tmp_path / "audit_log.jsonl"
        with patch("observability.audit._AUDIT_LOG_FILE", str(path)):
            _run(streaming, variant, "sql")
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert record["error_code"] == TRUNCATED_OUTPUT_ERROR_CODE
        assert record["llm"]["finish_reason"] == "length"
        assert record["llm"]["reasoning_tokens"] == _USAGE["completion_tokens"]

    @PATHS
    @VARIANTS
    def test_full_mode_does_whatever_an_empty_string_does(self, streaming, variant, audit_file):
        # (Full mode goes through SQLAgent's correction loop, which has no
        # truncation shortcut; it is the same for "" as for null.)
        exc, calls = _run(streaming, variant, "full")
        reference_exc, reference_calls = _run(streaming, "empty_string", "full")
        assert type(exc) is type(reference_exc) is InvalidSQLResponseError
        assert calls == reference_calls

    def test_mock_backend_still_returns_its_text(self):
        # the mock never had a content field; nothing to fix, nothing broken
        assert MockBackend(response="SELECT 1").generate("p") == "SELECT 1"
