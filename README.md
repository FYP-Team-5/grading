# Student answer grading service

A stateful FastAPI service for grading one or more answers from an exam or quiz
attempt. The frontend identifies the exam and attempt; the service reads the exact
rubric metadata from the RAG service's PostgreSQL database, retrieves the referenced
chunks directly from the shared Qdrant collection, and asks an OpenAI-compatible
local LLM to grade each answer.

The service persists courses, exams, questions, attempt limits, student answers, AI
scores, feedback, and grading evidence. It never creates embeddings, never calls the
embedding model container, and does not call the RAG HTTP search endpoint. RAG has
already embedded and stored the rubric before grading begins.

```text
Instructor -> Grading API -> courses / exams / questions ----+
Instructor -> RAG API -> embedding model -> vectors -> Qdrant|
                         versioned rubric metadata -> Postgres|
Instructor -> Grading API -> question-to-chunk mappings      |
                                                             v
Frontend -> create attempt -> submit answer(s) -> Grading API
                                                   |  |  |
                          rubric metadata ----------+  |  +-> grading Postgres
                          exact Qdrant chunks ----------+      answers + feedback
                                                   |
                                                   +-------> local LLM
```

## Ownership and data model

The RAG service owns the `rubrics` table, original documents, and Qdrant points. The
grading service only reads those records. It creates separate `grading_*` tables for
its own data; both database URLs point to the same PostgreSQL database in the Docker
Compose setup.

| Record | Relationship and behavior |
|---|---|
| Course | Has many exams and quizzes |
| Exam/quiz | Belongs to one course, has many questions, an attempt limit, and one active `rubric_id` |
| Question | Belongs to one exam and maps to one or more rubric `chunk_index` values |
| Attempt | Belongs to one student and exam and records the rubric ID/version used when it began |
| Response | Stores one student's answer for one question in an attempt |
| Question grade | Stores score, feedback, criteria, rubric/chunk IDs, LLM model, and prompt version |

### Database-backed models

| Model | Attributes | Purpose |
|---|---|---|
| `Course` | `id`, `title`, `created_at` | Represents a course stored in `grading_courses`. |
| `Exam` | `id`, `course_id`, `title`, `type`, `max_attempts`, `rubric_id`, `questions`, `created_at` | Represents an exam/quiz and its active rubric; persisted across `grading_exams` and its questions. |
| `Question` | `id`, `exam_id` (table), `position`, `prompt`, `max_score`, `rubric_chunk_indexes` | Stores an ordered assessment question and its rubric-chunk mapping in `grading_questions`. |
| `Attempt` | `id`, `exam_id`, `student_id`, `attempt_number`, `status`, `rubric_id`, `rubric_version`, `started_at`, `graded_at`, `error` | Records a student's assessment attempt and immutable rubric snapshot in `grading_attempts`. |
| Response record | `id`, `attempt_id`, `question_id`, `answer`, `created_at`, `updated_at` | Stores one submitted answer per attempt/question in `grading_responses`. |
| `QuestionGrade` | `question_id`, `score`, `max_score`, `percentage`, `feedback`, `criteria`, `rubric_chunk_ids` | Domain projection of a row in `grading_question_grades`, which also stores attempt/response IDs and LLM audit fields. |
| `RubricMetadata` | `id`, `document_id`, `version`, `course_id`, `exam_id`, `processed`, `processing_status`, `processing_error`, `archived`, `chunk_count`, `chunk_ids` | Read-only model of RAG-owned rubric metadata used during grading. |

### DTOs

DTO definitions live in `app/dto/`; they are transport or service-boundary payloads and are not database entities.

