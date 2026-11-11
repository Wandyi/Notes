# 03 — APIs and the Data Model

An API is a promise about behaviour that outlives the code behind it. For a system whose central
claim is "every statement is supported and current", the API has to make that claim **visible**:
a caller must be able to see which version of which document a claim came from, whether the
answer was fully verified or had claims removed, whether a document was excluded because it was
mid-update, and whether a deletion has actually reached every store. If those facts are not in
the API, nobody downstream can rely on them.

This document defines the external APIs, then the tables and index schemas behind them. It uses
Kestrel's travel policy, `FIN-POL-017`, as the running example.

---

## 1. Five rules the API follows

1. **Every write is idempotent.** Connectors retry. Users double-click. A request carries an
   `Idempotency-Key`, and repeating it returns the original result instead of creating a second
   document or a second version.
2. **Versions are explicit.** Every document response names its live version and any pending
   version. Every citation names the version it cites. "The document" is never ambiguous.
3. **Outcomes are named states, not booleans.** An answer is `VERIFIED`, `PARTIAL`, `EXTRACTIVE`
   or `ABSTAINED` (doc [02](02-architecture-and-the-ledger.md) §6). A deletion is `COMMITTED`,
   `PURGING`, `UNRETRIEVABLE` or `ERASED`. Callers can branch on them.
4. **Long-running work returns a handle.** Ingestion and deletion return a job or deletion ID
   immediately; the caller polls or subscribes for progress.
5. **The answer stream contains only verified material.** Nothing is sent to the client that has
   not passed verification. There is no "draft" event for a client to accidentally render.

---

## 2. Document APIs

### Register a document and upload it

Uploads go directly to the object store through a pre-signed URL, so that a 50 MB scanned
contract does not pass through the API servers. Registration and upload are two steps.

```http
POST /v1/documents
Idempotency-Key: sp-sync-4f1c9e2a-FIN-POL-017-2026-10-21T09:14:03Z
Content-Type: application/json

{
  "external_ref": "sharepoint://finance/policies/FIN-POL-017.pdf",
  "title": "Travel and Expense Policy",
  "department": "finance",
  "doc_type": "policy",
  "author": "usr_8f21c0",
  "visibility": "org_wide",
  "effective_date": "2026-11-01",
  "language_hint": "en",
  "locale_hint": "en-IN",
  "content_type": "application/pdf",
  "size_bytes": 1843200,
  "sha256": "9b1d…e04a"
}
```

```http
HTTP/1.1 201 Created

{
  "doc_id": "doc_01JB7K3V9Q2R",
  "version": { "seq": 5, "status": "AWAITING_UPLOAD" },
  "upload": { "url": "https://…/uploads/doc_01JB7K3V9Q2R/5?X-Amz-…", "expires_in": 900 },
  "job_id": "job_01JB7K3VB0XE"
}
```

If `external_ref` already exists, the same call registers a **new version** of the existing
document rather than a new document. That is what the connectors use: they do not need to know
whether a file is new.

**Why `sha256` is sent up front.** If the hash matches the live version, the API returns `200` with
`"status": "UNCHANGED"` and no upload URL. Connectors re-sync whole folders nightly, and most files
have not changed; this check turns those re-syncs into a database lookup instead of a parse, embed
and index cycle. At Kestrel it removes about 40% of connector traffic before it costs anything.

**`effective_date` is not the upload date.** A policy uploaded on 21 October that takes effect on
1 November must not be the answer to "what is the per-diem today?" until 1 November. Lodestar keeps
both dates and filters on the one the user means (§3, and doc
[07](07-retrieval-reranking-and-context.md) §2).

### Replace a specific version, safely

When a person (rather than a connector) uploads a new version, they usually mean "replace the
version I was looking at". If someone else uploaded a newer version in the meantime, silently
replacing theirs would lose their change. `If-Match` makes that explicit:

