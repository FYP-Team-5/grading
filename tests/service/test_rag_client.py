import asyncio
import json

import httpx
import pytest

from app.service import RAGResponseError, RAGServiceError, RubricRAGClient


def test_search_sends_rubric_filter_and_parses_results() -> None:
    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/search"
            assert request.headers["X-API-Key"] == "rag-secret"
            assert json.loads(request.content) == {
                "query": "answer",
                "rubric_id": "rubric-1",
                "k": 5,
                "score_threshold": 0.3,
            }
            return httpx.Response(
                200,
                json={
                    "query": "answer",
                    "results": [
                        {
                            "content": "Accuracy is worth five points.",
                            "score": 0.87,
                            "metadata": {"rubric_id": "rubric-1", "chunk_index": 2},
                        }
                    ],
                },
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"X-API-Key": "rag-secret"},
        )
        client = RubricRAGClient(
            base_url="http://rag.test",
            api_key="rag-secret",
            client=http_client,
        )

        chunks = await client.search(
            query="answer",
            rubric_id="rubric-1",
            k=5,
            score_threshold=0.3,
        )

        assert chunks[0].content.startswith("Accuracy")
        assert chunks[0].similarity_score == 0.87
        await http_client.aclose()

    asyncio.run(exercise())


def test_search_rejects_cross_rubric_result() -> None:
    async def exercise() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "content": "wrong rubric",
                                "score": 0.9,
                                "metadata": {"rubric_id": "rubric-2"},
                            }
                        ]
                    },
                )
            )
        )
        client = RubricRAGClient(base_url="http://rag.test", client=http_client)

        with pytest.raises(RAGResponseError, match="another rubric"):
            await client.search(
                query="answer", rubric_id="rubric-1", k=5, score_threshold=None
            )
        await http_client.aclose()

    asyncio.run(exercise())


def test_search_wraps_upstream_error() -> None:
    async def exercise() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        client = RubricRAGClient(base_url="http://rag.test", client=http_client)

        with pytest.raises(RAGServiceError, match="retrieval request failed"):
            await client.search(
                query="answer", rubric_id="rubric-1", k=5, score_threshold=None
            )
        await http_client.aclose()

    asyncio.run(exercise())
