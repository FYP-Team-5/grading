from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"


class CourseCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)


class Course(CourseCreate):
    created_at: datetime


class QuestionCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=10_000)
    max_score: float = Field(gt=0)
    rubric_chunk_indexes: list[int] = Field(default_factory=list)

    @field_validator("rubric_chunk_indexes")
    @classmethod
    def chunk_indexes_must_be_unique_and_non_negative(
        cls, value: list[int]
    ) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("rubric chunk indexes must be non-negative")
        if len(value) != len(set(value)):
            raise ValueError("rubric chunk indexes must be unique")
        return value


class Question(QuestionCreate):
    position: int = Field(ge=0)


class ExamCreate(BaseModel):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    type: Literal["exam", "quiz"] = "exam"
    max_attempts: int = Field(default=1, ge=1, le=100)
    rubric_id: str = Field(pattern=ID_PATTERN)
    questions: list[QuestionCreate] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def question_ids_must_be_unique(self) -> "ExamCreate":
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique within an exam")
        return self


class ExamRubricUpdate(BaseModel):
    rubric_id: str = Field(pattern=ID_PATTERN)


class Exam(BaseModel):
    id: str
    course_id: str
    title: str
    type: Literal["exam", "quiz"]
    max_attempts: int
    rubric_id: str
    questions: list[Question]
    created_at: datetime


class RubricChunkMappingRequest(BaseModel):
    chunk_indexes: list[int] = Field(min_length=1)

    @field_validator("chunk_indexes")
    @classmethod
    def indexes_must_be_unique_and_non_negative(cls, value: list[int]) -> list[int]:
        if any(index < 0 for index in value):
            raise ValueError("chunk indexes must be non-negative")
        if len(value) != len(set(value)):
            raise ValueError("chunk indexes must be unique")
        return value


class Attempt(BaseModel):
    id: str
    exam_id: str
    student_id: str
    attempt_number: int = Field(ge=1)
    status: Literal["in_progress", "graded", "failed"]
    rubric_id: str
    rubric_version: str
    started_at: datetime
    graded_at: datetime | None = None
    error: str | None = None


class QuestionResponseSubmission(BaseModel):
    question_id: str = Field(pattern=ID_PATTERN)
    answer: str = Field(min_length=1)

    @field_validator("answer")
    @classmethod
    def answer_must_contain_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer must contain non-whitespace text")
        return value


class GradeAttemptRequest(BaseModel):
    responses: list[QuestionResponseSubmission] = Field(min_length=1, max_length=500)
    finalize: bool = True

    @model_validator(mode="after")
    def response_question_ids_must_be_unique(self) -> "GradeAttemptRequest":
        ids = [response.question_id for response in self.responses]
        if len(ids) != len(set(ids)):
            raise ValueError("each question may appear only once per request")
        return self


class QuestionGrade(BaseModel):
    question_id: str
    score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    percentage: float = Field(ge=0, le=100)
    feedback: str
    criteria: list[dict]
    rubric_chunk_ids: list[str]


class AttemptGradeResponse(BaseModel):
    attempt: Attempt
    grades: list[QuestionGrade]
    total_score: float = Field(ge=0)
    max_score: float = Field(gt=0)
    percentage: float = Field(ge=0, le=100)
    completed_questions: int = Field(ge=0)
    total_questions: int = Field(ge=1)