```http
POST /v1/documents/doc_01JB7K3V9Q2R/versions
If-Match: "4"
Idempotency-Key: ui-7c2e9b1d
```

If the live version is no longer 4, the response is `412 Precondition Failed` with the current
version, and the UI asks the user what to do.

### Read a document's status

```http
GET /v1/documents/doc_01JB7K3V9Q2R
```

```json
{
  "doc_id": "doc_01JB7K3V9Q2R",
  "title": "Travel and Expense Policy",
  "status": "ACTIVE",
  "live_version":    { "seq": 4, "live_since": "2026-06-02T11:40:12Z", "chunks": 31 },
  "pending_version": { "seq": 5, "stage": "EMBEDDING", "uploaded_at": "2026-10-21T09:14:09Z" },
  "visibility": "org_wide",
  "strict_mode_class": false,
  "locator_kind": "page"
}
```

`status` is one of:

| Status | Meaning | Can it be cited? |
|---|---|---|
| `ACTIVE` | Live version is current | Yes, the live version only |
| `BLACKOUT` | A new version was uploaded more than 270 s ago and is not ready yet; the old version is hidden (doc 09 §3) | No. Answers that would have used it carry a notice |
| `DELETED` | Deleted | No |

### Delete a document, and get proof

```http
DELETE /v1/documents/doc_01JB7K3V9Q2R
Idempotency-Key: legal-ret-2026-10-21-0911
```

```http
HTTP/1.1 202 Accepted

{ "deletion_seq": 88123417, "status": "COMMITTED", "committed_at": "2026-10-21T10:02:00.412Z" }
```

`202 Accepted`, not `200 OK`, because the work is not finished. But note what *is* finished at this
point: the ledger transaction has committed, so the document can no longer be used as evidence by
any answer (doc [02](02-architecture-and-the-ledger.md) §3). The rest is cleaning up copies.

The caller — often a retention job or a privacy-request workflow that must prove deletion — can
follow it:

```http
GET /v1/deletions/88123417
```

```json
{
  "deletion_seq": 88123417,
  "doc_id": "doc_01JB7K3V9Q2R",
  "status": "UNRETRIEVABLE",
  "committed_at": "2026-10-21T10:02:00.412Z",
  "stores": {
    "chunk_store":   { "purged_at": "2026-10-21T10:02:00.412Z", "method": "same transaction" },
    "vector_index":  { "purged_at": "2026-10-21T10:02:04.9Z",   "audited_at": "2026-10-21T10:03:01Z" },
    "keyword_index": { "purged_at": "2026-10-21T10:02:06.2Z",   "audited_at": "2026-10-21T10:03:01Z" },
    "answer_cache":  { "purged_at": "2026-10-21T10:02:01.7Z",   "audited_at": "2026-10-21T10:03:01Z" },
    "conversations": { "purged_at": "2026-10-21T10:02:31Z",     "audited_at": "2026-10-21T10:05:02Z" },
    "traces":        { "purged_at": "2026-10-21T10:02:45Z",     "audited_at": "2026-10-21T10:05:02Z" },
    "object_store":  { "purged_at": "2026-10-21T10:02:09Z" }
  },
  "erasure": { "live_disks_by": "2026-10-22T10:02:00Z", "backups_expire_by": "2026-11-25T00:00:00Z" }
}
```

The deletion statuses progress `COMMITTED` → `PURGING` → `UNRETRIEVABLE` (every store has
confirmed, and the auditor has independently checked) → `ERASED` (live-disk compaction done).
Backups are reported with the date they expire, rather than pretending they are erased; doc
[09](09-freshness-updates-and-deletion.md) §8 explains the restore rule that keeps them safe.

### Change permissions

```http
PATCH /v1/documents/doc_01JB7K3V9Q2R/acl
{ "visibility": "restricted", "groups": ["grp:finance-leadership"] }
```

