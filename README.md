# Student answer grading service

A self-hosted FastAPI microservice that reads rubric metadata from the PostgreSQL
database owned by the RAG service, retrieves that rubric's chunks directly from the
shared Qdrant collection, and sends them with a student answer to an external local
LLM.

```text
Frontend --answer + rubric ID--> Grading API
                                      |
                                      +--rubric metadata--> PostgreSQL
                                      |
                                      +--chunk IDs--------> Qdrant
                                      |
                                      +--chunks + answer--> Local LLM
                                      |
Frontend <----score + feedback + retrieved evidence
```

The RAG service remains the only writer and continues to own uploads, processing,
embeddings, and storage schema. The grading service is read-only. It resolves the
requested rubric in PostgreSQL, verifies processing is complete, loads its ordered
`chunk_ids`, and retrieves those exact Qdrant payloads without creating a query
embedding. Each payload's `rubric_id` and `document_id` are checked against the
PostgreSQL record before the text enters the grading prompt.

## API contract

Submit the student answer to the rubric-scoped endpoint:

```bash
curl -X POST http://localhost:8001/api/v1/rubrics/history-short-answer-v1/grade \
  -H 'Content-Type: application/json' \
  -d '{
    "question": "Why did the event occur?",
    "student_answer": "The event occurred because ..."
  }'
```

`question` is optional. The service retrieves all chunks recorded for the rubric so
that every grading criterion is available to the LLM.

Example response:

```json
{
  "rubric_id": "history-short-answer-v1",
  "score": 8,
  "max_score": 10,
  "percentage": 80,
  "feedback": "Accurate explanation; add direct supporting evidence.",
  "criteria": [
    {
      "criterion": "Factual accuracy",
      "score": 4,
      "max_score": 5,
      "feedback": "The central claim is accurate."
    }
  ],
  "retrieved_chunks": [
    {
      "id": "442715d6-f5e2-4d1c-a78e-950ced8cd2c7",
      "content": "5 points: The response is factually accurate...",
      "metadata": {
        "rubric_id": "history-short-answer-v1",
        "chunk_index": 2
      }
    }
  ]
}
```

The retrieved chunks are returned as grading evidence. The service validates that
the LLM returns JSON, positive maximum scores, and scores that do not exceed their
maximums.

## Dependencies

The RAG Compose project must be running because this container joins its
`rubric-rag_default` network and connects to the `postgres` and `qdrant` services by
name. The selected rubric must already be processed. See `../rag/README.md` for
rubric upload and processing instructions.

The LLM must expose an OpenAI-compatible chat completions endpoint:

```http
POST /v1/chat/completions
Content-Type: application/json

{
  "model": "local-model",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "temperature": 0,
  "max_tokens": 2000,
  "response_format": {"type": "json_object"}
}
```

It must return `choices[0].message.content` as a JSON object containing `score`,
`max_score`, `feedback`, and `criteria`. The default port (`11434`) is suitable for
an OpenAI-compatible server exposed there; change `LLM_URL` and `LLM_MODEL` for the
local runtime being used.

## Run with Docker

Start the RAG service first, then configure and start this repository:

```bash
cp .env.example .env
# Set LLM_MODEL in .env to a model available from the local LLM server.
docker compose up --build -d
docker compose ps
```

The container joins the existing `rubric-rag_default` Docker network for PostgreSQL
and Qdrant. It reaches the local LLM on the workstation through
`host.docker.internal`. Defaults are:

- Grading Swagger UI: <http://localhost:8001/docs>
- Grading health: <http://localhost:8001/health>
- PostgreSQL: `postgres:5432/rag`
- Qdrant: `http://qdrant:6333`, collection `rubric_chunks`
- Local LLM: <http://host.docker.internal:11434/v1/chat/completions>

Health returns `503` unless PostgreSQL, the configured Qdrant collection, and the
LLM models endpoint are reachable. If `API_KEY` is configured, frontend grading
calls must include it in the `X-API-Key` header; `/health` remains unauthenticated.
`LLM_API_KEY`, when set, is sent as a Bearer token.

## Run without Docker

When the grading process runs directly on the workstation, use localhost URLs:

```dotenv
DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5432/rag
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=rubric_chunks
LLM_URL=http://localhost:11434/v1/chat/completions
LLM_MODEL=your-local-model
```

Then:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Configuration

All settings are environment variables; see `.env.example`. The most important are
`DATABASE_URL`, `QDRANT_URL`, `QDRANT_COLLECTION`, `QDRANT_API_KEY`, `LLM_URL`,
`LLM_MODEL`, `LLM_API_KEY`, timeout settings, CORS origins, and the optional inbound
`API_KEY`. Database credentials and the collection name must match the RAG service.

## Develop, lint, and test

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install ruff==0.16.3
make test
make lint
# Or run both:
make ci
```

Tests use SQLite, fake Qdrant clients, and in-memory HTTP transports; they do not
require running storage or LLM services.

The [GitHub Actions workflow](.github/workflows/ci.yml) runs on every push and pull
request. Its lint and test jobs run independently on Python 3.12, matching the
Docker image. Dependencies are installed directly in the workflow from
`requirements.txt`; there is no separate CI requirements file. The lint job runs
`python -m ruff check app tests`, and the test job runs `python -m pytest -q`.
