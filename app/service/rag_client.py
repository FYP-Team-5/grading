from __future__ import annotations

from typing import Any

import httpx

from app.model import RetrievedRubricChunk


class RAGServiceError(RuntimeError):
    """Raised when the rubric RAG service cannot be reached."""


class RAGResponseError(RAGServiceError):
    """Raised when the rubric RAG service returns an invalid response."""


class RubricRAGClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        if client is not None:
            self._client = client
            return

        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            transport=httpx.AsyncHTTPTransport(retries=max_retries),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> bool:
        try:
            response = await self._client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def search(
        self,
        *,
        query: str,
        rubric_id: str,
        k: int,
        score_threshold: float | None,
    ) -> list[RetrievedRubricChunk]:
        body: dict[str, Any] = {"query": query, "rubric_id": rubric_id, "k": k}
        if score_threshold is not None:
            body["score_threshold"] = score_threshold

        try:
            response = await self._client.post(
                f"{self.base_url}/api/v1/search",
                json=body,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RAGServiceError(f"Rubric retrieval request failed: {exc}") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise RAGResponseError("RAG response must contain a 'results' array.")

        chunks: list[RetrievedRubricChunk] = []
        try:
            for item in payload["results"]:
                if not isinstance(item, dict):
                    raise TypeError("result is not an object")
                metadata = item.get("metadata", {})
                if not isinstance(metadata, dict):
                    raise TypeError("metadata is not an object")
                if metadata.get("rubric_id") != rubric_id:
                    raise ValueError("result belongs to another rubric")
                chunks.append(
                    RetrievedRubricChunk(
                        content=item["content"],
                        similarity_score=item["score"],
                        metadata=metadata,
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise RAGResponseError(f"RAG response contains an invalid result: {exc}") from exc
        return chunks