This commits to the ledger immediately (and so takes effect for answers immediately), then fans
out payload updates to both indexes. Doc [13](13-access-control-security-and-privacy.md) §3 explains
why the order makes stale index permissions safe.

### Ingestion job status

```http
GET /v1/jobs/job_01JB7K3VB0XE
```

```json
{
  "job_id": "job_01JB7K3VB0XE",
  "doc_id": "doc_01JB7K3V9Q2R",
  "version_seq": 5,
  "lane": "interactive",
  "status": "RUNNING",
  "stages": [
    { "stage": "FETCH",   "status": "DONE", "ms": 412 },
    { "stage": "PARSE",   "status": "DONE", "ms": 6120, "pages": 22, "ocr_pages": 0, "parse_quality": 0.97 },
    { "stage": "CHUNK",   "status": "DONE", "ms": 380, "chunks": 33, "sentences": 611 },
    { "stage": "EMBED",   "status": "RUNNING" },
    { "stage": "INDEX",   "status": "PENDING" },
    { "stage": "COMMIT",  "status": "PENDING" }
  ],
  "warnings": [ { "code": "TABLE_SPLIT_ACROSS_PAGES", "page": 7 } ]
}
```

---

## 3. The query API

### Start a conversation and ask

```http
POST /v1/conversations
→ { "conversation_id": "cnv_01JB8Q0D4M7T" }
```

```http
POST /v1/conversations/cnv_01JB8Q0D4M7T/turns
Accept: text/event-stream
Content-Type: application/json

{
  "question": "Can I claim per-diem for a three-day trip to Madrid?",
  "filters": {
    "effective_on": "today",
    "department": ["finance", "hr"],
    "doc_type": ["policy"]
  },
  "mode": "auto",
  "client": { "surface": "web", "locale": "en-IN" }
}
```

The filters are deliberately named for what they mean:

| Filter | Meaning |
|---|---|
| `effective_on` | Only versions in effect on this date (default: today) |
| `effective_between` | Versions whose effective period overlaps a range — "what was the policy in March 2025?" |
| `uploaded_between` | When the document entered the system — useful for admins, rarely what users mean |
| `department`, `doc_type` | Exact match on ledger fields |
| `author` | A directory user ID, never free text, so "R. Sharma" cannot match three people |

`mode` is `auto` (the policy decides, including strict mode for legal, compliance and HR evidence)
or `strict` (the caller insists on quotes only). A caller cannot request *less* strictness than the
policy assigns; that would let a client bypass the control plane.

### The response stream

The response is a stream of **server-sent events**, a standard way for an HTTP response to deliver
a sequence of named messages. Here is the complete stream for the question above:

```
event: interpretation
data: {"turn_id":"trn_01JB8Q0F","standalone_question":"Can an employee claim per-diem for a three-day trip to Madrid?","language":"en","filters_applied":{"effective_on":"2026-10-21","department":["finance","hr"],"doc_type":["policy"]}}

event: claim
data: {"claim_id":"C1","text":"Yes. Employees travelling to Madrid can claim a per-diem of €85 per day.","citations":[{"ref":1,"doc_id":"doc_01JB7K3V9Q2R","version_seq":4,"title":"Travel and Expense Policy","locator":{"kind":"page","page":7},"sentence_id":"chk_7f3a:2","quote":"Employees travelling to Madrid may claim a per-diem of €85 per day."}],"notes":[]}

event: claim
data: {"claim_id":"C2","text":"For a three-day trip that is €255.","computed":{"expression":"85 × 3","result":255},"citations":[{"ref":1,"sentence_id":"chk_7f3a:2"}],"notes":[]}

event: claim
data: {"claim_id":"C3","text":"The per-diem covers meals and local transport.","citations":[{"ref":1,"sentence_id":"chk_7f3a:3"}],"notes":[{"quote":"Contractors are not eligible for per-diem allowances.","ref":1,"sentence_id":"chk_7f3a:4","reason":"condition near cited evidence"}]}

event: status
data: {"status":"VERIFIED","claims_delivered":3,"claims_removed":0}

event: done
data: {"trace_id":"7a9c1e…","timings_ms":{"first_claim":702,"complete":1281},"cache":"MISS"}
```

