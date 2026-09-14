"""Tool-call extraction across Responses and Chat Completions projections.

A Muse/Meta response may carry the model's chosen calls on the Responses
``output`` array, on a Chat Completions ``choices[].message.tool_calls``
projection, or on both at once. Every shape must surface each call exactly
once: a dropped call makes Evie narrate an action she never ran, and a
duplicated call runs the action twice.
"""

from __future__ import annotations

from app.gateway.muse_spark import MuseSparkProvider


def _provider() -> MuseSparkProvider:
    return MuseSparkProvider(base_url="https://api.meta.ai/v1", api_key="test-key")


def _chat_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_output_only_returns_each_function_call_once() -> None:
    result = _provider()._result_from_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_out_1",
                    "name": "search_mail",
                    "arguments": '{"query": "invoice"}',
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "on it"}],
                },
            ]
        }
    )
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_out_1", "search_mail", {"query": "invoice"})
    ]
    assert result.text == "on it"


def test_choices_only_still_returns_calls_and_text() -> None:
    result = _provider()._result_from_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "checking now",
                        "tool_calls": [
                            _chat_call("call_chat_1", "search_mail", '{"query": "invoice"}')
                        ],
                    }
                }
            ]
        }
    )
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_chat_1", "search_mail", {"query": "invoice"})
    ]
    assert result.text == "checking now"


def test_both_shapes_reporting_the_same_call_execute_it_once() -> None:
    shared = _chat_call("call_same", "search_mail", '{"query": "invoice"}')
    result = _provider()._result_from_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_same",
                    "name": "search_mail",
                    "arguments": {"query": "invoice"},
                }
            ],
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [shared]}}],
        }
    )
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_same", "search_mail", {"query": "invoice"})
    ]


def test_calls_present_in_only_one_shape_are_merged_not_preferred() -> None:
    result = _provider()._result_from_response(
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_from_output",
                    "name": "search_mail",
                    "arguments": {"query": "invoice"},
                }
            ],
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done!",
                        "tool_calls": [
                            _chat_call("call_from_choices", "read_mail", '{"id": "42"}')
                        ],
                    }
                }
            ],
        }
    )
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("call_from_output", "search_mail", {"query": "invoice"}),
        ("call_from_choices", "read_mail", {"id": "42"}),
    ]
    # A projected prose message must not replace the Responses text channel.
    assert result.text == ""
