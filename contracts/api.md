# CQA JSON API v1

Stack: Python 3.11+ standard library, SQLite, HTML/CSS/JavaScript ES modules with no build step. The Python server serves `web/` and the API from the same local origin. Frontend components are separated into conversation import, results, checklist configuration and appeal review.

All requests/responses use JSON, with POST `Content-Type: application/json`. Success is HTTP 200. Errors: `{error:{code,message}}`, HTTP 422 validation/provider failure, 404 missing, 409 conflict, 403 foreign Origin, 413 size, 415 content type, 500 generic internal failure. Error messages must be shown as text. No authentication is required for this local prototype; bind is loopback only. No client score injection or test-provider HTTP mode exists.

## Routes

| Method/path | Body or query | Response |
| --- | --- | --- |
| GET `/api/health` | none | `{status:"ok",api_version:1,model_configured:boolean}` (configuration flag, not an inference readiness check) |
| POST `/api/imports` | `{source:"json",payload:<dataset>,group_id?:"JSON"}` | `{imported,conversation_ids:[]}` |
| POST `/api/imports` | `{source:"dev",group_id:"DEV"}` | same; endpoint configured server-side only |
| GET `/api/conversations` | optional `language`, `channel`, `dataset_group_id` filters | `{items:[<conversation without transcript, plus message_count>]}` |
| GET `/api/conversations/{conversation_id}` | URL-encode ID | full conversation |
| GET `/api/checklists` | none | `{items:[<saved checklist version>]}` |
| POST `/api/checklists` | checklist below; include existing `family_id` to save a new version | saved version with new `id`, same/new `family_id`, increasing `version`, `created_at` |
| GET `/api/checklists/{id}` | none | immutable saved version |
| POST `/api/evaluations` | `{conversation_id,checklist_id}` | evaluation below; synchronous model call, allow at least 120 seconds |
| GET `/api/evaluations` | optional `conversation_id` | `{items:[<evaluation>]}` |
| GET `/api/evaluations/{id}` | none | evaluation |
| POST `/api/appeals` | `{evaluation_id,criterion_id,comment}` | pending appeal |
| GET `/api/appeals` | optional `evaluation_id` | `{items:[<appeal>]}` |
| GET `/api/appeals/{id}` | none | appeal with conversation_id |
| POST `/api/appeals/{id}/review` | `{decision:"confirm",reason}` or `{decision:"change",reason,answer:<complete criterion answer>}` | `{appeal,evaluation}` |

Dataset input accepts the official `{isSuccess:true,data:{dataset_group_id,total_records,records:[]}}`, `{dataset_group_id?,records:[]}`, or a records array. Each conversation has `conversation_id`, `channel` (`agent chat`/`voice call`), `language` (`RU`/`KZ`/`EN`), `transcript`, optional `audio_url`. Transcript messages retain `timestamp`, `speaker` (`operator`/`client`/`bot`/`system`), `text`, and any source fields. Server assigns zero-based IDs `${conversation_id}:${index}` in each message's `id`. Existing IDs with different content are rejected to keep evidence stable; reimport of identical data succeeds.

Server adds `dataset_group_id` and, when parseable, `audio_offset_seconds` to messages. MM:SS becomes seconds; ISO timestamps are relative to the first transcript timestamp. This is an inferred offset, not verified alignment with recording start. Current DEV URLs return HTML player pages; the recording-resolution route below extracts media for the in-card player. UI also offers the original `audio_url` as a fallback recording link. No audio proxy or transcription is required.

## Checklist

```json
{
  "name": "демонстрационный пример, не официальный чек-лист",
  "provenance": "demonstration",
  "criteria": [
    {"id":"greeting","title":"Example operator greeting","type":"boolean","weight":1,"allow_na":true,"section":"Example","gross_error":false},
    {"id":"clarity","title":"Example clarity","type":"numeric","min":1,"max":5,"weight":3,"allow_na":true}
  ],
  "rules":{"decimals":2,"gross_error_policy":"flag"}
}
```

