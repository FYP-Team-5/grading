from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import AttemptLimitExceededError
from app.dto import AttemptGradeResponse
from app.main import create_app
from app.model import (
    Attempt,
    Course,
    Exam,
    Question,
    QuestionGrade,
)


class FakeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls = []

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def health(self) -> dict[str, bool]:
        return {"postgres": True, "qdrant": True, "llm": True}

    async def create_course(self, request) -> Course:
        self.calls.append(("course", request))
        return Course(**request.model_dump(), created_at=datetime.now(UTC))

    async def list_courses(self) -> list[Course]:
        return [Course(id="HIST-101", title="History", created_at=datetime.now(UTC))]

    async def create_exam(self, course_id, request) -> Exam:
        self.calls.append(("exam", course_id, request))
        return make_exam(course_id=course_id)

    async def get_exam(self, exam_id) -> Exam:
        return make_exam()

    async def list_exams(self, course_id) -> list[Exam]:
        return [make_exam(course_id=course_id)]

    async def update_exam_rubric(self, exam_id, request) -> Exam:
        return make_exam().model_copy(update={"rubric_id": request.rubric_id})

    async def map_question_chunks(
        self, exam_id, question_id, chunk_indexes
    ) -> Question:
        return (
            make_exam()
            .questions[0]
            .model_copy(update={"rubric_chunk_indexes": chunk_indexes})
        )

    async def create_attempt(self, exam_id, student_id) -> Attempt:
        self.calls.append(("attempt", exam_id, student_id))
        return make_attempt(student_id=student_id)

    async def grade_attempt(self, exam_id, attempt_id, student_id, request):
        self.calls.append(("grade", exam_id, attempt_id, student_id, request))
        return make_grade_response(student_id=student_id)

    async def get_attempt_result(self, exam_id, attempt_id, student_id):
        return make_grade_response(student_id=student_id)

    async def list_attempts(self, exam_id, student_id) -> list[Attempt]:
        return [make_attempt(student_id=student_id)]


class UnhealthyService(FakeService):
    async def health(self) -> dict[str, bool]:
        return {"postgres": True, "qdrant": False, "llm": True}


class AttemptLimitService(FakeService):
    async def create_attempt(self, exam_id, student_id) -> Attempt:
        raise AttemptLimitExceededError("Exam allows 1 attempt.")


def make_exam(course_id: str = "HIST-101") -> Exam:
    return Exam(
        id="history-midterm",
        course_id=course_id,
        title="History midterm",
        type="exam",
        max_attempts=1,
        rubric_id="history-rubric-v1",
        created_at=datetime.now(UTC),
        questions=[
            Question(
                id="history-midterm-q1",
                position=0,
                prompt="Explain the cause.",
                max_score=10,
                rubric_chunk_indexes=[0],
            )
        ],
    )


def make_attempt(student_id: str = "student-1") -> Attempt:
    return Attempt(
        id="11111111-1111-1111-1111-111111111111",
        exam_id="history-midterm",
        student_id=student_id,
        attempt_number=1,
        status="graded",
        rubric_id="history-rubric-v1",
        rubric_version="1",
        started_at=datetime.now(UTC),
        graded_at=datetime.now(UTC),
    )


def make_grade_response(student_id: str = "student-1") -> AttemptGradeResponse:
    return AttemptGradeResponse(
        attempt=make_attempt(student_id),
        grades=[
            QuestionGrade(
                question_id="history-midterm-q1",
                score=8,
                max_score=10,
                percentage=80,
                feedback="Good answer.",
                criteria=[],
                rubric_chunk_ids=["chunk-1"],
            )
        ],
        total_score=8,
        max_score=10,
        percentage=80,
        completed_questions=1,
        total_questions=1,
    )


def make_client(
    tmp_path: Path,
    *,
    api_key: str | None = None,
    service_type: type[FakeService] = FakeService,
) -> tuple[TestClient, FakeService]:
    settings = Settings(api_key=api_key)
    service = service_type(settings)
    return TestClient(create_app(settings=settings, service=service)), service


