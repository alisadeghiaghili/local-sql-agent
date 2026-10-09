# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""``LLM_STREAM_TIMINGS`` -- splitting the llm stage without changing what the model returns.

Two promises are tested here, and the second is the one that matters:

* with the setting on, the audit ``llm`` block carries ``ttft_ms`` and
  ``generation_ms`` (and a reasoning-token count, with or without
  streaming);
* the streamed request **reassembles the identical response** -- same
  content, reasoning text, ``finish_reason``, ``usage`` -- as the
  non-streaming one, proven on recorded chunk sequences next to the
  non-streaming body the same model call produces.

The recordings are written out in full, in the shapes vLLM (both its
``reasoning_content`` and newer ``reasoning`` field names) and a
non-reasoning server emit; no network is used (``requests.post`` is patched).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

import config as cfg
from config import Settings, override_settings
from llm import providers
from llm.providers import (
    OpenAIBackend,
    StreamError,
    _iter_sse_events,
    _reasoning_token_stats,
    _StreamAssembler,
)
from observability.llm_status import build_llm_status, latency_fields_from_meta

# ---------------------------------------------------------------------------
# Recorded responses: (stream chunks, the non-streaming body of the same call)
# ---------------------------------------------------------------------------

_ID = "chatcmpl-8f2c"
_MODEL = "reasoning-model"
_FP = "fp_44709d6fcb"


def _chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": _ID, "object": "chat.completion.chunk", "created": 1760000000, "model": _MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra,
    }


def _usage_chunk(usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _ID, "object": "chat.completion.chunk", "created": 1760000000, "model": _MODEL,
        "choices": [], "usage": usage,
    }


def _body(message: dict[str, Any], finish: str | None, usage: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": _ID, "object": "chat.completion", "created": 1760000000, "model": _MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
        "usage": usage, **extra,
    }


_USAGE = {"prompt_tokens": 4612, "completion_tokens": 40, "total_tokens": 4652}

# 1. A reasoning model: reasoning first (vLLM's `reasoning_content`), then
#    the answer -- Persian included, split across chunks.
REASONING_STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"reasoning_content": "We need the "}),
    _chunk({"reasoning_content": "total per month. "}),
    _chunk({"reasoning_content": "Use SUM."}),
    _chunk({"content": "SELECT TOP 5 N'تا"}),
    _chunk({"content": "لار' AS r, "}),
    _chunk({"content": "SUM(v) FROM t"}, "stop"),
    _usage_chunk(_USAGE),
]
REASONING_BODY = _body(
    {"content": "SELECT TOP 5 N'تالار' AS r, SUM(v) FROM t",
     "reasoning_content": "We need the total per month. Use SUM."},
    "stop", _USAGE,
)

# 2. Newer vLLM spells the field `reasoning`; and the server fingerprints.
REASONING_FIELD_STREAM = [
    _chunk({"role": "assistant", "content": ""}, system_fingerprint=_FP),
    _chunk({"reasoning": "Think. "}),
    _chunk({"content": "SELECT 1"}, "stop"),
    _usage_chunk(_USAGE),
]
REASONING_FIELD_BODY = _body(
    {"content": "SELECT 1", "reasoning": "Think. "}, "stop", _USAGE, system_fingerprint=_FP,
)

# 3. A non-reasoning model.
PLAIN_STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"content": "SELECT "}),
    _chunk({"content": "COUNT(*) FROM t"}),
    _chunk({}, "stop"),
    _usage_chunk(_USAGE),
]
PLAIN_BODY = _body({"content": "SELECT COUNT(*) FROM t"}, "stop", _USAGE)

# 4. Cut off while still reasoning: no content at all, finish_reason length.
#    A non-streaming server returns `content: null` here.
TRUNCATED_STREAM = [
    _chunk({"role": "assistant", "content": None}),
    _chunk({"reasoning_content": "Let me think about "}),
    _chunk({"reasoning_content": "the tables"}, "length"),
    _usage_chunk({"prompt_tokens": 4612, "completion_tokens": 512, "total_tokens": 5124}),
]
TRUNCATED_BODY = _body(
    {"content": None, "reasoning_content": "Let me think about the tables"},
    "length", {"prompt_tokens": 4612, "completion_tokens": 512, "total_tokens": 5124},
)