`provenance` is `demonstration`, `custom` or `official` (canonical preset configuration). Startup idempotently saves canonical preset `official-cqa-v1`, family `official-cqa`, from the supplied official PDF. POST claiming `official` is compared with the canonical configuration excluding storage metadata; modified content is saved as `custom`. All titles, IDs and name are nonempty. IDs unique; weights finite and nonnegative, at least one positive. Numeric `min < max`, finite. Optional `allowed_values` is an ordered unique numeric array including min/max; answers must match a listed value. Optional `scale_labels` maps stringified values to descriptions. `description` and `na_condition` carry assessment and applicability instructions. Boolean `gross_error_when` derives the flag from equality with the answer when gross_error is enabled, overriding any independent model flag. `allow_na`/`gross_error` default false. Section optional. `decimals` integer 0..6 defaults 2. Gross-error policy `flag` (only mark) or `zero_total` (global result zero). These remain configurable for custom versions. Official preset uses decimals=2, flag policy, weights 10/15/20/20/15/15/5/0, numeric 04=[0,5,10,15,20], 06=[0,4,7,10], N/A only 02/03/04/07. Criterion 08 derives gross_error from answer=true. Three-minute first response, tags and attachments are excluded. Version edits always POST a new snapshot with `family_id`; no PUT/DELETE updates.

## Model answer and score

An answer has `{criterion_id,answer,explanation,evidence_message_ids,missed?,checked_span?,na_reason?,gross_error?}`. `answer` is boolean, in-range numeric (and a configured allowed_values member), or null for allowed N/A. `explanation` nonempty. `evidence_message_ids` is an array of existing message IDs (required even if empty). An applicable answer needs evidence, or `missed:true` with `checked_span:{start_message_id,end_message_id}` naming an existing ordered range. Missing actions always require that range. N/A requires a nonempty `na_reason`. `gross_error:true` is allowed only on an enabled criterion and never on N/A. Every criterion must occur exactly once.

`result` is `{criteria,total,sections,gross_error,applicable_weight,rules,provenance}`. Each `criteria` row contains the answer plus `title,section,weight,normalized_score,score,effective_weight,weighted_score` and criterion type/scale/applicability metadata. For numeric answers normalized score = `(answer-min)/(max-min)`; boolean maps false=0/true=1. N/A normalized score and score are null, effective weight and contribution zero. Effective weight is original weight / sum of applicable weights, in 0..1. Score and total are 0..100; weighted_score is the row's contribution to the global result. All N/A or zero applicable weight means total null, never invented zero. Sections are `[{name,total,applicable_weight}]`, locally renormalized; absent section uses empty name. Gross-error `zero_total` affects the global total only, with section diagnostic scores retained.

Round displayed points and final total with decimal HALF_UP at `rules.decimals`; total uses unrounded normalized contributions, so rounded row contributions need not sum exactly to the rounded total. No intermediate rounding. The official PDF specifies two decimal places but does not define ties; HALF_UP is the explicit application choice for exact halves.

## Evaluation and appeals

Evaluation is `{id,conversation_id,checklist_id,created_at,original:{answers,result},final:{answers,result}}`. Original is never changed by any route. The initial final equals original. Evaluations remain pinned to their checklist version and imported transcript. A new evaluation makes a new ID.

New evaluations additionally snapshot `provider:{type:"ollama",model,prompt_revision,think}` (`think` mirrors `CQA_THINK`; the thinking trace itself is never parsed or stored). This metadata survives model configuration changes and appeals; it contains no endpoint URL or reasoning. Injected test providers are explicitly labelled `{type:"injected"}`. Legacy evaluations can omit `provider`; absence means unknown historical configuration, not the current server model. The metadata identifies execution settings, not semantic acceptance.

Appeal is `{id,evaluation_id,conversation_id,criterion_id,comment,status:"pending",created_at,disputed_answer,original_result}`. `disputed_answer` snapshots the current answer when filed. Only one pending appeal per evaluation/criterion. Filing never recalculates or changes a score.