def test_admin_can_create_course_exam_and_map_question(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        course = client.post(
            "/api/v1/courses", json={"id": "HIST-101", "title": "History"}
        )
        exam = client.post(
            "/api/v1/courses/HIST-101/exams",
            json={
                "id": "history-midterm",
                "title": "History midterm",
                "max_attempts": 1,
                "rubric_id": "history-rubric-v1",
                "questions": [
                    {
                        "id": "history-midterm-q1",
                        "prompt": "Explain the cause.",
                        "max_score": 10,
                    }
                ],
            },
        )
        mapping = client.put(
            "/api/v1/exams/history-midterm/questions/history-midterm-q1/rubric-chunks",
            json={"chunk_indexes": [0, 1]},
        )

    assert course.status_code == 201
    assert exam.status_code == 201
    assert mapping.json()["rubric_chunk_indexes"] == [0, 1]


def test_admin_can_list_catalog_and_activate_rubric(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        courses = client.get("/api/v1/courses")
        exams = client.get("/api/v1/courses/HIST-101/exams")
        activated = client.put(
            "/api/v1/exams/history-midterm/rubric",
            json={"rubric_id": "history-rubric-v2"},
        )

    assert courses.json()[0]["id"] == "HIST-101"
    assert exams.json()[0]["id"] == "history-midterm"
    assert activated.json()["rubric_id"] == "history-rubric-v2"


def test_student_can_create_attempt_and_grade_multiple_responses(
    tmp_path: Path,
) -> None:
    client, service = make_client(tmp_path)
    headers = {"X-Student-ID": "student-1"}
    with client:
        attempt = client.post("/api/v1/exams/history-midterm/attempts", headers=headers)
        grade = client.post(
            "/api/v1/exams/history-midterm/attempts/attempt-1/grade",
            headers=headers,
            json={
                "responses": [
                    {"question_id": "history-midterm-q1", "answer": "Answer one."},
                    {"question_id": "history-midterm-q2", "answer": "Answer two."},
                ],
                "finalize": True,
            },
        )

    assert attempt.status_code == 201
    assert grade.status_code == 200
    assert grade.json()["attempt"]["student_id"] == "student-1"
    assert service.calls[-1][0] == "grade"
    assert len(service.calls[-1][-1].responses) == 2


def test_student_can_list_attempts_and_retrieve_feedback(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    headers = {"X-Student-ID": "student-1"}
    with client:
        attempts = client.get(
            "/api/v1/exams/history-midterm/attempts",
            headers=headers,
        )
        feedback = client.get(
            "/api/v1/exams/history-midterm/attempts/attempt-1",
            headers=headers,
        )

    assert attempts.status_code == 200
    assert attempts.json()[0]["student_id"] == "student-1"
    assert feedback.json()["grades"][0]["feedback"] == "Good answer."


def test_student_header_is_required_and_attempt_limit_maps_to_conflict(
    tmp_path: Path,
) -> None:
    client, _ = make_client(tmp_path, service_type=AttemptLimitService)
    with client:
        missing_identity = client.post("/api/v1/exams/history-midterm/attempts")
        limited = client.post(
            "/api/v1/exams/history-midterm/attempts",
            headers={"X-Student-ID": "student-1"},
        )

    assert missing_identity.status_code == 422
    assert limited.status_code == 409


def test_api_key_and_health_behavior(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, api_key="secret")
    with client:
        unauthorized = client.post(
            "/api/v1/courses",
            json={"id": "HIST-101", "title": "History"},
        )
        authorized = client.post(
            "/api/v1/courses",
            headers={"X-API-Key": "secret"},
            json={"id": "HIST-101", "title": "History"},
        )
        health = client.get("/health")

    assert unauthorized.status_code == 401
    assert authorized.status_code == 201
    assert health.status_code == 200


def test_openapi_contains_complete_user_flow(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        schema = client.get("/openapi.json").json()

    assert [tag["name"] for tag in schema["tags"]] == [
        "health",
        "catalog",
        "attempts",
        "grading",
    ]
    assert "/api/v1/courses" in schema["paths"]
    assert "/api/v1/courses/{course_id}/exams" in schema["paths"]
    assert "/api/v1/exams/{exam_id}/attempts" in schema["paths"]
    assert "/api/v1/exams/{exam_id}/attempts/{attempt_id}/grade" in schema["paths"]


def test_unhealthy_dependency_returns_service_unavailable(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path, service_type=UnhealthyService)
    with client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"postgres": "ok", "qdrant": "unavailable", "llm": "ok"}
    }
