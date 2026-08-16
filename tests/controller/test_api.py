from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import QdrantStoreError, RubricMetadataNotFoundError
from app.main import create_app
from app.model import CriterionGrade, GradeResponse, RetrievedRubricChunk
from app.service import LLMServiceError, RubricProcessingIncompleteError


class FakeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.requests = []

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def health(self) -> dict[str, bool]:
        return {"postgres": True, "qdrant": True, "llm": True}

    async def grade(self, rubric_id, request) -> GradeResponse:
        self.requests.append((rubric_id, request))
        return GradeResponse(
            rubric_id=rubric_id,
            score=8,
            max_score=10,
            percentage=80,
            feedback="Accurate, but add supporting evidence.",
            criteria=[
                CriterionGrade(
                    criterion="Accuracy",
                    score=4,
                    max_score=5,
                    feedback="Mostly accurate.",
                )
            ],
            retrieved_chunks=[
                RetrievedRubricChunk(
                    id="chunk-1",
                    content="Award up to five points for accuracy.",
                    metadata={"rubric_id": rubric_id, "chunk_index": 0},
                )
            ],
        )


class UnhealthyService(FakeService):
    async def health(self) -> dict[str, bool]:
        return {"postgres": True, "qdrant": False, "llm": True}


class MissingRubricService(FakeService):
    async def grade(self, rubric_id, request) -> GradeResponse:
        raise RubricMetadataNotFoundError(rubric_id)


class ProcessingRubricService(FakeService):
    async def grade(self, rubric_id, request) -> GradeResponse:
        raise RubricProcessingIncompleteError("Rubric processing is processing.")


class FailingQdrantService(FakeService):
    async def grade(self, rubric_id, request) -> GradeResponse:
        raise QdrantStoreError("offline")


class FailingLLMService(FakeService):
    async def grade(self, rubric_id, request) -> GradeResponse:
        raise LLMServiceError("offline")


def make_client(
    tmp_path: Path,
    *,
    api_key: str | None = None,
    service_type: type[FakeService] = FakeService,
) -> tuple[TestClient, FakeService]:
    settings = Settings(api_key=api_key)
    service = service_type(settings)
    return TestClient(create_app(settings=settings, service=service)), service


def test_frontend_can_submit_answer_for_rubric(tmp_path: Path) -> None:
    client, service = make_client(tmp_path)
    with client:
        response = client.post(
            "/api/v1/rubrics/history-v1/grade",
            json={
                "question": "Why did the event occur?",
                "student_answer": "It occurred because of economic pressure.",
            },
        )

    assert response.status_code == 200
    assert response.json()["rubric_id"] == "history-v1"
    assert response.json()["percentage"] == 80
    assert response.json()["retrieved_chunks"][0]["id"] == "chunk-1"
    assert service.requests[0][1].student_answer.startswith("It occurred")


def test_request_validation_rejects_empty_answer_and_invalid_rubric_id(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        empty = client.post(
            "/api/v1/rubrics/history-v1/grade",
            json={"student_answer": ""},
        )
        invalid_id = client.post(
            "/api/v1/rubrics/not%20valid/grade",
            json={"student_answer": "answer"},
        )
        whitespace = client.post(
            "/api/v1/rubrics/history-v1/grade",
            json={"student_answer": "   "},
        )

    assert empty.status_code == 422
    assert invalid_id.status_code == 422
    assert whitespace.status_code == 422


def test_api_key_is_optional_but_enforced_when_configured(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, api_key="secret")
    with client:
        unauthorized = client.post(
            "/api/v1/rubrics/history-v1/grade",
            json={"student_answer": "answer"},
        )
        authorized = client.post(
            "/api/v1/rubrics/history-v1/grade",
            headers={"X-API-Key": "secret"},
            json={"student_answer": "answer"},
        )
        health = client.get("/health")

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert health.status_code == 200


def test_health_and_openapi(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        health = client.get("/health")
        openapi = client.get("/openapi.json").json()

    assert health.json() == {
        "status": "ok",
        "postgres": "ok",
        "qdrant": "ok",
        "llm": "ok",
        "model": "local-model",
    }
    assert [tag["name"] for tag in openapi["tags"]] == ["health", "grading"]
    assert "/api/v1/rubrics/{rubric_id}/grade" in openapi["paths"]


def test_unhealthy_dependency_returns_service_unavailable(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, service_type=UnhealthyService)
    with client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"postgres": "ok", "qdrant": "unavailable", "llm": "ok"}
    }


def test_dependency_failures_are_mapped(tmp_path: Path) -> None:
    missing_client, _ = make_client(tmp_path, service_type=MissingRubricService)
    processing_client, _ = make_client(tmp_path, service_type=ProcessingRubricService)
    qdrant_client, _ = make_client(tmp_path, service_type=FailingQdrantService)
    llm_client, _ = make_client(tmp_path, service_type=FailingLLMService)

    with missing_client:
        missing = missing_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )
    with processing_client:
        processing = processing_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )
    with qdrant_client:
        qdrant = qdrant_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )
    with llm_client:
        llm = llm_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )

    assert missing.status_code == 404
    assert processing.status_code == 409
    assert qdrant.status_code == 502
    assert qdrant.json() == {"detail": "Rubric chunk storage failed."}
    assert llm.status_code == 502
    assert llm.json() == {"detail": "LLM grading request failed."}
