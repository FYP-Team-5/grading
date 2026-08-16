# Student answer grading service

A self-hosted FastAPI microservice that retrieves passages for one rubric from the
existing RAG service, sends those passages and a student answer to an external local
LLM, and returns a validated structured grade.

```text
Frontend --answer + rubric ID--> Grading API
                                      |
                                      +--filtered search--> Rubric RAG API
                                      |
                                      +--rubric + answer--> Local LLM
                                      |
Frontend <----score + feedback + retrieved evidence
```

The grading service deliberately uses the RAG HTTP API instead of reading Qdrant
directly. The RAG service remains responsible for embeddings, vector-store schema,
and filtering. Every retrieval request includes `rubric_id`, preventing criteria
from another assignment from entering the grading prompt.

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

`question` is optional. `retrieval_k` and `score_threshold` may also be supplied per
request to override their configured retrieval defaults.

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
      "content": "5 points: The response is factually accurate...",
      "similarity_score": 0.88,
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

The rubric RAG service must be running and the selected rubric must already be
processed. See `../rag/README.md` for rubric upload and processing instructions.

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

The container reaches services on the workstation through
`host.docker.internal`. Defaults are:

- Grading Swagger UI: <http://localhost:8001/docs>
- Grading health: <http://localhost:8001/health>
- RAG API: <http://host.docker.internal:8000>
- Local LLM: <http://host.docker.internal:11434/v1/chat/completions>

Health returns `503` unless both the RAG health endpoint and the LLM models endpoint
are reachable. If `API_KEY` is configured, frontend grading calls must include it in
the `X-API-Key` header; `/health` remains unauthenticated. `RAG_API_KEY` is sent only
to the downstream RAG service. `LLM_API_KEY`, when set, is sent as a Bearer token.

## Run without Docker

When the grading process runs directly on the workstation, use localhost URLs:

```dotenv
RAG_URL=http://localhost:8000
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
`RAG_URL`, `RAG_API_KEY`, `RETRIEVAL_K`, `RETRIEVAL_SCORE_THRESHOLD`, `LLM_URL`,
`LLM_MODEL`, `LLM_API_KEY`, timeout/retry settings, CORS origins, and the optional
inbound `API_KEY`.

Semantic retrieval selects the rubric passages most relevant to the question and
answer. If a rubric is short and every criterion must always be graded, set
`RETRIEVAL_K` high enough to cover all chunks; the RAG service caps it at 50.

## Test

```bash
make test
make lint
```

Tests use in-memory HTTP transports and do not require a running RAG or LLM service.