| DTO | Attributes | Purpose |
|---|---|---|
| `CourseCreate` | `id`, `title` | Course creation request. |
| `QuestionCreate` | `id`, `prompt`, `max_score`, `rubric_chunk_indexes` | Nested question creation request. |
| `ExamCreate` | `id`, `title`, `type`, `max_attempts`, `rubric_id`, `questions` | Exam/quiz creation request. |
| `ExamRubricUpdate` | `rubric_id` | Selects a new active rubric for an exam. |
| `RubricChunkMappingRequest` | `chunk_indexes` | Updates a question's rubric mapping. |
| `QuestionResponseSubmission` | `question_id`, `answer` | Carries one submitted answer. |
| `GradeAttemptRequest` | `responses`, `finalize` | Submits one or more answers for grading. |
| `AttemptGradeResponse` | `attempt`, `grades`, `total_score`, `max_score`, `percentage`, `completed_questions`, `total_questions` | Aggregated attempt result returned by the API. |
| `RetrievedRubricChunk` | `id`, `content`, `metadata` | Qdrant retrieval result passed into grading. |
| `CriterionGrade` | `criterion`, `score`, `max_score`, `feedback` | One criterion in the LLM's structured output. |
| `GradingResult` | `score`, `max_score`, `feedback`, `criteria` | Validated structured response from the grading LLM. |
| `HealthResponse` | `status`, `postgres`, `qdrant`, `llm`, `model` | Health endpoint response. |

## Run with Docker

Start the RAG project first because it provides PostgreSQL, Qdrant, and the external
Docker network:

```bash
cd ../rag
docker compose up --build -d

cd ../grading
cp .env.example .env
# Set LLM_MODEL to a model served by your local OpenAI-compatible endpoint.
docker compose up --build -d
docker compose ps
```

Defaults:

- API and Swagger UI: <http://localhost:8001/docs>
- Health: <http://localhost:8001/health>
- Shared PostgreSQL: `postgres:5432/rag`
- Shared Qdrant collection: `qdrant:6333/rubric_chunks`
- Local LLM: `http://host.docker.internal:11434/v1/chat/completions`

The grading container joins `rubric-rag_default`. If the RAG Compose project or
network has another name, update `networks.rag_network.name` in `compose.yaml`.

There are two distinct model dependencies:

- The embedding model container is called directly by RAG during rubric processing
  and semantic search. Grading never contacts it.
- The grading LLM is called by this service for each submitted answer and must expose
  the configured OpenAI-compatible chat-completions endpoint.

Once a rubric is processed, grading retrieves its exact stored Qdrant points by ID;
it does not perform semantic search or generate a query embedding.

## Complete user flows

These examples assume no `API_KEY`. When one is configured, include
`-H 'X-API-Key: <key>'` on every `/api/v1` request.

### 1. Instructor creates or lists courses

```bash
curl -X POST http://localhost:8001/api/v1/courses \
  -H 'Content-Type: application/json' \
  -d '{"id":"HIST-101","title":"World History"}'

curl http://localhost:8001/api/v1/courses
```

### 2. Instructor creates and inspects an exam or quiz

Create the exam and its questions. `rubric_id` must match the rubric that will be
uploaded to RAG. Leave `rubric_chunk_indexes` empty until processing reveals the
chunk indexes.

```bash
curl -X POST http://localhost:8001/api/v1/courses/HIST-101/exams \
  -H 'Content-Type: application/json' \
  -d '{
    "id":"history-midterm",
    "title":"History midterm",
    "type":"exam",
    "max_attempts":2,
    "rubric_id":"history-midterm-rubric-v1",
    "questions":[
      {"id":"history-midterm-q1","prompt":"Explain the primary cause.","max_score":10},
      {"id":"history-midterm-q2","prompt":"Evaluate the evidence.","max_score":5}
    ]
  }'

curl http://localhost:8001/api/v1/courses/HIST-101/exams
curl http://localhost:8001/api/v1/exams/history-midterm
```

Use `type: "quiz"` for a quiz. `max_attempts: 1` permits one attempt per student;
higher values permit that many attempts per student for this exam.

### 3. Instructor uploads and waits for the rubric

Upload through the RAG API with the exact same course and exam IDs:

