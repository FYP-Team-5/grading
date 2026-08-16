import asyncio

import pytest

from app.config import Settings
from app.model import (
    CriterionGrade,
    GradeRequest,
    GradingResult,
    RetrievedRubricChunk,
    RubricMetadata,
)
from app.service import (
    GradingService,
    RubricChunksMissingError,
    RubricProcessingIncompleteError,
    StudentAnswerTooLargeError,
)


class FakeMetadataStore:
    def __init__(self, rubric: RubricMetadata) -> None:
        self.rubric = rubric
        self.get_call = None

    def close(self) -> None:
        pass

    def health(self) -> bool:
        return True

    def get(self, rubric_id: str) -> RubricMetadata:
        self.get_call = rubric_id
        return self.rubric


class FakeChunkStore:
    def __init__(self, chunks) -> None:
        self.chunks = chunks
        self.retrieve_call = None

    def close(self) -> None:
        pass

    def health(self) -> bool:
        return True

    def retrieve(self, **kwargs):
        self.retrieve_call = kwargs
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


def completed_rubric() -> RubricMetadata:
    return RubricMetadata(
        id="history-v1",
        document_id="document-1",
        processed=True,
        processing_status="completed",
        chunk_count=1,
        chunk_ids=["chunk-1"],
    )


def test_grade_uses_postgres_chunk_ids_and_builds_prompt() -> None:
    metadata = FakeMetadataStore(completed_rubric())
    chunks = FakeChunkStore(
        [
            RetrievedRubricChunk(
                id="chunk-1",
                content="Evidence: 5 points",
                metadata={"rubric_id": "history-v1", "document_id": "document-1"},
            )
        ]
    )
    llm = FakeLLMClient()
    service = GradingService(
        Settings(),
        metadata_store=metadata,
        chunk_store=chunks,
        llm_client=llm,
    )

    response = asyncio.run(
        service.grade(
            "history-v1",
            GradeRequest(
                question="Explain the cause.",
                student_answer="Economic pressure was the main cause.",
            ),
        ),
    )

    assert metadata.get_call == "history-v1"
    assert chunks.retrieve_call == {
        "chunk_ids": ["chunk-1"],
        "rubric_id": "history-v1",
        "document_id": "document-1",
    }
    assert "Evidence: 5 points" in llm.grade_call["user_prompt"]
    assert "Explain the cause" in llm.grade_call["user_prompt"]
    assert response.percentage == 75
    assert response.rubric_id == "history-v1"


def test_grade_stops_when_postgres_chunk_metadata_is_empty() -> None:
    rubric = completed_rubric()
    rubric.chunk_count = 0
    rubric.chunk_ids = []
    service = GradingService(
        Settings(),
        metadata_store=FakeMetadataStore(rubric),
        chunk_store=FakeChunkStore([]),
        llm_client=FakeLLMClient(),
    )

    with pytest.raises(RubricChunksMissingError, match="history-v1"):
        asyncio.run(service.grade("history-v1", GradeRequest(student_answer="answer")))


def test_grade_rejects_unprocessed_rubric() -> None:
    rubric = completed_rubric()
    rubric.processed = False
    rubric.processing_status = "processing"
    service = GradingService(
        Settings(),
        metadata_store=FakeMetadataStore(rubric),
        chunk_store=FakeChunkStore([]),
        llm_client=FakeLLMClient(),
    )

    with pytest.raises(RubricProcessingIncompleteError, match="processing"):
        asyncio.run(service.grade("history-v1", GradeRequest(student_answer="answer")))


def test_grade_enforces_configured_answer_limit() -> None:
    service = GradingService(
        Settings(max_answer_characters=1000),
        metadata_store=FakeMetadataStore(completed_rubric()),
        chunk_store=FakeChunkStore([]),
        llm_client=FakeLLMClient(),
    )

    with pytest.raises(StudentAnswerTooLargeError):
        asyncio.run(
            service.grade("history-v1", GradeRequest(student_answer="x" * 1001))
        )
