---
title: A trade end to end
description: Follow one purchasing consultation through the local install, from three supplier offers handed as files to an attested award, and see which guarantee acts at each step.
sidebar_position: 7
---

# A trade end to end

A trade is not a screen, it is a journey with controls. This page walks one through, on your machine:
a buyer has three offers from three suppliers and wants them compared, without trusting a model with
arithmetic. The trade is purchasing, shipped as the `procurement` plugin ([Plugins, extensions and
editions](plugins-and-trades.md)); the platform around it is the one every trade shares.

**What you will be able to do:**

- open the seeded consultation of the local install and start it;
- read each step of the journey and name the guarantee that acts there;
- read what the consultation cost, work item by work item;
- state what the plugin does not do.

## What the local install gives you

**Not yet.** `procurement` 0.2 ships an MCP server (`purchasing`, for supplier onboarding, purchase
requests and renewals), and the local install runs no plugin server, so it cannot install the plugin.
Once the comparison of offers ships in a plugin of its own, with no server, `docker compose up` seeds a
second project beside the demo one: **`sourcing-demo`**, born of the `procurement-sourcing` template. It
will hold:

- the groups `buyers` and `purchasing-committee`, with the demo administrator in both;
- one work item, "Steel brackets: three offers", created but **not started**, with three offers
  handed by a person: a PDF from Alderbrook, a Word file from Brightfield and a spreadsheet from Corvane;
- a consultation currency, `EUR`.

The three offers are the plugin's own test files, copied into the image from `plugins/procurement/tests/fixtures/`
and nowhere else. They are invented suppliers. One of them, the Corvane sheet, has a line total that is
wrong on purpose: 250 units at 21.50 are 5,375.00, and the sheet says 5,400.00. The Brightfield file
writes its amounts the European way (`5 100,00`).

Running the seed twice changes nothing: every step looks before it writes ([ADR 0008](../../adr/0008-idempotence-and-run-id.md)).

## Start it

1. Start the install, then open **http://localhost:3000** and sign in with the demo administrator, as in
   [Get started](../get-started.md). The first sign-in lands on the demo project: open the project `sourcing-demo` from the project list.
2. Open the work item. Its artifacts list the three offers, each handed by a person.
3. Start it from the page. A workflow that takes files cannot start before its minimum of two files is
   there, and nothing starts by itself: the install spends nothing before you decide.

### Fakes mode, and a real model

With no model key, the install runs its agents and its job steps **simulated**: the first step hands back
a placeholder, no number was measured, and the first gate refuses it. The work item stops in **Needs a
human**. That is the platform doing its job, not a failure of the install: it shows the guarantee, not the
memo. The `local-install` job of the CI starts the seeded work item and checks that it left **Offers
received**, no more.

