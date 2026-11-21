"""请求 id：每个响应都带 X-Request-Id，客户端自带的沿用。"""

from fastapi.testclient import TestClient

import main


def client():
	return TestClient(main.app)


def test_response_carries_request_id_header():
	response = client().get("/v1/models")
	assert response.status_code == 200
	assert response.headers.get("x-request-id")


def test_client_supplied_request_id_is_kept():
	response = client().get("/v1/models", headers={"x-request-id": "abc123"})
	assert response.headers.get("x-request-id") == "abc123"
