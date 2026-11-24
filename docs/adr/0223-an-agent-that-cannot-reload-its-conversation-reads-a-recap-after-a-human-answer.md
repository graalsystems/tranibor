# 0223 — An agent that cannot reload its conversation reads a recap of its own work after a person's answer

- **Status**: proposed, 2026-11-24
- **Concerns**: `tranibor_core/recap.py`, `tranibor_runner/runner.py` (`_open_session`),
  `activities/answer_recap.py`, `activities/suspension.py` (`resume_run`), `activities/stage.py`,
  `activities/tracker.py` (`close_human_request`); builds on [ADR 0016](0016-agent-sandbox-executor.md)
  (suspension across a question), S35-03 (`session/load`) and [ADR 0219](0219-a-classifier-service-may-read-third-party-text-beside-the-patterns.md)
  (the guard on third-party text). No contract changes.

## Context

A buyer's review: "the human loop does not give the agent back its context after the wait." Checked in
the code:

- `agent_sandbox` suspends the sandbox across a question; `resume_run` reopens the same run, puts the
  answer at the head of the playbook, and the runner's `_open_session` reloads the saved conversation
  **only if** the agent announces ACP `loadSession`. Otherwise it journals `session/load.unsupported`
  and opens a new conversation, in the kept workspace, with the answer and nothing of what the agent had
  done. The same happens when a reload is refused (`session/load.failed`).
- The agent SDK (`tranibor_agent_sdk`) announces `load_session=False`. No other backend is bundled with
  a promise either way: it is the agent's own announcement, read at run time (`can_load_session`).
- An executor that cannot suspend (`k8s_job`, Tekton, a custom-image second executor) replays the stage
  in a new run and a new workspace: no conversation exists to reload, and the agent read the answer only.
- The answer reached the agent through the guard when a stage started, but not when `resume_run`
  reopened a suspended run; and it did not say who answered or when.

## Decision

1. **A recap, built deterministically from the journal, no model call.** `tranibor_core.recap.build_recap`
   reads a run's `session/update` events and writes, in order: where the agent was (stage, task), its
   last plan, its last three messages (chunks joined), the question of its last `ask_human` call and the
   names and statuses of its last ten tool calls. The same journal gives the same text.
2. **Bounded.** Sections have their own room (plan 1,500 characters, a message 700, a call title 120,
   the question 600) and the whole never exceeds `MAX_CHARS = 8,000`. Only the last 4 MB of a transcript
   are read; the orchestrator reads the last 3,000 events of a run.
3. **Handled as third-party text.** The agent's messages may quote a ticket, so: secrets are masked
   (`redaction.redact`); an excerpt the lexical injection patterns flag is replaced by a stated
   withholding, **whatever the policy's mode** (the rule of the decision step: replayed text is never
   worth the risk) and counted in the journal; every excerpt is flattened and quoted line by line so
   that it cannot forge a heading; tool inputs and outputs are left out (only the `question` argument of
   `ask_human` is read); the recap says it is data, not instructions.
4. **Who adds it, and when.**
   - *Resumed run, kept workspace* (any executor that resumes: `agent_sandbox`, the fake): the runner
     builds it from the workspace's `transcript.jsonl` **only when a saved session exists and the
     conversation was not reloaded** (unsupported, failed, or another backend's). It goes before the
     prompt and the journal records `session/recap` (sections, sizes, withheld count, never the text).
     When `session/load` works there is no recap: the agent already holds that history.
   - *Stage replayed after an answer* (a new run): there is nothing to reload, so the orchestrator builds
     the recap from the journal of the latest earlier run of the work item on the same transition and
     prefixes the run's prompt, after the playbook reference (a checksum of the playbook) is taken.
5. **The answer says who and when.** `close_human_request` keeps `documents.human_answer_by`
   (`Answered by <who> at <time>`) beside `human_answer`; both doors render them under one heading, and
   the outcome activity drops both when the step is done. `resume_run` now runs the answer through
   `guard_sources` **before** anything is resumed (`block` leaves the run asleep, `warn` records the
   event), as `prepare_stage` already did for the replay door.

## Consequences

- Nothing in `workflows/` or the contracts changes: no replayed history is needed; the activities keep
  their names and payloads.
- The recap is not part of `context.parts` (registered before the session opens), so the gateway counts
  it as what the backend added.
- The recap is **extractive**: an agent that announced no plan and said little gets a short one, and what
  a tool returned is not recalled. Summaries by a model are out of scope, because the recap must be
  reproducible, cheap and guarded; a later decision may add an optional one.
- A replayed stage recalls the journal, not the files: its workspace is new.