```bash
curl -X POST http://localhost:8000/api/v1/rubrics \
  -F 'file=@./history-midterm-rubric.pdf' \
  -F 'rubric_id=history-midterm-rubric-v1' \
  -F 'version=1' \
  -F 'course_id=HIST-101' \
  -F 'exam_id=history-midterm'

curl http://localhost:8000/api/v1/rubrics/history-midterm-rubric-v1/status
```

After upload, RAG chunks the document, sends text batches directly to its external
embedding model container, validates the vectors, and upserts them to Qdrant. Do not
create attempts until the status is `completed`. The grading service rejects missing,
failed, processing, archived, or course/exam-mismatched active rubrics.

### 4. Instructor maps questions to rubric chunks

Inspect processed chunks and their zero-based `chunk_index` values:

```bash
curl http://localhost:8000/api/v1/rubrics/history-midterm-rubric-v1/chunks
```

Map each question to all chunks containing its relevant grading criteria:

```bash
curl -X PUT \
  http://localhost:8001/api/v1/exams/history-midterm/questions/history-midterm-q1/rubric-chunks \
  -H 'Content-Type: application/json' \
  -d '{"chunk_indexes":[0,1]}'

curl -X PUT \
  http://localhost:8001/api/v1/exams/history-midterm/questions/history-midterm-q2/rubric-chunks \
  -H 'Content-Type: application/json' \
  -d '{"chunk_indexes":[2]}'
```

The API verifies that every supplied index exists. Every question must have a
non-empty, valid mapping before an attempt can be created.

### 5. Instructor activates a new rubric version

Upload a new RAG record with a new `rubric_id` and version but the same `course_id`
and `exam_id`. After processing completes, activate it:

```bash
curl -X PUT http://localhost:8001/api/v1/exams/history-midterm/rubric \
  -H 'Content-Type: application/json' \
  -d '{"rubric_id":"history-midterm-rubric-v2"}'
```

