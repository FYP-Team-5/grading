import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request
from fastapi.security import APIKeyHeader

from app.db import (
    AttemptLimitExceededError,
    AttemptStateError,
    GradingConflictError,
    GradingRecordNotFoundError,
    GradingStoreError,
    MetadataStoreError,
    QdrantStoreError,
    RubricMetadataNotFoundError,
)
from app.dto import (
    AttemptGradeResponse,
    CourseCreate,
    ExamCreate,
    ExamRubricUpdate,
    GradeAttemptRequest,
    HealthResponse,
    RubricChunkMappingRequest,
)
from app.model import Attempt, Course, Exam, Question
from app.service import (
    GradingService,
    AttemptService,
    CatalogService,
    IncompleteAttemptError,
    LLMResponseError,
    LLMScoreScaleError,
    LLMServiceError,
    RubricChunkMappingError,
    RubricChunksMissingError,
    RubricOwnershipError,
    RubricProcessingIncompleteError,
    StudentAnswerTooLargeError,
)

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def get_service(request: Request) -> GradingService:
    return request.app.state.grading_service

def get_catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog_service

def get_attempt_service(request: Request) -> AttemptService:
    return request.app.state.attempt_service


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Depends(api_key_header)],
) -> None:
    configured = request.app.state.settings.api_key
    if configured and (
        x_api_key is None or not secrets.compare_digest(x_api_key, configured)
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


async def require_student_id(
    student_id: Annotated[
        str, Header(alias="X-Student-ID", min_length=1, max_length=128)
    ],
) -> str:
    if not ID_PATTERN.fullmatch(student_id):
        raise HTTPException(status_code=422, detail="Invalid X-Student-ID header.")
    return student_id


health_router = APIRouter(tags=["health"])
courses_router = APIRouter(prefix="/courses", tags=["catalog"], dependencies=[Depends(require_api_key)])
exams_router = APIRouter(prefix="/exams", tags=["catalog"], dependencies=[Depends(require_api_key)])


@health_router.get("/health", response_model=HealthResponse)
async def health(
    service: Annotated[GradingService, Depends(get_service)],
) -> HealthResponse:
    components = await service.health()
    if not all(components.values()):
        raise HTTPException(
            status_code=503,
            detail={
                name: "ok" if available else "unavailable"
                for name, available in components.items()
            },
        )
    return HealthResponse(
        status="ok",
        postgres="ok",
        qdrant="ok",
        llm="ok",
        model=service.settings.llm_model,
    )


@courses_router.post("", response_model=Course, status_code=201)
async def create_course(
    body: CourseCreate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Course:
    try:
        return await service.create_course(body)
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@courses_router.get("", response_model=list[Course])
async def list_courses(
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> list[Course]:
    return await service.list_courses()


@courses_router.post(
    "/{course_id}/exams",
    response_model=Exam,
    status_code=201,
)
async def create_exam(
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: ExamCreate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Exam:
    try:
        return await service.create_exam(course_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course not found.") from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@courses_router.get(
    "/{course_id}/exams",
    response_model=list[Exam],
)
async def list_exams(
    course_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> list[Exam]:
    try:
        return await service.list_exams(course_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Course not found.") from exc


@exams_router.get("/{exam_id}", response_model=Exam)
async def get_exam(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Exam:
    try:
        return await service.get_exam(exam_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Exam not found.") from exc


@exams_router.put("/{exam_id}/rubric", response_model=Exam)
async def update_exam_rubric(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: ExamRubricUpdate,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Exam:
    try:
        return await service.update_exam_rubric(exam_id, body)
    except (GradingRecordNotFoundError, RubricMetadataNotFoundError) as exc:
        raise HTTPException(
            status_code=404, detail="Exam or rubric not found."
        ) from exc
    except (RubricOwnershipError, RubricProcessingIncompleteError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MetadataStoreError as exc:
        raise HTTPException(status_code=502, detail="Rubric metadata failed.") from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@exams_router.put(
    "/{exam_id}/questions/{question_id}/rubric-chunks",
    response_model=Question,
)
async def map_question_chunks(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    question_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    body: RubricChunkMappingRequest,
    service: Annotated[CatalogService, Depends(get_catalog_service)],
) -> Question:
    try:
        return await service.map_question_chunks(
            exam_id, question_id, body.chunk_indexes
        )
    except (GradingRecordNotFoundError, RubricMetadataNotFoundError) as exc:
        raise HTTPException(
            status_code=404, detail="Exam, question, or rubric not found."
        ) from exc
    except (RubricChunkMappingError, RubricOwnershipError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RubricProcessingIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError, QdrantStoreError) as exc:
        raise HTTPException(status_code=502, detail="Rubric storage failed.") from exc


@exams_router.post(
    "/{exam_id}/attempts",
    response_model=Attempt,
    status_code=201,
    tags=["attempts"],
)
async def create_attempt(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> Attempt:
    try:
        return await service.create_attempt(exam_id, student_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Exam not found.") from exc
    except RubricMetadataNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="Exam rubric has not been uploaded."
        ) from exc
    except AttemptLimitExceededError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        RubricChunkMappingError,
        RubricOwnershipError,
        RubricProcessingIncompleteError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GradingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError, QdrantStoreError) as exc:
        raise HTTPException(status_code=502, detail="Rubric storage failed.") from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@exams_router.get(
    "/{exam_id}/attempts",
    response_model=list[Attempt],
    tags=["attempts"],
)
async def list_attempts(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> list[Attempt]:
    try:
        return await service.list_attempts(exam_id, student_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Exam not found.") from exc


@exams_router.post(
    "/{exam_id}/attempts/{attempt_id}/grade",
    response_model=AttemptGradeResponse,
    tags=["grading"],
)
async def grade_attempt(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    body: GradeAttemptRequest,
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> AttemptGradeResponse:
    try:
        return await service.grade_attempt(exam_id, attempt_id, student_id, body)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Exam or attempt not found."
        ) from exc
    except RubricMetadataNotFoundError as exc:
        raise HTTPException(
            status_code=409, detail="Attempt rubric is unavailable."
        ) from exc
    except RubricProcessingIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (AttemptStateError, IncompleteAttemptError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RubricOwnershipError, RubricChunkMappingError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except StudentAnswerTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError, QdrantStoreError) as exc:
        raise HTTPException(status_code=502, detail="Rubric storage failed.") from exc
    except (LLMResponseError, LLMScoreScaleError) as exc:
        raise HTTPException(
            status_code=502, detail="LLM returned an invalid grade."
        ) from exc
    except LLMServiceError as exc:
        raise HTTPException(
            status_code=502, detail="LLM grading request failed."
        ) from exc
    except GradingStoreError as exc:
        raise HTTPException(status_code=502, detail="Grading database failed.") from exc


@exams_router.get(
    "/{exam_id}/attempts/{attempt_id}",
    response_model=AttemptGradeResponse,
    tags=["attempts"],
)
async def get_attempt_result(
    exam_id: Annotated[str, Path(pattern=ID_PATTERN.pattern)],
    attempt_id: str,
    student_id: Annotated[str, Depends(require_student_id)],
    service: Annotated[AttemptService, Depends(get_attempt_service)],
) -> AttemptGradeResponse:
    try:
        return await service.get_attempt_result(exam_id, attempt_id, student_id)
    except GradingRecordNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Exam or attempt not found."
        ) from exc
    except AttemptStateError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