Points worth noticing:

- **`quote` is filled in by the system, not the model.** The model emitted `C1 [S1.2]`; the
  orchestrator mapped the prompt-local ID `S1.2` to the stable sentence key `chk_7f3a:2` and
  fetched its text from the chunk store (doc [08](08-grounded-generation-and-verification.md) §2).
- **`computed` is present only on computed claims**, and carries the arithmetic the verifier
  recomputed.
- **`notes` are verbatim source sentences**, attached when the evidence states a condition the
  claim does not (doc [08](08-grounded-generation-and-verification.md) §7).
- **There is no event carrying unverified text.** A client that renders every event it receives
  cannot display an unverified claim.

### A partial answer

When a claim fails verification it is not sent. The `status` event reports it:

```
event: status
data: {"status":"PARTIAL","claims_delivered":2,"claims_removed":1,"message":"One statement was removed because it could not be verified against the sources."}
```

The removed claim's text is **not** in the event. It is logged (with the verifier's reasons) in the
trace store for evaluation, under the same access controls as query logs (doc
[15](15-observability-slos-and-admin-dashboard.md) §6).

### An abstention

```
event: status
data: {"status":"ABSTAINED","reason":"INSUFFICIENT_EVIDENCE","message":"I couldn't find this in the documents you can access.","nearest":[{"doc_id":"doc_01JA2…","title":"Relocation Policy — India","locator":{"kind":"page","page":3}},{"doc_id":"doc_01J9X…","title":"Travel and Expense Policy","locator":{"kind":"page","page":9}}]}
```

The phrase "the documents you can access" is deliberate. When a user cannot read the document that
answers their question, Lodestar must not reveal that such a document exists — so the message is
the same whether the corpus has no answer or the user lacks access to it.

### A document excluded mid-update

```
event: notice
data: {"kind":"DOCUMENT_BEING_UPDATED","doc_id":"doc_01JB7K3V9Q2R","title":"Travel and Expense Policy","message":"Travel and Expense Policy is being updated and was not used for this answer."}
```

A notice is only sent when the user could read the document; otherwise it would leak the title.

### Non-streaming callers

Machine callers like the service-desk copilot usually want one JSON object. `Accept:
application/json` returns the same content assembled into a single response, after the stream has
completed. The semantics are identical; only the transport differs.

### Feedback

```http
POST /v1/turns/trn_01JB8Q0F/feedback
{ "rating": "down", "reason": "WRONG_DOCUMENT", "comment": "This is the old Spain policy" }
```

Feedback reasons are a fixed list (`WRONG`, `WRONG_DOCUMENT`, `OUTDATED`, `INCOMPLETE`,
`NOT_ANSWERED`, `OTHER`), because free-text-only feedback cannot be counted. Doc
[14](14-evaluation.md) §7 shows how feedback feeds the golden set.

---

## 4. Errors and limits

| Code | HTTP | When |
|---|---|---|
| `UNSUPPORTED_FORMAT` | 415 | Not one of the five formats, after content sniffing (the extension is not trusted) |
| `FILE_TOO_LARGE` | 413 | Over 50 MB or 2,000 pages |
| `PASSWORD_PROTECTED` | 422 | Encrypted PDF or DOCX; reported on the job, not at registration |
| `PARSE_FAILED` | 422 | The parser could not extract usable text; job carries the parser's reason |
| `OCR_LOW_CONFIDENCE` | — (warning) | Indexed, but excluded from strict-mode answers and flagged in the dashboard (doc 04 §4) |
| `PRECONDITION_FAILED` | 412 | `If-Match` did not match the live version |
| `RATE_LIMITED` | 429 | With `Retry-After`; machine callers have their own quota (doc 16 §5) |
| `OVERLOADED` | 503 | Load shedding; with `Retry-After` |