Review and update question mappings because chunk indexes can change. See
[Limitations](#limitations) before changing a rubric while attempts are in progress.

### 6. Student starts an attempt

```bash
curl -X POST http://localhost:8001/api/v1/exams/history-midterm/attempts \
  -H 'X-Student-ID: student-42'
```

Save the returned attempt `id`. Creating it consumes one of this student's allowed
attempts. The API returns `409 Conflict` once the configured limit is reached.

`X-Student-ID` is an identity handoff, not authentication by itself. In production,
a trusted authentication gateway must derive this value from the authenticated user
and replace any client-supplied header.

### 7. Student lists their attempts

```bash
curl http://localhost:8001/api/v1/exams/history-midterm/attempts \
  -H 'X-Student-ID: student-42'
```

Only attempts for that student and exam are returned.

### 8. Student submits one question at a time

Use `finalize: false` while more questions remain:

```bash
curl -X POST \
  http://localhost:8001/api/v1/exams/history-midterm/attempts/<attempt-id>/grade \
  -H 'X-Student-ID: student-42' \
  -H 'Content-Type: application/json' \
  -d '{
    "responses":[
      {"question_id":"history-midterm-q1","answer":"Economic pressure was ..."}
    ],
    "finalize":false
  }'
```

The response and AI grade are saved immediately; the attempt remains `in_progress`.
A later call can submit another question. Resubmitting a question before finalization
updates its saved answer and grade.

### 9. Student submits multiple questions and finalizes

A request can contain several unique question IDs. Set `finalize: true` only when
this request plus earlier submissions covers every question:

```bash
curl -X POST \
  http://localhost:8001/api/v1/exams/history-midterm/attempts/<attempt-id>/grade \
  -H 'X-Student-ID: student-42' \
  -H 'Content-Type: application/json' \
  -d '{
    "responses":[
      {"question_id":"history-midterm-q1","answer":"Economic pressure was ..."},
      {"question_id":"history-midterm-q2","answer":"The source demonstrates ..."}
    ],
    "finalize":true
  }'
```

Finalization returns `409` while a question is ungraded. A finalized attempt is
immutable. The result contains per-question scores, criteria, feedback, Qdrant chunk
IDs, and aggregate totals. If the LLM or response validation fails, the attempt is
marked `failed`; the same unfinalized attempt may be retried.

### 10. Student retrieves saved feedback

```bash
curl \
  http://localhost:8001/api/v1/exams/history-midterm/attempts/<attempt-id> \
  -H 'X-Student-ID: student-42'
```

The service checks that the attempt belongs to the supplied student and exam. Stored
scores and feedback are returned without calling the LLM again.

## API summary

| Method | Path | User action |
|---|---|---|
| `GET` | `/health` | Check PostgreSQL, Qdrant, and LLM readiness |
| `POST` / `GET` | `/api/v1/courses` | Create or list courses |
| `POST` / `GET` | `/api/v1/courses/{course_id}/exams` | Create or list exams/quizzes |
| `GET` | `/api/v1/exams/{exam_id}` | Inspect an exam and its questions |
| `PUT` | `/api/v1/exams/{exam_id}/rubric` | Activate a processed rubric version |
| `PUT` | `/api/v1/exams/{exam_id}/questions/{question_id}/rubric-chunks` | Map a question to chunks |
| `POST` / `GET` | `/api/v1/exams/{exam_id}/attempts` | Create or list a student's attempts |
| `POST` | `/api/v1/exams/{exam_id}/attempts/{attempt_id}/grade` | Save and grade one/many answers |
| `GET` | `/api/v1/exams/{exam_id}/attempts/{attempt_id}` | Retrieve saved result and feedback |

## Local grading LLM contract

This is the grading model, not RAG's embedding model. The endpoint must support
OpenAI-style `POST /v1/chat/completions` and
`GET /v1/models`. The chat response's `choices[0].message.content` must be a JSON
object containing `score`, `max_score`, `feedback`, and `criteria`. The service
rejects out-of-range scores and any LLM `max_score` that differs from the question's
configured maximum. Temperature defaults to zero.

## Limitations

- Question IDs are currently the primary key of `grading_questions`, so they must be
  unique across the whole grading service, not only within one exam. Use namespaced
  IDs such as `history-midterm-q1` rather than reusing `q1`.
- An attempt records its rubric ID and version, but does not snapshot the question
  prompt, maximum score, or chunk-index mapping. Grading reads those fields from the
  current exam record. Do not activate/remap a new rubric or otherwise change exam
  question configuration while any attempt for that exam is still in progress.
- Catalog creation and mapping endpoints are protected only by the optional shared
  `X-API-Key`; production deployments should put role-aware authorization in front
  of the service.
- Grading multiple answers invokes the LLM sequentially. A failure can leave earlier
  answers from that request saved while the attempt is marked `failed`.

## Configuration and non-Docker use

See `.env.example`. For a process running directly on the workstation:

```dotenv
RAG_DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5432/rag
GRADING_DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5432/rag
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=rubric_chunks
LLM_URL=http://localhost:11434/v1/chat/completions
LLM_MODEL=your-local-model
```

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

`RAG_DATABASE_URL` needs read access to the RAG-owned `rubrics` table.
`GRADING_DATABASE_URL` needs create/update access to the `grading_*` tables. For
production, use least-privilege database users, TLS, restricted CORS, and a non-empty
`API_KEY`.

## Develop, test, and release

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install ruff==0.16.3
make ci
```

Tests use SQLite, fake Qdrant clients, and in-memory HTTP transports; no live LLM or
storage service is required.

The pull-request [CI workflow](.github/workflows/ci.yml) installs dependencies in
the YAML, then runs Ruff and pytest. Configure its lint and test jobs as required
branch-protection checks to prevent a failing PR from being merged. GitHub cannot
undo a merge after it has occurred.

The [post-merge workflow](.github/workflows/post-merge.yml) reruns both checks on
each push to `main`. Only a successful run creates the next tag, beginning with
`v0.1`. Its GHCR Docker build/publish job is present but fully commented out.
