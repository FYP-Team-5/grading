import asyncio

import pytest

from app.config import Settings
from app.model import CriterionGrade, GradeRequest, GradingResult, RetrievedRubricChunk
from app.service import (
    EmptyRubricContextError,
    GradingService,
    StudentAnswerTooLargeError,
)


class FakeRAGClient:
    def __init__(self, chunks) -> None:
        self.chunks = chunks
        self.search_call = None

    async def close(self) -> None:
        pass

    async def health(self) -> bool:
        return True

    async def search(self, **kwargs):
        self.search_call = kwargs
        return self.chunks


class FakeLLMClient:
    def __init__(self) -> None:
        self.grade_call = None

    async def close(self) -> None:
        pass

    async def health(self) -> bool:
        return True

    async def grade(self, **kwargs) -> GradingResult:
        self.grade_call = kwargs
        return GradingResult(
            score=7.5,
            max_score=10,
            feedback="Use more evidence.",
            criteria=[
                CriterionGrade(
                    criterion="Evidence",
                    score=3,
                    max_score=5,
                    feedback="One relevant example.",
                )
            ],
        )


def test_grade_retrieves_only_selected_rubric_and_builds_prompt() -> None:
    rag = FakeRAGClient(
        [
            RetrievedRubricChunk(
                content="Evidence: 5 points",
                similarity_score=0.8,
                metadata={"rubric_id": "history-v1"},
            )
        ]
    )
    llm = FakeLLMClient()
    service = GradingService(Settings(), rag_client=rag, llm_client=llm)

    response = asyncio.run(
        service.grade(
            "history-v1",
            GradeRequest(
                question="Explain the cause.",
                student_answer="Economic pressure was the main cause.",
                retrieval_k=4,
            ),
        ),
    )

    assert rag.search_call["rubric_id"] == "history-v1"
    assert rag.search_call["k"] == 4
    assert "Explain the cause" in rag.search_call["query"]
    assert "Evidence: 5 points" in llm.grade_call["user_prompt"]
    assert response.percentage == 75
    assert response.rubric_id == "history-v1"


def test_grade_stops_when_no_rubric_context_is_found() -> None:
    service = GradingService(
        Settings(),
        rag_client=FakeRAGClient([]),
        llm_client=FakeLLMClient(),
    )

    with pytest.raises(EmptyRubricContextError, match="history-v1"):
        asyncio.run(service.grade("history-v1", GradeRequest(student_answer="answer")))


def test_grade_enforces_configured_answer_limit() -> None:
    service = GradingService(
        Settings(max_answer_characters=1000),
        rag_client=FakeRAGClient([]),
        llm_client=FakeLLMClient(),
    )

    with pytest.raises(StudentAnswerTooLargeError):
        asyncio.run(
            service.grade("history-v1", GradeRequest(student_answer="x" * 1001))
        )