There is intentionally **no error for "answer could not be verified"**. That is not an error; it is
the `PARTIAL`, `EXTRACTIVE` or `ABSTAINED` outcome, delivered with `200 OK`. Treating verification
failures as errors would encourage clients to retry until they get an answer, which defeats the
point.

---

## 5. Admin APIs

| Endpoint | Purpose |
|---|---|
| `GET /v1/admin/jobs?status=FAILED&source=contracts&since=…` | Ingestion failures, by reason, with retry |
| `POST /v1/admin/jobs/{id}/retry` | Re-run from the failed stage |
| `GET /v1/admin/queries?outcome=PARTIAL&language=hi&since=…` | Query log, **users pseudonymised** |
| `POST /v1/admin/queries/{id}/reveal` | Reveal the real user for one query; requires a reason and a second approver; audited and reviewed weekly by the privacy office — never routed to the user's management chain (doc 13 §7) |
| `GET /v1/admin/freshness` | Current purge backlog, oldest unconfirmed deletion, auditor results |
| `GET /v1/admin/deletions?status=PURGING&older_than=120s` | Deletions at risk of missing the SLA |

Doc [15](15-observability-slos-and-admin-dashboard.md) describes the dashboard built on these.

---

## 6. Identifiers, and why each is shaped the way it is

| ID | Shape | Why |
|---|---|---|
| `doc_id` | `doc_` + ULID | A ULID sorts by creation time, which keeps index insertion local in PostgreSQL B-trees |
| `external_ref` | Source URI, unique | Lets connectors register by source location without knowing the `doc_id` |
| `version_id` | `doc_id` + sequence number | Monotonic per document; the committer's conditional flip compares sequence numbers |
| `chunk_id` | UUIDv5 of `version_id:ordinal` | **Deterministic**, so re-running a crashed ingestion produces the same IDs and the index upserts overwrite rather than duplicate |
| Sentence key | `chunk_id:ordinal` | Stable reference for citations, conversation history and evaluation labels |
| Prompt sentence ID | `S{chunk position}.{sentence}`, e.g. `S2.4` | Local to one request; about 3 tokens instead of ~20 for a UUID (doc 08 §2) |

The last two rows are the same sentence named twice. The model sees `S2.4` because it is short and
cannot be confused with content. Everything outside the request — the API, the conversation store,
the evaluation labels — uses `chk_7f3a:4`, which means the same thing next week. The orchestrator
holds the mapping between them for the duration of one request and discards it afterwards.

---

## 7. The relational schema

PostgreSQL holds the ledger, the chunk store, ingestion jobs, deletion tracking, conversations and
the query log. The DDL below is trimmed to the columns that matter for behaviour.

### Documents, versions, permissions

