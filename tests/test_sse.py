"""Formato de eventos SSE."""
from app.sse import SSE_KEEPALIVE, sse_event


def test_sse_event_format():
    assert sse_event("rates", [{"price": 1.5}]) == 'event: rates\ndata: [{"price": 1.5}]\n\n'


def test_sse_keepalive_is_comment():
    assert SSE_KEEPALIVE.startswith(":") and SSE_KEEPALIVE.endswith("\n\n")
