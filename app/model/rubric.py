from typing import Literal

from pydantic import BaseModel, Field


class RubricMetadata(BaseModel):
    id: str
    document_id: str
    processed: bool
    processing_status: Literal["processing", "completed", "failed"]
    processing_error: str | None = None
    chunk_count: int = Field(ge=0)
    chunk_ids: list[str]