```sql
CREATE TYPE doc_status     AS ENUM ('active', 'blackout', 'deleted');
CREATE TYPE version_status AS ENUM ('awaiting_upload', 'ingesting', 'ready', 'live',
                                    'superseded', 'failed');
CREATE TYPE visibility     AS ENUM ('org_wide', 'department', 'restricted');

CREATE TABLE documents (
  doc_id            text PRIMARY KEY,
  external_ref      text NOT NULL UNIQUE,
  title             text NOT NULL,
  department        text NOT NULL,
  doc_type          text NOT NULL,
  author_id         text,
  visibility        visibility NOT NULL,
  strict_class      boolean NOT NULL DEFAULT false,   -- set by answer policy, not by uploader
  status            doc_status NOT NULL DEFAULT 'active',
  live_version_id   text,                             -- NULL until the first version goes live
  requested_seq     integer NOT NULL DEFAULT 0,       -- newest version anyone has asked for
  pending_since     timestamptz,                      -- upload time of the newest unflipped version
  acl_epoch         bigint NOT NULL DEFAULT 0,        -- bumped on every permission change
  created_at        timestamptz NOT NULL DEFAULT now(),
  deleted_at        timestamptz
);

CREATE TABLE doc_versions (
  version_id        text PRIMARY KEY,                 -- doc_id || '@' || seq
  doc_id            text NOT NULL REFERENCES documents,
  seq               integer NOT NULL,
  status            version_status NOT NULL,
  sha256            bytea NOT NULL,
  object_key        text NOT NULL,
  page_count        integer,
  locator_kind      text NOT NULL,                    -- 'page' or 'section'
  primary_language  text,
  locale            text,                             -- 'es-ES' vs 'es-MX' matters (doc 11 §6)
  parse_quality     real,
  effective_from    date,
  effective_to      date,
  uploaded_at       timestamptz NOT NULL,
  live_at           timestamptz,
  superseded_at     timestamptz,
  UNIQUE (doc_id, seq)
);

CREATE TABLE doc_acl (
  doc_id            text NOT NULL REFERENCES documents,
  principal_id      text NOT NULL,                    -- 'grp:legal', 'usr:…'
  PRIMARY KEY (doc_id, principal_id)
);
CREATE INDEX doc_acl_by_principal ON doc_acl (principal_id);
```

Two columns carry most of the correctness argument. `live_version_id` is what the hydration gate
compares against, so changing it is the single act that makes a new version current.
`requested_seq` is what the committer compares against, so an older version that finishes
ingesting late cannot become live:

```sql
-- The flip. Succeeds only if this version is still the newest one requested.
UPDATE documents
   SET live_version_id = $version_id,
       status = CASE WHEN status = 'blackout' THEN 'active' ELSE status END,
       pending_since = NULL
 WHERE doc_id = $doc_id
   AND requested_seq = $seq
   AND status <> 'deleted';
-- 1 row: this version is live. 0 rows: a newer version was requested, or the doc was deleted.
```

### The chunk store

```sql
CREATE TABLE chunks (
  chunk_id          uuid NOT NULL,
  doc_id            text NOT NULL,
  version_id        text NOT NULL,
  ordinal           integer NOT NULL,
  text              text NOT NULL,
  sentences         int4range[] NOT NULL,             -- character spans of each sentence
  sentence_pages    integer[],                        -- page of each sentence, for paginated formats
  locator           jsonb NOT NULL,                   -- {"page":7} or {"section":"3.2","para":4}
  breadcrumb        text NOT NULL,                    -- "Travel and Expense Policy > 4 Per-diem > 4.2 Europe"
  language          text NOT NULL,
  token_count       integer NOT NULL,
  near_dup_cluster  bigint,                           -- doc 04 §9
  PRIMARY KEY (doc_id, chunk_id)
) PARTITION BY HASH (doc_id);
-- 64 partitions, each ~4.7 GB of the 302.9 GB total
CREATE INDEX chunks_by_id      ON chunks (chunk_id);
CREATE INDEX chunks_by_version ON chunks (version_id);
```

Sentences are stored as character ranges inside the chunk's text rather than as separate rows.
At 2.18 billion sentences, one row each would make this the largest table in the company for no
benefit: sentences are only ever read together with their chunk.

`sentence_pages` exists because a 400-token chunk can straddle a page break. A citation must say
page 8 for the sentence that is on page 8, even when the chunk began on page 7.

### Ingestion jobs

```sql
CREATE TABLE ingest_jobs (
  job_id      text PRIMARY KEY,
  doc_id      text NOT NULL,
  version_id  text NOT NULL,
  lane        text NOT NULL,              -- 'interactive' | 'sync' | 'bulk' | 'reembed'
  status      text NOT NULL,              -- 'queued' | 'running' | 'done' | 'failed' | 'superseded'
  stage       text,
  attempts    integer NOT NULL DEFAULT 0,
  error_code  text,
  error_detail text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ingest_jobs_failed ON ingest_jobs (error_code, created_at) WHERE status = 'failed';
```

