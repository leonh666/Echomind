"""Authenticated HTTP boundaries and automatic answer persistence; no provider calls."""
from api import main, operations
from test_operations_api import service
from test_chat_api import configure


def test_chat_persists_server_owned_snapshot_and_customer_feedback(service, monkeypatch):
    client, _, _, _, _, headers = service
    configure(monkeypatch)
    own = headers("customer")
    answer = client.post("/chat", headers=own, json={"message": "退款", "user_id": "forged"}).json()
    assert answer["feedback_available"] is True
    request_id = answer["request_id"]
    snapshot = client.get(f"/answers/{request_id}", headers=own).json()
    assert snapshot["question"] == "退款"
    assert snapshot["response"] == answer["response"]
    assert client.get(f"/answers/{request_id}", headers=headers("other")).status_code == 404
    saved = client.put(f"/answers/{request_id}/feedback", headers=own, json={"decision": "accepted"})
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    forged = client.put(f"/answers/{request_id}/feedback", headers=own, json={"decision": "accepted", "sources": []})
    assert forged.status_code == 422
    assert client.get("/answers", headers=own).json()["total"] == 1


def test_stream_snapshot_and_admin_report_are_durable(service, monkeypatch):
    import json
    client, store, _, _, _, headers = service
    configure(monkeypatch)
    response = client.post("/chat/stream", headers=headers("customer"), json={"message": "退款"})
    done = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")][-1]
    assert done["response"]["feedback_available"] is True
    assert client.post("/diagnostics/reports", headers=headers("customer"), json={}).status_code == 403
    report = client.post("/diagnostics/reports", headers=headers("owner"), json={"days": 7})
    assert report.status_code == 201
    body = report.json()
    assert body["counts"]["answers"] == 1 and body["counts"]["unreviewed"] == 1
    assert body["metrics"]["resolution_rate"]["value"] is None
    assert client.get("/diagnostics/reports", headers=headers("owner")).json()["total"] == 1
    assert client.get(f"/diagnostics/reports/{body['id']}", headers=headers("owner")).json() == body
    assert client.get(f"/diagnostics/reports/{body['id']}", headers=headers("other")).status_code == 404


def test_management_protection_survives_development_auth_mode(service, monkeypatch):
    client, _, _, _, _, _ = service
    operations._runtime.auth_required = False
    for path, method, body in (("/answers", "get", None), ("/diagnostics/reports", "get", None),
                               ("/diagnostics/reports", "post", {})):
        kwargs = {} if body is None else {"json": body}
        assert getattr(client, method)(path, **kwargs).status_code == 401
