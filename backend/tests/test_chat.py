"""Chat REST routes."""

from __future__ import annotations

from app.llm.scripted import ScriptedTurn


def _send(client, event_id: str, text: str, **extra) -> dict:
    response = client.post(f"/api/v1/events/{event_id}/chat/messages", json={"text": text, **extra})
    assert response.status_code == 202, response.text
    return response.json()


def _wait(client, run_id: str) -> None:
    ctx = client.app.state.ctx
    run = client.portal.call(ctx.runtime.wait, run_id, 5)
    assert run.status == "succeeded", run.error


def test_post_message_starts_turn_and_get_lists_oldest_first(client, client_event, llm):
    llm.on_run("orchestrator", [ScriptedTurn(text="Hi there.")])

    body = _send(client, client_event.id, "  hello  ", context={"selected_card_ids": []})

    assert body["message"]["role"] == "user" and body["message"]["content"] == "hello"
    assert body["message"]["event_id"] == client_event.id and body["message"]["status"] == "complete"
    assert body["run"]["kind"] == "orchestrator" and body["run"]["input"]["message_id"] == body["message"]["id"]
    _wait(client, body["run"]["id"])

    listing = client.get(f"/api/v1/events/{client_event.id}/chat/messages")
    assert listing.status_code == 200
    messages = listing.json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "Hi there." and messages[1]["status"] == "complete"
    assert "llm_messages" not in messages[1]

    llm.on_run("orchestrator", [ScriptedTurn(text="Still here.")])
    _wait(client, _send(client, client_event.id, "second")["run"]["id"])
    all_four = client.get(f"/api/v1/events/{client_event.id}/chat/messages").json()
    assert [m["content"] for m in all_four] == ["hello", "Hi there.", "second", "Still here."]

    newest = client.get(f"/api/v1/events/{client_event.id}/chat/messages", params={"limit": 1}).json()
    assert [m["content"] for m in newest] == ["Still here."]
    older = client.get(
        f"/api/v1/events/{client_event.id}/chat/messages", params={"limit": 2, "before": all_four[2]["id"]}
    ).json()
    assert [m["content"] for m in older] == ["hello", "Hi there."]


def test_post_message_rejects_blank_text_and_unknown_event(client, client_event):
    assert client.post(f"/api/v1/events/{client_event.id}/chat/messages", json={"text": "   "}).status_code == 422
    assert client.post("/api/v1/events/evt_nope/chat/messages", json={"text": "hi"}).status_code == 404
    assert client.get("/api/v1/events/evt_nope/chat/messages").status_code == 404
    missing_cursor = client.get(f"/api/v1/events/{client_event.id}/chat/messages", params={"before": "msg_nope"})
    assert missing_cursor.status_code == 404


def test_post_message_returns_503_without_llm(client, client_event, llm):
    llm.available = False
    response = client.post(f"/api/v1/events/{client_event.id}/chat/messages", json={"text": "hi"})
    assert response.status_code == 503 and "ANTHROPIC_API_KEY" in response.json()["detail"]
    assert client.get(f"/api/v1/events/{client_event.id}/chat/messages").json() == []