### Deletion tracking and the outbox

```sql
CREATE TABLE deletion_log (
  deletion_seq   bigserial PRIMARY KEY,
  doc_id         text NOT NULL,
  version_ids    text[] NOT NULL,
  reason         text NOT NULL,           -- 'user' | 'retention' | 'privacy_request' | 'superseded'
  committed_at   timestamptz NOT NULL DEFAULT now(),
  unretrievable_at timestamptz,           -- set when every store has confirmed AND been audited
  erased_at      timestamptz
);

CREATE TABLE purge_confirmations (
  deletion_seq   bigint NOT NULL REFERENCES deletion_log,
  store          text NOT NULL,           -- 'vector_index' | 'keyword_index' | 'answer_cache' | …
  purged_at      timestamptz,
  audited_at     timestamptz,
  audit_result   text,                    -- 'clean' | 'found_N'
  PRIMARY KEY (deletion_seq, store)
);

-- Transactional outbox: written in the same transaction as the change it announces.
CREATE TABLE outbox (
  id            bigserial PRIMARY KEY,
  aggregate_id  text NOT NULL,            -- doc_id; also the Kafka partition key
  event_type    text NOT NULL,            -- 'version_live' | 'deleted' | 'acl_changed' | 'blackout'
  payload       jsonb NOT NULL,           -- IDs only, never document content
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz
);
```

The **transactional outbox** solves a specific problem: the delete must commit to PostgreSQL *and*
an event must reach Kafka so the purge workers act, and those are two systems that cannot share a
transaction. Writing the event into an `outbox` table in the same PostgreSQL transaction, and having
a relay process publish outbox rows to Kafka afterwards, guarantees that every committed delete is
eventually published, and no event is published for a delete that rolled back. The deletion is
effective at commit either way; the outbox only drives the cleanup.

### Conversations

```sql
CREATE TABLE conversations (
  conversation_id text PRIMARY KEY,
  user_pseudo     text NOT NULL,          -- keyed hash of the user ID; doc 13 §7
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL    -- 30 days after last activity
);

CREATE TABLE turns (
  turn_id          text PRIMARY KEY,
  conversation_id  text NOT NULL REFERENCES conversations,
  ordinal          integer NOT NULL,
  question         text NOT NULL,
  standalone_q     text NOT NULL,
  filters          jsonb NOT NULL,
  outcome          text NOT NULL,
  claims           jsonb NOT NULL,        -- [{claim_id, text, sentence_keys:[…], computed}]
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- So a deletion can find every turn whose claims came from the document.
CREATE TABLE turn_citations (
  doc_id     text NOT NULL,
  turn_id    text NOT NULL REFERENCES turns,
  version_id text NOT NULL,
  PRIMARY KEY (doc_id, turn_id)
);
```

Turns store **sentence keys**, not quote text. When history is displayed, quotes are re-hydrated
through the ledger, so a quote from a deleted document shows as `[source removed]` without any purge
having run. The claim text the model wrote is the one derived copy that does need purging, and
`turn_citations` is the index that makes that purge a lookup rather than a scan (doc
[09](09-freshness-updates-and-deletion.md) §4). `ledger.py` in the reference implementation tests
exactly this behaviour.

---

## 8. The index schemas

### Vector index: one point per chunk

```json
{
  "id": "7f3a1c2e-…",
  "vector": "<1,024 int8 values; float32 original kept on disk for re-scoring>",
  "payload": {
    "doc_id": "doc_01JB7K3V9Q2R",
    "version_seq": 4,
    "department": "finance",
    "doc_type": "policy",
    "author_id": "usr_8f21c0",
    "effective_from": 20250401,
    "effective_to": 99991231,
    "uploaded_at": 1749000012,
    "language": "en",
    "visibility": "org_wide",
    "acl": [],
    "near_dup_cluster": 551203
  }
}
```

