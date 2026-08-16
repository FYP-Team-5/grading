import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.security import APIKeyHeader

from app.db import (
    MetadataStoreError,
    QdrantStoreError,
    RubricMetadataNotFoundError,
)
from app.model import GradeRequest, GradeResponse, HealthResponse
from app.service import (
    GradingService,
    LLMResponseError,
    LLMServiceError,
    RubricChunksMissingError,
    RubricProcessingIncompleteError,
    StudentAnswerTooLargeError,
)


def get_service(request: Request) -> GradingService:
    return request.app.state.grading_service


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Depends(api_key_header)],
) -> None:
    configured = request.app.state.settings.api_key
    if configured and (x_api_key is None or not secrets.compare_digest(x_api_key, configured)):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")


health_router = APIRouter(tags=["health"])
router = APIRouter(dependencies=[Depends(require_api_key)])


@health_router.get("/health", response_model=HealthResponse)
async def health(service: Annotated[GradingService, Depends(get_service)]) -> HealthResponse:
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


@router.post(
    "/rubrics/{rubric_id}/grade",
    response_model=GradeResponse,
    tags=["grading"],
)
async def grade_answer(
    rubric_id: Annotated[
        str,
        Path(
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        ),
    ],
    body: GradeRequest,
    service: Annotated[GradingService, Depends(get_service)],
) -> GradeResponse:
    try:
        return await service.grade(rubric_id, body)
    except StudentAnswerTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except RubricMetadataNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Rubric not found.") from exc
    except RubricProcessingIncompleteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (MetadataStoreError, RubricChunksMissingError) as exc:
        raise HTTPException(status_code=502, detail="Rubric metadata is unavailable.") from exc
    except QdrantStoreError as exc:
        raise HTTPException(status_code=502, detail="Rubric chunk storage failed.") from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail="LLM returned an invalid grade.") from exc
    except LLMServiceError as exc:
        raise HTTPException(status_code=502, detail="LLM grading request failed.") from exc
