from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model import CriterionGrade, GradeResponse, RetrievedRubricChunk
from app.service import EmptyRubricContextError, LLMServiceError, RAGServiceError


class FakeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.requests = []

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def health(self) -> dict[str, bool]:
        return {"rag": True, "llm": True}

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
                    content="Award up to five points for accuracy.",
                    similarity_score=0.91,
                    metadata={"rubric_id": rubric_id, "chunk_index": 0},
                )
            ],
        )


class UnhealthyService(FakeService):
    async def health(self) -> dict[str, bool]:
        return {"rag": False, "llm": True}


class EmptyContextService(FakeService):
    async def grade(self, rubric_id, request) -> GradeResponse:
        raise EmptyRubricContextError("No rubric context was retrieved.")


class FailingRAGService(FakeService):
    async def grade(self, rubric_id, request) -> GradeResponse:
        raise RAGServiceError("offline")


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
    assert response.json()["retrieved_chunks"][0]["similarity_score"] == 0.91
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
        "rag": "ok",
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
    assert response.json() == {"detail": {"rag": "unavailable", "llm": "ok"}}


def test_dependency_failures_are_mapped(tmp_path: Path) -> None:
    empty_client, _ = make_client(tmp_path, service_type=EmptyContextService)
    rag_client, _ = make_client(tmp_path, service_type=FailingRAGService)
    llm_client, _ = make_client(tmp_path, service_type=FailingLLMService)

    with empty_client:
        empty = empty_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )
    with rag_client:
        rag = rag_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )
    with llm_client:
        llm = llm_client.post(
            "/api/v1/rubrics/history-v1/grade", json={"student_answer": "answer"}
        )

    assert empty.status_code == 404
    assert rag.status_code == 502
    assert rag.json() == {"detail": "Rubric retrieval service failed."}
    assert llm.status_code == 502
    assert llm.json() == {"detail": "LLM grading request failed."}