There is **no text in the payload**. Each filter field has a payload index (doc
[06](06-indexing-at-120m-chunks.md) §6), and `acl` is populated only for restricted documents — for
the 92% that are organisation-wide or department-scoped, visibility and department are enough.

### Keyword index

```json
{
  "settings": { "number_of_shards": 8, "number_of_replicas": 2, "refresh_interval": "1s" },
  "mappings": {
    "_source": { "includes": ["chunk_id", "doc_id", "version_seq"] },
    "properties": {
      "chunk_id":    { "type": "keyword" },
      "doc_id":      { "type": "keyword" },
      "version_seq": { "type": "integer" },
      "text_en":     { "type": "text", "analyzer": "english_kestrel" },
      "text_es":     { "type": "text", "analyzer": "spanish_kestrel" },
      "text_hi":     { "type": "text", "analyzer": "hindi_kestrel" },
      "text_hi_rom": { "type": "text", "analyzer": "romanized_hindi" },
      "codes":       { "type": "keyword" },
      "breadcrumb":  { "type": "text", "analyzer": "standard" },
      "department":  { "type": "keyword" },
      "doc_type":    { "type": "keyword" },
      "effective_from": { "type": "integer" },
      "effective_to":   { "type": "integer" },
      "visibility":  { "type": "keyword" },
      "acl":         { "type": "keyword" }
    }
  }
}
```

Each chunk's text goes into the field for its language, so each gets the right tokenizer and
stemmer. Hindi chunks also get a romanized copy so that a query typed as "chhutti" can match "छुट्टी"
(doc [11](11-multilingual-en-hi-es.md) §4). `codes` holds identifiers extracted at ingestion —
`FIN-POL-017`, `Form 16`, `ISO 45001` — as exact keywords, because analyzers would split them into
meaningless fragments. `_source` keeps only IDs: the index can match text, but cannot return it.

### Redis keys

| Key | Value | TTL |
|---|---|---|
| `ans:{sha256(canonical_q, filters, permission_fp, lang)}` | Answer, plus the `(doc_id, version_seq)` set it cites | 24 h |
| `ans_by_doc:{doc_id}` | Set of answer keys citing this document | 24 h |
| `perm:{user_id}` | The user's resolved principal set and its fingerprint | 5 min |
| `rl:{caller}:{window}` | Rate-limit counters | window |

`perm:` has a 5-minute TTL because that is the revocation SLA (doc
[13](13-access-control-security-and-privacy.md) §4). The answer cache's 24-hour TTL is a storage
bound, not a correctness mechanism — correctness comes from validating every hit against the ledger
(doc [12](12-latency-budget-and-caching.md) §6).

---

## What to take away

1. **Make the guarantee visible in the API.** Every citation names a version; every answer has a
   named outcome; every deletion has a per-store proof. If a fact is not in the API, nobody
   downstream can rely on it.
2. **Never stream unverified text.** If a client can receive it, a client will render it.
3. **Separate the two dates.** `effective_on` and `uploaded_between` answer different questions,
   and most users mean the first.
4. **Deterministic chunk IDs make ingestion safe to retry.** The same version always produces the
   same IDs, so a re-run overwrites instead of duplicating.
5. **Give the model short, request-local sentence IDs and keep stable keys everywhere else.**
   `S2.4` costs 3 tokens; `chk_7f3a:4` survives next week.
6. **Store references, not copies.** Turns hold sentence keys and indexes hold IDs, so most of a
   deletion's work is done by the hydration gate rather than by purge jobs.
7. **A deletion is `202 Accepted` with a proof you can poll.** It is effective at commit; the proof
   shows when every copy is gone.
8. **Verification outcomes are not errors.** Delivering `PARTIAL` or `ABSTAINED` with `200 OK`
   prevents clients from retrying until they get an answer they like.

Continue to [04 — Parsing and chunking](04-parsing-and-chunking.md).