Review is a separate action; reason required. `confirm` keeps the current answer and records review.answer=null. `change` validates the complete replacement answer and recalculates all weights, sections and total. A reviewed appeal adds `review:{decision,reason,reviewed_at,answer,previous_result,final_result}` and status `reviewed`. Evaluation final and appeal decision persist in one transaction; repeat review returns 409. All prior decisions remain visible in the appeals list. UI must show original, current final, disputed answer and decision history distinctly.

No batch/job endpoints at v1: UI may explicitly iterate conversation IDs sequentially, handling errors per conversation. List/detail filters are limited to the table above. Model absent returns 422 with `модель не настроена`; never substitute synthetic scores. See README for local provider configuration and limitations.

## Recording resolution

GET `/api/conversations/{conversation_id}/audio` (no body) returns `{status,media_url,media_type,page_url,message}`; 404 if the conversation is missing. It is not a proxy: the server only resolves the link, the browser plays `media_url` directly with a cross-origin `<audio src>` (no CORS needed). `status`: `none` (no `audio_url`), `playable` (`audio_url` itself is `audio/*`, or an HTML player page whose first `<audio src>`/`<audio><source src>` resolves to an absolute https URL; `media_type` from the `type` attribute or response, may be null), `page_only` (page reachable but no usable audio source; `page_url` is the original link), `error` (non-https `audio_url`, network/HTTP/timeout failure; never raised as HTTP error). Only https is fetched; redirects to other schemes are refused; 10 s timeout; at most 256 KB of HTML is read; media bodies are not downloaded. Non-error results are cached in memory for 10 minutes per URL. `message` is a Russian explanation without URLs. Signed URLs are secrets: neither server nor UI logs them. The resolution runs outside the write lock. Offsets remain inferred, not verified alignment with the recording start.

Audio resolution rejects nonpublic IP literals and DNS answers (including mixed public/private results), credentials and malformed URLs. Redirect destinations, final pages, embedded media and cached media are checked. HTTPS connects to a validated numeric IP while retaining certificate verification and SNI for the original hostname; environment proxies are disabled. Loopback HTTP is allowed only through an explicit internal test argument, never by the public API.

Model output contract (adapter-internal; `prompt_revision` is `operator-evidence-v7` for unchanged official criterion definitions and `configured-evidence-v1` for changed/custom criteria): when the transcript has operator turns the model returns `operator_evidence_ids` (enum of operator message ids; at least one for a quoted finding) and `context_evidence_ids` (client/bot/system ids). N/A-capable criteria first return `na_condition_met`, which must equal `answer === null`. `infer` validates and merges these into the stored `evidence_message_ids` (operator ids first) and drops `na_condition_met`; the stored answer shape and HTTP API are unchanged. `explanation` is limited to 700 characters.

Custom criterion definitions are authoritative: edited IDs or titles never re-enable old official behavioural rules. Exact official definitions retain the calibrated official prompt. Display-only checklist name/version changes do not change the instruction variant. Scores and all criterion definitions are pinned to the selected immutable checklist ID.

## Recording-derived transcription

GET `/api/conversations/{id}` may additionally return `recording_transcription` for a voice call. A ready value contains `{conversation_id,status:"ready",source:"recording",model,language,created_at,reviewed:false,audio_duration_seconds,segments:[{id,start,end,text}]}`. Offsets are seconds in the actual recording, not source ISO timestamps. Segments do not claim operator/client/bot speaker attribution. Local optional `tools/transcribe_calls.py` creates these records; the HTTP request does not launch recognition.

This read view never replaces `transcript`. Evaluation inputs, message IDs, source timestamps, original results and appeal evidence remain bound to the original role-tagged conversation. Evidence navigation switches the UI to that original view. Invalid recording metadata falls back to the unchanged source with a redacted status. A recording transcription is automatic and requires human checking for names, numbers and unclear speech.
