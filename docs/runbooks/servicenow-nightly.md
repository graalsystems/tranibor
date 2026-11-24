# The ServiceNow nightly on a deployment

## What it is for

Plan workstream P1 asks for fourteen green nights in a row on a real ServiceNow instance (S80-20, S80-46). The
nightly is not a CI job: the instance's credentials would have to leave the platform for that. It runs **on a
deployment**, with the platform's own scheduler and secret storage:

- a project with an internal tracker (`servicenow-nightly` on the first deployment that ran it) whose
  `schedules:` opens one work item a night, `cron: "0 3 * * *"` (UTC);
- its workflow, [`examples/servicenow-nightly/workflow.yaml`](../../examples/servicenow-nightly/workflow.yaml):
  one `job` step (ADR 0056) in the runner image, which reaches the instance only through the organisation's
  `servicenow` connector with the run's token (`search_records`, `tracker_create_item`, `tracker_fetch_item`);
- the connector of the `servicenow` plugin, configured as its procedure says: the integration user's password
  (or the OAuth client secret) is sealed in the organisation's store and never leaves it;
- a triage project created from the plugin's template (`itsm-incident-triage`), which the procedure's
  Business Rule tells of the incident as soon as it is filed (its echo guard must let an insert by the
  integration user through:
  `current.operation() == 'insert' || gs.getUserName() != 'choregos.integration'`), and whose queue poll
  picks it up anyway if the delivery is lost. The job cannot tell the two apart: the triage project's work
  item says it (`initiated_by: tracker:plugin:…` or `tracker:sync`).

Each night the job files an incident in the Service Desk queue, then reads it back every 45 s for at most
30 minutes, and is **green** when the platform's write-back is on the incident: its status note (the poll opened
a work item), `Triage to review` in that note (the agent's run ended), the state `In Progress` and the review's
approval request. It is **red** otherwise, or when any call fails.

The governed apply step (a person settles the review, another approves the write) is not played at night: it
needs two people signed in. Each night therefore leaves one work item waiting for its review in the triage
project and one incident `In Progress` on the instance; settle or ignore them.

## How to know it is this

The nightly's work item, in the nightly project, ends in **Red** (`declined`) instead of **Green**. Read it in the
console (the project's work items, key `<PREFIX>-nightly-<yyyymmddThhmm>Z`) or through the API:

```bash
GET /api/v1/projects/{id}/work-items          # the night's item: state green or red
GET /api/v1/work-items/{id}                   # current_run, failure
```

The run's result says why: its `summary` (`GREEN: …` or `RED: …`) and its measured facts (`incident`,
`s_opened`, `s_triaged`, `s_in_progress`, `s_review_asked`, seconds after the job started).

| `RED:` says | It means |
| :-- | :-- |
| `search_records answered …` / `tracker_create_item answered …` with an HTML or connection error | the instance is hibernating or gone (below), or its credentials changed |
| `tracker_create_item answered 202` or no `key` | `tracker_create_item` is no longer `allowed` for the nightly project's group: it was proposed for approval |
| `no Service Desk group on the instance` | the instance was reset or replaced |
| `reached only [...]` | the incident was filed, the triage side did not answer in 30 minutes: read the triage project's work item for that incident (its run, the poll, the model budget) |

## A Personal Developer Instance

A PDI hibernates when it is not used and is reclaimed after some days without use. Whether REST traffic counts
as use is not documented: the nightly and the triage project's poll call the instance every night and every
minute, and that is **not known to keep it awake**. Hibernating, the instance answers with a page instead of
JSON and the night is red. Wake it from the developer site and run the night again.

Reclaimed, the instance's name answers nothing or belongs to someone else: every night is red. A new instance
needs the plugin's procedure again (the integration user, its role, `snc_basic_auth_api_access`), then the
connector's `base_url` and secret, and nothing else: the projects and the schedule stay.

## Commands

- Run the night now: open a work item in the nightly project with the workflow `servicenow-nightly` (the
  console, or `POST /api/v1/projects/{id}/work-items`), or set the schedule's cron a few minutes ahead and back.
- Check the write is still allowed:
  `GET /api/v1/orgs/{org}/connectors/servicenow` → operation `tracker_create_item`, `policy: allowed`,
  `groups` holding the nightly project's group.
- Rotate the instance's credential: the procedure's page on the instance, then
  `PUT /api/v1/orgs/{org}/connectors/servicenow/secrets/password`; the server's pod rolls on its own.

## How to check it is fixed

The next night, or a work item opened by hand, ends **Green** with a summary naming the incident and the four
timings.