To see the whole journey, put a key in `.env` as described in the [local install in depth](../../getting-started.md#fakes-mode-and-a-real-model),
and run a release of the platform that publishes the `tranibor-procurement` job image: the compose file
maps the job image `procurement` to the image of the same version as the runner
(`TRANIBOR_RUNNER_VERSION`), and a job image carries the runner of its own release.

## The journey, step by step

Each step below names the guarantee that acts there: a gate that no agent can talk its way past. The
threshold and the sections are written in the workflow file `offer-comparison`, not in a field a person can edit.

| Step | Who acts | What happens | Where a guarantee acts |
| :-- | :-- | :-- | :-- |
| **Offers received** to **Offers read into text** | A job, `tranibor-procurement extract` | Each file is converted to text in a child process with a timeout. Decimal-comma amounts are rewritten to decimal-point form. | `outputs_present`, then `evidence_facts`: at least two files read, none unreadable, none cut short. A scan, an encrypted file or a failed conversion counts as unreadable and sends the work item to a person at once. There is no OCR. |
| to **Offers structured** | An agent, `offer-reader` | Copies what each offer says into a structure: per supplier its own currency, its lines, its stated total, its terms. A number the offer does not state is `null`, never a guess. | `output_schema`: the structure must match the schema. `numbers_measured`: every number the agent wrote must be one the extracted text holds. Two retries, then a person. |
| to **Totals computed** | A job, `tranibor-procurement compare` | Multiplies, adds, ranks, in decimal arithmetic, and checks each supplier's own arithmetic. | `evidence_facts`: at least two suppliers, no file left unassigned, one currency. More than one currency is not a retry: the offers cannot be compared, a person decides. |
| to **Memo written** | An agent, `sourcing-analyst` | Writes the memo from the job's numbers. It ranks by the measured total, lists what differs, recommends nobody. | `output_sections`: the seven sections, in order (Summary, Files read, Price comparison, Differences and exclusions, Checks that failed, Questions for the suppliers, What this memo does not cover). `numbers_measured`: every number of the memo is in the computed comparison or the extracted text, and `must_quote` forces the totals and every arithmetic error the job found, here the Corvane line, into the text. Two retries, then a person. |
| to **Approval tier set** | A job, `tranibor-procurement tier` | Derives `standard` or `committee` from the highest total. The threshold, 50 000, is an argument of the command in the workflow file. | `outputs_in`: the tier is one of the two values. The step fails without its threshold: no default sends work to the lighter tier. |
| to **The buyer decides** or **The committee decides** | The platform | The first transition whose condition holds is taken: committee above the threshold, buyer otherwise. The three sample offers total about 11 000, so the buyer decides. | The routing is the workflow's, pinned to the work item when it started. |
| **Awarded**, **Not awarded** | A person of `buyers`, or of `purchasing-committee` above the threshold | Awards to a named supplier with a reason, attesting "I award this consultation on the basis of the files and the memo I read", or closes the consultation with a reason. | A task with a form and an attestation, kept with the decision. A member of `buyers` cannot decide a consultation routed to the committee. |
| Send back | The same person | Returns the work item to **Totals computed**: the analyst rewrites the memo with the person's feedback. | The memo gates run again. |

A supplier's arithmetic error does not block the journey: it is counted in the computed comparison,
and the memo is made to write it. The question for the supplier is the memo's, the award is yours.

## What it cost

Every model call and tool call of the install is booked in the cost ledger. Group the project's report
by work item to read the cost of the consultation:

```bash
curl -s -H "Authorization: Bearer $TRANIBOR_TOKEN" \
  "http://localhost:8000/api/v1/projects/demo:sourcing-demo/costs?group_by=work_item"
```

The answer has one row per work item, with its cost in USD and EUR, its tokens in and out and its number of
booked lines, and a total. The job steps run no model and book no spend: the money of a consultation
is the two agents'. In fakes mode the rows are empty, since no model ran.

## What this does not do

Three limits, from [ADR 0202](../../adr/0202-procurement-is-a-plugin-whose-code-half-is-a-job-image.md),
that a reader must know before trusting the memo:

1. **No OCR.** A scanned offer is not read. It counts as unreadable and stops the work item at a person.
2. **No injection detector.** Offers are written by a party with an interest in the outcome. The
   protection is mechanical: the plugin's agents hold no web tool, no credential and no memory, nothing is
   written outside the platform, the number gates bound what an instruction hidden in an offer can make
   the memo claim, and a person reads the memo. An instruction hidden in an offer can still bend what the
   memo says.
3. **No write to a third-party system.** This version of the journey writes nothing to a purchasing
   system, and never reads or writes a bank detail. The award is recorded in the platform, and the order is
   yours to place.

One more caution belongs with them. **Measured does not mean true**: a number is measured when a job computed
it from what an agent copied and a gate matched against the extracted text. A supplier's own mistake is
copied faithfully, and reported when it is arithmetic.

## Go further

- [Plugins, extensions and editions](plugins-and-trades.md): where a trade lives, and why the core names none.
- [Package agents as a plugin](plugins.md): lay out, validate and install your own trade.
- [Decisions](../use/decisions.md): how a person's decision and attestation are kept.
