import asyncio

import pytest
from pydantic import ValidationError

from app.dto import (
    ExamCreate,
    GradeAttemptRequest,
    QuestionCreate,
    QuestionResponseSubmission,
)
from app.service import AttemptService, CatalogService


class CoreSpy:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        async def call(*args, **kwargs): self.calls.append((name, args, kwargs)); return name
        return call


def test_catalog_and_attempt_facades_expose_only_owned_operations() -> None:
    core = CoreSpy(); catalog = CatalogService(core); attempts = AttemptService(core)
    assert asyncio.run(catalog.list_courses()) == "list_courses"
    assert asyncio.run(catalog.get_exam("exam")) == "get_exam"
    assert asyncio.run(attempts.create_attempt("exam", "student")) == "create_attempt"
    assert asyncio.run(attempts.get_attempt_result("exam", "attempt", "student")) == "get_attempt_result"
    with pytest.raises(AttributeError):
        catalog.__getattr__("grade_attempt")
    with pytest.raises(AttributeError):
        attempts.__getattr__("create_course")


@pytest.mark.parametrize("value", [-1, 0])
def test_question_score_boundary_rejects_non_positive_values(value: float) -> None:
    with pytest.raises(ValidationError): QuestionCreate(id="q1", prompt="Question", max_score=value)


def test_exam_accepts_limits_and_rejects_mutated_duplicate_question_ids() -> None:
    question = QuestionCreate(id="q1", prompt="Question", max_score=1)
    assert ExamCreate(id="e1", title="Exam", max_attempts=100, rubric_id="r1", questions=[question]).max_attempts == 100
    with pytest.raises(ValidationError):
        ExamCreate(id="e1", title="Exam", max_attempts=101, rubric_id="r1", questions=[question])
    with pytest.raises(ValidationError):
        ExamCreate(id="e1", title="Exam", rubric_id="r1", questions=[question, question])


def test_grade_request_rejects_blank_and_duplicate_answers() -> None:
    with pytest.raises(ValidationError): QuestionResponseSubmission(question_id="q1", answer="   ")
    response = QuestionResponseSubmission(question_id="q1", answer="answer")
    with pytest.raises(ValidationError): GradeAttemptRequest(responses=[response, response])
