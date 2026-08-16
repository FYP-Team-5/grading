from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class GradeRequest(BaseModel):
    student_answer: str = Field(min_length=1)
    question: str | None = Field(default=None, max_length=10_000)
    retrieval_k: int | None = Field(default=None, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=-1, le=1)

    @field_validator("student_answer")
    @classmethod
    def answer_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("student_answer must contain non-whitespace text")
        return value


class RetrievedRubricChunk(BaseModel):
    content: str
    similarity_score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class CriterionGrade(BaseModel):
    criterion: str = Field(min_length=1)
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    feedback: str = Field(min_length=1)

    @model_validator(mode="after")
    def score_does_not_exceed_maximum(self) -> "CriterionGrade":
        if self.score > self.max_score:
            raise ValueError("criterion score cannot exceed max_score")
        return self


class GradingResult(BaseModel):
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    feedback: str = Field(min_length=1)
    criteria: list[CriterionGrade] = Field(default_factory=list)

    @model_validator(mode="after")
    def score_does_not_exceed_maximum(self) -> "GradingResult":
        if self.score > self.max_score:
            raise ValueError("score cannot exceed max_score")
        return self


class GradeResponse(GradingResult):
    rubric_id: str
    percentage: float = Field(ge=0, le=100)
    retrieved_chunks: list[RetrievedRubricChunk]