# 5. A server that reports the reasoning-token count itself (OpenAI's field).
REPORTED_USAGE = {
    "prompt_tokens": 100, "completion_tokens": 90,
    "completion_tokens_details": {"reasoning_tokens": 80},
}
REPORTED_STREAM = [
    _chunk({"role": "assistant", "content": ""}),
    _chunk({"reasoning_content": "hmm"}),
    _chunk({"content": "SELECT 1"}, "stop"),
    _usage_chunk(REPORTED_USAGE),
]
REPORTED_BODY = _body({"content": "SELECT 1", "reasoning_content": "hmm"}, "stop", REPORTED_USAGE)

RECORDINGS = {
    "reasoning_content": (REASONING_STREAM, REASONING_BODY),
    "reasoning_field": (REASONING_FIELD_STREAM, REASONING_FIELD_BODY),
    "plain": (PLAIN_STREAM, PLAIN_BODY),
    "truncated_reasoning": (TRUNCATED_STREAM, TRUNCATED_BODY),
    "server_reported_reasoning_tokens": (REPORTED_STREAM, REPORTED_BODY),
}


def _sse(chunks: list[dict[str, Any]], *, done: bool = True) -> list[bytes]:
    """The wire form: ``data:`` lines, blank separators, keep-alives, ``[DONE]``."""
    lines: list[bytes] = [b": keep-alive", b""]
    for chunk in chunks:
        lines.append(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8"))
        lines.append(b"")
    if done:
        lines.append(b"data: [DONE]")
        lines.append(b"")
    return lines


class FakeStreamResponse:
    """What ``requests.post(..., stream=True)`` returns."""

    def __init__(self, lines: list[bytes], status: int = 200) -> None:
        self.status_code = status
        self._lines = lines
        self.closed = False
        self.iter_kwargs: dict[str, Any] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def iter_lines(self, **kwargs: Any):
        self.iter_kwargs = kwargs
        yield from self._lines

    def close(self) -> None:
        self.closed = True


def _json_response(body: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = body
    return resp


def _backend() -> OpenAIBackend:
    return OpenAIBackend(model="m", api_key="k", base_url="http://localhost:8000/v1", retries=2)


def _non_streaming(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    with override_settings(llm_stream_timings=False), patch(
        "llm.providers.requests.post", return_value=_json_response(body),
    ):
        return _backend().generate_with_meta("prompt")


def _streaming(chunks: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    with override_settings(llm_stream_timings=True), patch(
        "llm.providers.requests.post", return_value=FakeStreamResponse(_sse(chunks)),
    ):
        return _backend().generate_with_meta("prompt")


# ---------------------------------------------------------------------------
# Equivalence: the heart of "streaming changes no result"
# ---------------------------------------------------------------------------

class TestStreamReassemblesTheIdenticalResponse:
    @pytest.mark.parametrize("name", RECORDINGS)
    def test_reassembled_body_equals_the_non_streaming_body(self, name):
        chunks, body = RECORDINGS[name]
        assembler = _StreamAssembler()
        for i, chunk in enumerate(chunks):
            assembler.feed(chunk, float(i))
        assert assembler.body() == body

    @pytest.mark.parametrize("name", RECORDINGS)
    def test_the_decoded_wire_form_gives_the_same_body_as_the_chunk_objects(self, name):
        chunks, body = RECORDINGS[name]
        assembler = _StreamAssembler()
        for event in _iter_sse_events(_sse(chunks)):
            if event is not None:
                assembler.feed(event, 0.0)
        assert assembler.body() == body

    @pytest.mark.parametrize("name", RECORDINGS)
    def test_generate_with_meta_returns_the_same_text_and_the_same_facts(self, name):
        chunks, body = RECORDINGS[name]
        text_plain, meta_plain = _non_streaming(body)
        text_stream, meta_stream = _streaming(chunks)

        assert text_stream == text_plain
        assert meta_stream["raw"] == meta_plain["raw"]
        for key in (
            "endpoint_status", "attempts", "finish_reason", "reasoning_detected",
            "reasoning_tokens", "reasoning_tokens_estimated",
        ):
            assert meta_stream[key] == meta_plain[key], key

    @pytest.mark.parametrize("name", RECORDINGS)
    def test_the_audit_block_is_the_same_apart_from_the_timings(self, name):
        chunks, body = RECORDINGS[name]
        blocks = []
        for text_meta in (_non_streaming(body), _streaming(chunks)):
            _text, meta = text_meta
            blocks.append(build_llm_status(
                meta["raw"], model="m", endpoint_status=meta["endpoint_status"],
                attempts=meta["attempts"], finish_reason=meta["finish_reason"],
                reasoning_detected=meta["reasoning_detected"], total_ms=0,
                **latency_fields_from_meta({**meta, "ttft_ms": None, "generation_ms": None}),
            ))
        assert blocks[0] == blocks[1]

    def test_persian_survives_the_stream(self):
        text, _meta = _streaming(REASONING_STREAM)
        assert text == "SELECT TOP 5 N'تالار' AS r, SUM(v) FROM t"

    def test_all_reasoning_response_gets_the_same_none_content_as_non_streaming(self):
        # Both paths feed the same `str(message.get("content", ""))`, so a
        # response that is all reasoning reads identically either way.
        text_plain, _ = _non_streaming(TRUNCATED_BODY)
        text_stream, meta = _streaming(TRUNCATED_STREAM)
        assert text_stream == text_plain
        assert meta["finish_reason"] == "length"
        assert meta["reasoning_detected"] is True

    def test_the_request_asks_for_usage_and_streams(self):
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=FakeStreamResponse(_sse(PLAIN_STREAM)),
        ) as post:
            _backend().generate_with_meta("prompt")
        payload = post.call_args.kwargs["json"]
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        assert post.call_args.kwargs["stream"] is True
        # everything else a real request carries is unchanged
        with override_settings(llm_stream_timings=False):
            plain = _backend()._build_payload("prompt")
        assert {k: v for k, v in payload.items() if k not in ("stream", "stream_options")} == plain

    def test_the_default_request_is_unchanged(self):
        with override_settings(llm_stream_timings=False), patch(
            "llm.providers.requests.post", return_value=_json_response(PLAIN_BODY),
        ) as post:
            _backend().generate_with_meta("prompt")
        assert "stream" not in post.call_args.kwargs["json"]
        assert "stream_options" not in post.call_args.kwargs["json"]
        assert "stream" not in post.call_args.kwargs

    def test_the_response_is_closed(self):
        fake = FakeStreamResponse(_sse(PLAIN_STREAM))
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=fake,
        ):
            _backend().generate_with_meta("prompt")
        assert fake.closed
        assert fake.iter_kwargs == {"chunk_size": 64}

    def test_a_server_that_ignores_stream_options_degrades_to_no_usage(self):
        no_usage = PLAIN_STREAM[:-1]
        _text, meta = _streaming(no_usage)
        assert "usage" not in meta["raw"]
        assert meta["finish_reason"] == "stop"

    def test_out_of_scope_still_carries_the_meta(self):
        chunks = [
            _chunk({"role": "assistant", "content": ""}),
            _chunk({"content": "OUT_OF_"}),
            _chunk({"content": "SCOPE"}, "stop"),
            _usage_chunk(_USAGE),
        ]
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=FakeStreamResponse(_sse(chunks)),
        ):
            with pytest.raises(ValueError, match="OUT_OF_SCOPE") as excinfo:
                _backend().generate_with_meta("prompt")
        assert excinfo.value.llm_meta["finish_reason"] == "stop"
        assert excinfo.value.llm_meta["ttft_ms"] is not None


# ---------------------------------------------------------------------------
# The timings
# ---------------------------------------------------------------------------

class _Clock:
    """A ``perf_counter`` that moves 100 ms on every read."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.1
        return value


class TestTimings:
    def test_first_token_is_the_first_non_empty_delta_not_the_role_chunk(self):
        assembler = _StreamAssembler()
        times = [1.0, 2.0, 3.0]
        for chunk, t in zip(REASONING_STREAM[:3], times):
            assembler.feed(chunk, t)
        assert assembler.first_chunk_at == 1.0
        assert assembler.first_token_at == 2.0, "the reasoning token, not the empty role chunk"

    def test_an_answer_only_response_times_its_first_content_token(self):
        assembler = _StreamAssembler()
        for chunk, t in zip(PLAIN_STREAM, [1.0, 2.0, 3.0, 4.0, 5.0]):
            assembler.feed(chunk, t)
        assert assembler.first_token_at == 2.0

    def test_ttft_and_generation_add_up_to_total(self):
        clock = _Clock()
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=FakeStreamResponse(_sse(PLAIN_STREAM)),
        ), patch("llm.providers.time.perf_counter", clock):
            _text, meta = _backend().generate_with_meta("prompt")
        assert meta["ttft_ms"] is not None and meta["generation_ms"] is not None
        assert meta["ttft_ms"] + meta["generation_ms"] == meta["total_ms"]
        assert 0 < meta["ttft_ms"] < meta["total_ms"]

    def test_non_streaming_cannot_split_the_wait(self):
        _text, meta = _non_streaming(PLAIN_BODY)
        assert meta["ttft_ms"] is None and meta["generation_ms"] is None
        assert meta["total_ms"] >= 0

    def test_a_response_with_no_token_at_all_falls_back_to_the_first_chunk(self):
        chunks = [_chunk({"role": "assistant", "content": ""}), _chunk({}, "stop"), _usage_chunk(_USAGE)]
        _text, meta = _streaming(chunks)
        assert meta["ttft_ms"] is not None

    def test_setting_default_off_and_env_on(self, monkeypatch):
        monkeypatch.delenv("LLM_STREAM_TIMINGS", raising=False)
        assert Settings().llm_stream_timings is False
        monkeypatch.setenv("LLM_STREAM_TIMINGS", "true")
        assert Settings().llm_stream_timings is True

    def test_the_fields_reach_the_audit_block(self):
        _text, meta = _streaming(REASONING_STREAM)
        block = build_llm_status(
            meta["raw"], model="m", finish_reason=meta["finish_reason"],
            total_ms=meta["total_ms"], **latency_fields_from_meta(meta),
        )
        assert block["ttft_ms"] == meta["ttft_ms"]
        assert block["generation_ms"] == meta["generation_ms"]
        assert block["reasoning_tokens"] == meta["reasoning_tokens"]
        assert block["reasoning_tokens_estimated"] is True

    def test_a_backend_that_reports_nothing_yields_the_unknown_block(self):
        block = build_llm_status(None, model="m", finish_reason="stop", **latency_fields_from_meta({}))
        assert block["ttft_ms"] is None and block["generation_ms"] is None
        assert block["reasoning_tokens"] is None
        assert block["reasoning_tokens_estimated"] is False

    def test_runner_audit_block_carries_the_fields(self):
        from api import runner
        from llm.providers import MockBackend

        meta = {"ttft_ms": 900, "generation_ms": 2100, "reasoning_tokens": 80,
                "reasoning_tokens_estimated": True, "total_ms": 3000}
        block = runner._llm_status_block(meta, MockBackend(), finish_reason="stop")
        assert (block["ttft_ms"], block["generation_ms"]) == (900, 2100)
        assert (block["reasoning_tokens"], block["reasoning_tokens_estimated"]) == (80, True)
        # and without them, exactly as before the fields existed
        bare = runner._llm_status_block({}, MockBackend(), finish_reason="stop")
        assert bare["ttft_ms"] is None and bare["reasoning_tokens"] is None


# ---------------------------------------------------------------------------
# Reasoning tokens: reported, estimated, absent
# ---------------------------------------------------------------------------

class TestReasoningTokens:
    def test_server_reported_count_wins_and_is_not_an_estimate(self):
        for text_meta in (_non_streaming(REPORTED_BODY), _streaming(REPORTED_STREAM)):
            _t, meta = text_meta
            assert meta["reasoning_tokens"] == 80
            assert meta["reasoning_tokens_estimated"] is False

    def test_a_reported_zero_is_zero_not_unknown(self):
        body = {"usage": {"completion_tokens": 9, "completion_tokens_details": {"reasoning_tokens": 0}}}
        assert _reasoning_token_stats(body, "", "SELECT 1") == (0, False)

    def test_estimate_splits_the_exact_total_by_character_share(self):
        # 40 completion tokens; reasoning is 36 of 36 + 43 characters.
        _t, meta = _streaming(REASONING_STREAM)
        assert meta["reasoning_tokens_estimated"] is True
        share = len("We need the total per month. Use SUM.") / (
            len("We need the total per month. Use SUM.") + len("SELECT TOP 5 N'تالار' AS r, SUM(v) FROM t")
        )
        assert meta["reasoning_tokens"] == round(40 * share)

    def test_estimate_without_any_usage_is_chars_over_four(self):
        assert _reasoning_token_stats({}, "x" * 80, "SELECT 1") == (20, True)

    def test_a_truncated_all_reasoning_response_counts_the_whole_completion(self):
        _t, meta = _non_streaming(TRUNCATED_BODY)
        assert meta["reasoning_tokens"] == 512
        assert meta["reasoning_tokens_estimated"] is True

    def test_no_reasoning_is_none_not_zero(self):
        _t, meta = _streaming(PLAIN_STREAM)
        assert meta["reasoning_tokens"] is None
        assert meta["reasoning_tokens_estimated"] is False

    def test_the_reasoning_text_never_reaches_the_audit_block(self):
        _t, meta = _streaming(REASONING_STREAM)
        block = build_llm_status(meta["raw"], model="m", finish_reason="stop", **latency_fields_from_meta(meta))
        assert "total per month" not in json.dumps(block, ensure_ascii=False)

    def test_booleans_are_not_token_counts(self):
        body = {"usage": {"completion_tokens_details": {"reasoning_tokens": True}}}
        assert _reasoning_token_stats(body, "", "")[0] is None


# ---------------------------------------------------------------------------
# Failure modes of the stream
# ---------------------------------------------------------------------------

class TestStreamFailures:
    def _run(self, response: FakeStreamResponse, retries: int = 2):
        backend = OpenAIBackend(
            model="m", api_key="k", base_url="http://localhost:8000/v1", retries=retries,
        )
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=response,
        ) as post, patch("llm.providers.time.sleep"):
            try:
                return backend.generate_with_meta("prompt"), post
            except Exception as exc:  # noqa: BLE001
                return exc, post

    def test_an_error_object_mid_stream_is_retried_like_any_transport_failure(self):
        lines = _sse([_chunk({"role": "assistant", "content": ""}), {"error": {"type": "overloaded"}}])
        exc, post = self._run(FakeStreamResponse(lines))
        assert isinstance(exc, RuntimeError)
        assert post.call_count == 2

    def test_a_stream_that_just_stops_is_an_error_not_a_short_answer(self):
        lines = _sse(PLAIN_STREAM[:2], done=False)
        exc, _post = self._run(FakeStreamResponse(lines))
        assert isinstance(exc, RuntimeError)

    def test_a_stream_ending_in_done_without_finish_reason_is_accepted(self):
        lines = _sse(PLAIN_STREAM[:2], done=True)
        result, _post = self._run(FakeStreamResponse(lines))
        assert result[0] == "SELECT"

    def test_malformed_json_is_a_transport_failure(self):
        lines = [b'data: {"choices": [', b"data: [DONE]"]
        exc, post = self._run(FakeStreamResponse(lines))
        assert isinstance(exc, RuntimeError) and post.call_count == 2

    def test_http_error_status_is_retried_as_before(self):
        exc, post = self._run(FakeStreamResponse([], status=503))
        assert isinstance(exc, RuntimeError) and post.call_count == 2

    def test_stream_error_is_a_request_exception(self):
        assert issubclass(StreamError, requests.RequestException)

    def test_a_whole_call_deadline_is_a_timeout_that_is_not_retried(self):
        backend = OpenAIBackend(
            model="m", api_key="k", base_url="http://localhost:8000/v1", retries=3, timeout=1,
        )
        clock = _Clock()  # 100 ms per read: past 1 s after ten reads
        many = _sse([_chunk({"content": "x"})] * 30)
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=FakeStreamResponse(many),
        ) as post, patch("llm.providers.time.perf_counter", clock):
            with pytest.raises(requests.Timeout):
                backend.generate_with_meta("prompt")
        assert post.call_count == 1

    def test_the_structured_request_is_not_streamed(self):
        body = {"choices": [{"message": {"content": "{\"a\": 1}"}, "finish_reason": "stop"}]}
        with override_settings(llm_stream_timings=True), patch(
            "llm.providers.requests.post", return_value=_json_response(body),
        ) as post:
            parsed, _meta = _backend().generate_structured(
                providers_segments(), {"type": "object"},
            )
        assert parsed == {"a": 1}
        assert "stream" not in post.call_args.kwargs["json"]


def providers_segments():
    from llm.router import PromptSegments

    return PromptSegments(question="q")


# ---------------------------------------------------------------------------
# The assembler on its own
# ---------------------------------------------------------------------------

class TestAssembler:
    def test_sse_comments_blanks_and_events_are_ignored(self):
        lines = [b": ping", b"", b"event: message", b'data: {"a": 1}', b"data:", b"data: [DONE]", b'data: {"b": 2}']
        assert list(_iter_sse_events(lines)) == [{"a": 1}, None]

    def test_str_lines_work_too(self):
        assert list(_iter_sse_events(['data: {"a": 1}'])) == [{"a": 1}]

    def test_only_choice_zero_is_read(self):
        assembler = _StreamAssembler()
        assembler.feed({"choices": [
            {"index": 1, "delta": {"content": "second"}},
            {"index": 0, "delta": {"content": "first"}, "finish_reason": "stop"},
        ]}, 0.0)
        assert assembler.body()["choices"][0]["message"]["content"] == "first"

    def test_a_non_object_chunk_is_refused(self):
        with pytest.raises(StreamError):
            _StreamAssembler().feed([1, 2], 0.0)  # type: ignore[arg-type]

    def test_usage_may_arrive_on_a_chunk_that_also_has_choices(self):
        assembler = _StreamAssembler()
        assembler.feed(_chunk({"content": "x"}, "stop", usage={"prompt_tokens": 3}), 0.0)
        assert assembler.body()["usage"] == {"prompt_tokens": 3}

    def test_module_exports_what_the_docs_name(self):
        assert providers.StreamError is StreamError
        assert cfg.settings.llm_stream_timings in (True, False)


# ---------------------------------------------------------------------------
# Over a real socket: both HTTP framings a streaming server can use
# ---------------------------------------------------------------------------

class _StandInServer:
    """A loopback OpenAI-compatible server: the non-streaming body, or the same
    response as server-sent events -- chunk-encoded (what vLLM and llama.cpp
    send) or close-delimited (the framing that makes a read of N bytes wait
    for N bytes).

    The first token is held back ``first_token_delay`` seconds, so a real
    ``ttft_ms`` is measurable without sleeping in the test body."""

    def __init__(self, *, chunked: bool, first_token_delay: float = 0.15) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self
        self.requests: list[dict[str, Any]] = []
        self.chunked = chunked
        self.delay = first_token_delay

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                import time as _time

                size = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(size))
                outer.requests.append(payload)
                _time.sleep(outer.delay)
                if not payload.get("stream"):
                    data = json.dumps(REASONING_BODY, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if outer.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Connection", "close")
                self.end_headers()
                for line in _sse(REASONING_STREAM):
                    piece = line + b"\n"
                    if outer.chunked:
                        piece = f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
                    self.wfile.write(piece)
                    self.wfile.flush()
                if outer.chunked:
                    self.wfile.write(b"0\r\n\r\n")
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


@pytest.fixture(params=[True, False], ids=["chunk-encoded", "close-delimited"])
def stand_in(request):
    server = _StandInServer(chunked=request.param)
    yield server
    server.close()


class TestOverARealSocket:
    def test_streamed_and_plain_responses_are_the_same_response(self, stand_in):
        backend = OpenAIBackend(model="m", api_key="", base_url=stand_in.url)
        with override_settings(llm_stream_timings=False):
            plain_text, plain = backend.generate_with_meta("prompt")
        with override_settings(llm_stream_timings=True):
            streamed_text, streamed = backend.generate_with_meta("prompt")

        assert streamed_text == plain_text == "SELECT TOP 5 N'تالار' AS r, SUM(v) FROM t"
        assert streamed["raw"] == plain["raw"]
        for key in ("finish_reason", "reasoning_detected", "reasoning_tokens",
                    "reasoning_tokens_estimated", "endpoint_status"):
            assert streamed[key] == plain[key], key
        assert [bool(r.get("stream")) for r in stand_in.requests] == [False, True]

    def test_the_first_token_time_includes_the_servers_wait_and_is_not_held_back(self, stand_in):
        backend = OpenAIBackend(model="m", api_key="", base_url=stand_in.url)
        with override_settings(llm_stream_timings=True):
            _text, meta = backend.generate_with_meta("prompt")
        assert meta["ttft_ms"] >= round(stand_in.delay * 1000) - 5
        assert meta["ttft_ms"] + meta["generation_ms"] == meta["total_ms"]

    def test_the_warm_up_request_over_the_wire(self, stand_in):
        backend = OpenAIBackend(model="m", api_key="", base_url=stand_in.url)
        info = backend.warm_prefix("PREFIX ONLY", timeout=5)
        sent = stand_in.requests[-1]
        assert sent["messages"] == [{"role": "user", "content": "PREFIX ONLY"}]
        assert sent["max_tokens"] == 1 and sent["temperature"] == 0 and "stream" not in sent
        assert info["prompt_tokens"] == 4612
