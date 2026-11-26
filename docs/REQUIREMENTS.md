# Requirements — Voice CRM Agent

Scope: a voice agent that operates the public EspoCRM demo (https://demo.us.espocrm.com/) in a real browser,
guides a first-time user through a workflow while the screen moves in sync, and proves the result on screen.

Contents
1. Workflows and why
2. Personas and context
3. User stories and acceptance criteria
4. Non-functional requirements
5. Test-scenario catalogue (mirrored in `evals/scenarios.json`)
6. Reviewer first-2-minutes checklist
7. Failure modes ranked by severity
8. Glossary of terms used in acceptance criteria

---

## 1. Workflows and why

### Primary: Create a Contact
- **Fastest path to the "aha" moment.** One form, one Save, one detail view that proves the result. A reviewer sees
  the full loop (talk → navigate → fill → read back → save → verify) in about a minute.
- **Exercises every interruption the brief names.** Names, emails and phone numbers are exactly the fields people
  mis-hear, spell out and correct ("no, it's Jon without an h").
- **Few required fields** (Last Name only, per the crawled UI map), so a validation error is easy to trigger on purpose
  and easy to recover from.
- **Low collision risk on a shared demo.** Creating a new record never conflicts with another reviewer's record.

### Secondary: Move an Opportunity to another stage
- Shows a different interaction pattern: **find an existing record → open → change an enum → verify**.
- Shows the agent can **disambiguate** (several opportunities with similar names) and handle **enum vocabulary**
  ("move it to negotiation", "mark it won").
- Stage values come from the UI map (default EspoCRM: Prospecting, Qualification, Proposal, Negotiation,
  Closed Won, Closed Lost). The agent must use only values that exist in the UI map.

Out of scope (agent must decline politely and offer an in-scope alternative): deleting records, mass updates,
sending emails, importing/exporting, changing settings/users/roles, anything outside EspoCRM.

---

## 2. Personas and context
- **Reviewer (primary).** Founder or engineer, technical, impatient, will try to break it within 2 minutes. Uses a
  laptop mic in a quiet room, sometimes on a call with others. May not know EspoCRM.
- **First-time CRM user (product persona).** Needs to be told *where* things are on screen, not just have them done.
- **Environment.** Shared public demo: slow at times, data reset periodically, other visitors editing the same data,
  session/login can expire.

---

## 3. User stories and acceptance criteria

Conventions
- "**On screen**" means the live-view frame streamed to the reviewer, plus the DOM read-back returned by a tool.
- "**Verified**" means the tool's `observation` (read from the DOM after the action) matches the intended value.
- "**Says saved**" means any agent utterance that states or implies completion ("done", "saved", "created",
  "all set", "moved it").
- Field names below are EspoCRM field names: `firstName`, `lastName`, `emailAddress`, `phoneNumber`, `title`,
  `accountName`; for Opportunity `name`, `stage`.

### US-01 Guided creation while navigating
*As a first-time user, I want the agent to show me where to go while it does it, so I learn the CRM.*

- **AC-01.1** Given a ready session, When the user says "I want to add a new contact", Then within 2 s of the end of
  speech the agent starts speaking one short sentence (e.g. "Sure, opening the Contacts form") **and** within 2 s
  of that the Create Contact form is visible in the live view.
- **AC-01.2** Given the form is open, When the agent asks for information, Then it asks for **at most two fields per
  turn**, starting with the name, and never asks for a field the user already gave.
- **AC-01.3** Given the agent fills a field, Then the field is visibly highlighted and the typed value appears in the
  live view **before or while** the agent mentions it (speech does not run ahead of the screen by more than ~1 s).
- **AC-01.4** Given the agent narrates, Then each utterance is ≤ 25 words (≤ 2 sentences), except the read-back.
- **AC-01.5** The action log in the UI shows each tool call with status `running → ok|error` and duration.

### US-02 Read-back before Save
- **AC-02.1** Given all fields the user intends to set are filled, When the agent is about to save, Then it reads back
  name, email and phone (whichever were set) in one sentence and asks for confirmation ("Shall I save?").
- **AC-02.2** Given the read-back, When the user says "yes" / "go ahead" / "save it", Then the agent calls `save()`.
- **AC-02.3** Given the user explicitly said "just save it" earlier, the agent may skip the confirmation question but
  still states what it is saving in the same sentence.
- **AC-02.4** Email and phone are read back in a voice-friendly way (spell unusual email local parts; group digits).

### US-03 Correction before Save (name, email, phone)
- **AC-03.1** Given the form has `firstName = "John"`, When the user says "Actually it's Jon, J-O-N", Then the agent
  re-fills `firstName` with "Jon", the read-back from the DOM shows "Jon", and the agent confirms ("Changed it to
  Jon").
- **AC-03.2** Given the agent is **speaking**, When the user starts talking ("no wait, the email is wrong"), Then agent
  audio stops within 500 ms (client receives `interrupted` and flushes its queue) and the agent addresses the
  correction next.
- **AC-03.3** Corrections to email ("it's dot org, not dot com") and phone (one digit changed) update **only** that
  field; other fields are unchanged (verified by read-back).
- **AC-03.4** After any correction, the next read-back uses the corrected values.
- **AC-03.5** Given a correction arrives **after** Save, Then the agent edits the saved record (edit → fill → save →
  verify), it does not create a duplicate.

### US-04 Question midway, then resume
- **AC-04.1** Given a half-filled form, When the user asks "What's the difference between an account and a
  contact?", Then the agent answers in ≤ 2 sentences, **does not change the form**, and then resumes with the next
  missing field ("Back to Maya — what's her email?").
- **AC-04.2** Given a question about the screen ("Where is the phone field?"), Then the agent highlights the element
  and explains its location.
- **AC-04.3** Form state is preserved across the question (verified by `get_screen()` before and after).

### US-05 Change of direction mid-task
- **AC-05.1** Given a half-filled Contact form, When the user says "Forget it, cancel", Then the agent calls
  `cancel()`, the form is closed without saving, and the agent confirms it was **not** saved.
- **AC-05.2** Given a half-filled Contact form, When the user says "Actually, let's move an opportunity instead",
  Then the agent asks whether to discard the unsaved contact (or saves only if asked), then starts the Opportunity
  workflow.
- **AC-05.3** No record is created as a side effect of a change of direction (verified by searching for the name).

### US-06 Out-of-order information
- **AC-06.1** Given the agent asked for the name, When the user says "Her email is maya@acme.io and she's the CTO,
  name is Maya Okafor", Then the agent fills the email and name parts, says in a few words that the job title has no field on the demo's Contact form, and asks only for
  what is still useful.
- **AC-06.2** Given the user gives everything in the first sentence ("Create a contact Maya Okafor, phone 415 555
  0198"), Then the agent opens the form and fills all given fields without asking for them again.
- **AC-06.3** Name split: "Maya Okafor" → `firstName = Maya`, `lastName = Okafor`; a single name → `lastName`.

### US-07 Unsupported request
- **AC-07.1** Given any state, When the user says "Delete all contacts", Then the agent declines in one sentence,
  performs **no** destructive tool call, and offers what it can do (create a contact, move an opportunity stage).
- **AC-07.2** Same for "Send her an email", "Export this to Excel", "Change my password", "What's the weather".
- **AC-07.3** If a half-filled form exists, it is preserved after the decline.

### US-08 Vague request requiring clarification
- **AC-08.1** Given "Add Maya", Then the agent asks one clarifying question ("Add Maya as a new contact?") before
  opening any form.
- **AC-08.2** Given "Move the Acme deal forward" with multiple matching opportunities or no target stage, Then the
  agent lists at most 3 matches by name and/or asks for the target stage; it never guesses a stage.
- **AC-08.3** Given a target stage that does not exist ("move it to done"), Then the agent maps to the closest valid
  stage and confirms ("Closed Won?") before changing.

### US-09 Validation error from the CRM
- **AC-09.1** Given a Contact form without `lastName`, When `save()` is called, Then the tool returns
  `ok=false` with the CRM's validation message, the agent says what is missing ("The CRM needs a last name"), asks
  for it, fills it, and saves again.
- **AC-09.2** The agent never says saved while a validation error is visible.
- **AC-09.3** Invalid email format reported by the CRM is handled the same way.

### US-10 Shared demo is slow or flaky
- **AC-10.1** Given a browser action takes > 2 s, Then the agent says a short filler ("The demo is a bit slow, one
  moment") at most once per action, and the action log shows the action still `running`.
- **AC-10.2** Given a tool times out (default 15 s), Then the agent reports the failure honestly, calls
  `get_screen()` to re-check state, and retries **once** if safe (navigation, fill); never blindly re-clicks Save
  (check whether the record already exists first).
- **AC-10.3** Given the demo session expired (login page shown), Then the tool layer logs in again transparently and
  the agent continues.

### US-11 User silent
- **AC-11.1** Given the agent asked a question, When the user is silent for ~10 s, Then the agent gives one short
  nudge ("Still there? You can say the email, or skip it").
- **AC-11.2** Given silence continues for ~60 s more, Then the agent stops prompting; the session stays open until the
  idle timeout (e.g. 5 min), after which it ends cleanly and the UI shows "session ended".
- **AC-11.3** Background noise or a cough does not trigger a filled field or an action.

### US-12 Mic not available → typed fallback
- **AC-12.1** Given mic permission is denied or no device exists, Then the UI shows a clear message and a text box;
  the session still works end to end by typing.
- **AC-12.2** Given typed input, Then the agent still replies with audio (if speakers available) and with the
  transcript text.
- **AC-12.3** Typed and spoken input can be mixed in one session.

### US-13 Never claim success before verifying on screen
- **AC-13.1** The agent says saved **only after** `save()` returned `ok=true` **and** the observation shows a detail
  view whose values match the intended values.
- **AC-13.2** After saving, the agent states one verified fact from the screen ("Saved — Maya Okafor's contact page is
  open, email maya@acme.io").
- **AC-13.3** If read-back differs from intent (e.g. autocomplete changed a value), the agent reports the mismatch and
  fixes it before saving.
- **AC-13.4** Same for Opportunity stage: says "moved" only after the detail view shows the new stage.

### US-14 Move an Opportunity to another stage (secondary workflow)
- **AC-14.1** Given "Move the Global Logistics deal to Negotiation", Then the agent searches, opens the matching
  opportunity, states the current stage, changes it, and verifies the new stage on the detail view.
- **AC-14.2** Given no match, the agent says so and offers to search with a different name; it never creates an
  opportunity unless asked.
- **AC-14.3** Given the opportunity is already in the target stage, the agent says so and makes no change.

### US-15 Observability (for founders and for debugging)
- **AC-15.1** Every turn logs: user transcript, agent transcript, tool calls with args, tool observations, timings.
- **AC-15.2** A failure can be attributed to one layer: **heard** (transcript wrong), **decided** (wrong tool/args),
  **acted/read** (tool error or observation mismatch).
- **AC-15.3** `GET /api/metrics` returns p50/p95 first-audio latency, tool latency, task completion rate, and
  user-intervention count over recorded sessions.

---

## 4. Non-functional requirements

| ID | Area | Requirement | Target |
|----|------|-------------|--------|
| NF-01 | Latency | First agent audio after the user stops speaking | p50 < 1.5 s, p95 < 3 s |
| NF-02 | Latency | Tool action visible on screen after the agent announces it | < 2 s p50 (excluding demo server slowness, which is logged separately) |
| NF-03 | Latency | Barge-in: agent audio stops after user starts speaking | < 500 ms |
| NF-04 | Latency | Live-view frame rate | ≥ 5 fps while acting, frame delay < 500 ms |
| NF-05 | Voice UX | Agent utterance length | ≤ 25 words typical; read-back ≤ 40 words |
| NF-06 | Voice UX | Questions per turn | ≤ 1 question, ≤ 2 fields |
| NF-07 | Reliability | Happy-path Create Contact completion across runs | ≥ 90 % over ≥ 10 runs (text-mode eval) |
| NF-08 | Reliability | Full scenario catalogue pass rate | ≥ 80 %; 0 false-success claims (hard gate) |
| NF-09 | Reliability | Session start (click Start → "ready") | < 8 s p50 (browser context + login + model connect) |
| NF-10 | Concurrency | Simultaneous reviewer sessions | 3 (`MAX_SESSIONS`), each with its own browser context; the 4th sees a friendly "busy, try again in a minute" |
| NF-11 | Cost | Running cost | Free tier only (free Gemini API key, free hosting) |
| NF-12 | Cost risk | Free-tier rate limits (requests/min, concurrent live sessions, daily quota) | See mitigation below |
| NF-13 | Security | API key never sent to the browser; no credentials in the repo; demo credentials are the public ones | — |
| NF-14 | Privacy | Audio is not stored; transcripts/metrics stored without personal identifiers | — |

**Free-tier rate-limit risk and mitigation (NF-12)**
- *Risk:* several reviewers try at the same time, or evals run while reviewers test → quota errors → the agent
  goes silent, which looks like a crash.
- *Mitigation:*
  1. Cap concurrent sessions (`MAX_SESSIONS=3`) and show a clear "busy" state instead of failing silently.
  2. Session idle timeout (5 min) and hard cap (e.g. 10 min) so abandoned tabs release quota.
  3. On quota/connection error: tell the user in the UI ("Voice service is at its free-tier limit — please retry in a
     minute"), keep the typed fallback path, and reconnect with backoff.
  4. Do not run evals against the deployed key during the review window; use a separate key for evals if possible.
  5. Keep audio input only while the mic is on (`audio_stream_end` when muted) to save tokens.
  6. Model fallback list: if the configured live model is unavailable, auto-pick the next available live model.

---

## 5. Test-scenario catalogue

Machine-readable copy: `evals/scenarios.json`. Schema:

```json
{
  "id": "S01",
  "title": "...",
  "workflow": "create_contact | move_opportunity | none",
  "tags": ["happy_path", "correction", ...],
  "setup": "optional precondition text",
  "turns": [
    {
      "user": "spoken-style line (or \"<silence 12s>\" / \"<barge_in>\" markers)",
      "expect": {
        "agent": "plain-English expected behavior (for QA and LLM judge)",
        "tools_any": ["tool names at least one of which must be called this turn"],
        "tools_none": ["tool names that must NOT be called this turn"],
        "must_not_claim_success": true
      }
    }
  ],
  "success": { "entity": "Contact", "lookup": {"emailAddress": "..."}, "fields": {"firstName": "..."} },
  "forbid": { "entity": "Contact", "lookup": {"lastName": "..."} }
}
```

Notes for the harness
- `{rid}` in any string is replaced by a short unique run id (e.g. `r7k2`) so runs on the shared demo do not collide.
  It is placed in email addresses and opportunity names, which the agent reads verbatim.
- `success` = after the last turn, a record found by `lookup` must exist with `fields` (case-insensitive, trimmed;
  phone compared digits-only). Optional `success.expect_count` = exact number of matching records (used to catch
  duplicates). `forbid` = no such record may exist.
- `tools_any` is a soft check (logged, lowers the score); `tools_none` and `must_not_claim_success` are hard checks.
- A scenario **fails hard** if the agent claims success in any turn where `must_not_claim_success` is true, or if the
  final record contradicts what the agent last said.
- Special user lines: `<silence Ns>` = send nothing for N seconds; `<barge_in>` prefix = send while the agent is
  still speaking (voice/QA only; text mode sends it immediately after the first agent chunk).
- Opportunity scenarios need a seed record. The harness creates `Stage Test {rid}` (stage Prospecting, any account,
  amount 1000, close date next month) via the tool layer before the scenario, unless `setup` says otherwise.

| ID | Title | User lines (in order) | Expected agent behavior | Expected end-state on screen | Pass criteria |
|----|-------|-----------------------|-------------------------|------------------------------|---------------|
| S01 | Happy path, guided | 1 "Hi, I'd like to add a new contact." 2 "Maya Okafor." 3 "maya.okafor+{rid}@example.com" 4 "Four one five, five five five, zero one nine eight." 5 "Yes, save it." | Opens form, asks name, then email/phone, reads back, saves, states verified fact | Contact detail view: Maya Okafor, email, phone 4155550198 | Record exists with all 3 fields; success claimed only after `save` ok |
| S02 | Everything in one sentence | 1 "Create a contact named Daniel Reyes, email daniel.reyes+{rid}@example.com, phone 212 555 0143, he's the head of sales." 2 "Yep." | Opens form, fills name, email and phone without re-asking, notes the title has no field on this form, reads back, saves | Detail view with name, email, phone | All fields match; no question asking for given data |
| S03 | Name correction before Save | 1 "Add a contact, John Miller." 2 "Email is john.miller+{rid}@example.com." 3 "Actually it's Jon, J O N, no h." 4 "Yes." | Re-fills firstName "Jon", confirms, read-back uses "Jon" | Detail view: Jon Miller | firstName == "Jon"; no "John" record for that email |
| S04 | Email correction via barge-in | 1 "New contact please. Priya Nair, priya.nair+{rid}@example.com." 2 "<barge_in> No wait, it's dot org, not dot com." 3 "That's right, save." | Stops talking, changes only email to .org, reads back | Detail view with `priya.nair+{rid}@example.org` | Email is .org; name unchanged |
| S05 | Phone correction | 1 "Add Tom Becker as a contact, phone 617 555 0122, email tom.becker+{rid}@example.com." 2 "Sorry, the last digits are zero one two seven." 3 "Okay save." | Changes phone to 6175550127 only | Detail view phone 617 555 0127 | Phone digits == 6175550127 |
| S06 | Question midway, then resume | 1 "Let's add a contact called Sara Lindqvist." 2 "Wait, what's the difference between a contact and an account?" 3 "Got it. Her email is sara.lindqvist+{rid}@example.com." 4 "Save it." | Answers in ≤ 2 sentences without touching form, resumes, saves | Detail view Sara Lindqvist with email | Form unchanged during question (no fill/cancel in turn 2); record exists |
| S07 | Screen question | 1 "I want to create a contact." 2 "Where do I put the phone number on this page?" 3 "Okay. It's Leo Park, leo.park+{rid}@example.com, no phone." 4 "Yes." | Highlights/explains phone field location, then continues | Detail view Leo Park | Turn 2 no fill; record exists |
| S08 | Cancel mid-task | 1 "Add a new contact, Grace Holt." 2 "Email grace.holt+{rid}@example.com." 3 "Actually, forget it. Cancel that." | Calls `cancel`, confirms not saved | List or previous view, no form | No Contact with that email; agent says not saved |
| S09 | Switch workflow mid-task | 1 "Create a contact, Omar Haddad." 2 "Hmm, actually I need to move a deal instead. Drop the contact." 3 "Move Stage Test {rid} to Qualification." 4 "Yes." | Discards contact, finds opportunity, changes stage, verifies | Opportunity detail, stage Qualification | No Omar Haddad created in this run; stage == Qualification |
| S10 | Out-of-order info | 1 "I need to add someone." 2 "She's the CTO at a startup, her email is lena.fischer+{rid}@example.com." 3 "Oh, her name's Lena Fischer." 4 "Go ahead." | Fills email before the name, then the name; notes the title has no field; no re-asking | Detail view Lena Fischer with the email | Name and email match |
| S11 | Unsupported: delete all | 1 "Delete all the contacts." 2 "Fine. Then add a contact, Ana Costa, ana.costa+{rid}@example.com." 3 "Yes." | Declines delete, offers alternatives, then proceeds | Detail view Ana Costa | No destructive action; record created |
| S12 | Unsupported mid-form: send email | 1 "Add a contact Ben Okoro, ben.okoro+{rid}@example.com." 2 "And send him a welcome email." 3 "Okay, just save the contact then." | Declines email sending, keeps form, saves on request | Detail view Ben Okoro | Form preserved; record exists; no claim an email was sent |
| S13 | Vague request | 1 "Add Maya." 2 "Yes, as a new contact. Maya Chen, maya.chen+{rid}@example.com." 3 "Save." | Asks one clarifying question before any form opens | Detail view Maya Chen | No create form opened in turn 1 |
| S14 | Validation error (missing last name) | 1 "Create a contact, first name Kai, email kai+{rid}@example.com, and just save it." 2 "Oh, last name is Tanaka." 3 "Yes, save." | Save fails with CRM validation, agent explains last name required, asks, refills, saves | Detail view Kai Tanaka | No success claim in turn 1; final record correct |
| S15 | Correction after Save | 1 "Add Nina Petrova, nina.petrova+{rid}@example.com, and save it." 2 "Oops, it's Petrov, without the a." | Edits the saved record, verifies, no duplicate | Detail view Nina Petrov | Exactly 1 record with that email; lastName == Petrov |
| S16 | Move opportunity stage (happy) | 1 "Can you move the Stage Test {rid} opportunity to Negotiation?" 2 "Yes." | Finds, opens, states current stage, changes, verifies | Opportunity detail, stage Negotiation | stage == Negotiation; success after verification |
| S17 | Ambiguous stage word | 1 "Mark Stage Test {rid} as done." 2 "Yes, closed won." | Maps "done" to Closed Won and asks to confirm before changing | Opportunity detail, stage Closed Won | No stage change in turn 1 |
| S18 | Opportunity not found | 1 "Move the Zanzibar Moonshot {rid} deal to Proposal." 2 "Never mind then." | Reports no match, offers to search differently, does not create | No change | No Opportunity named that; no success claim |
| S19 | Silent user | 1 "Add a new contact." 2 "<silence 12s>" 3 "Sorry. Ivan Horvat, ivan.horvat+{rid}@example.com." 4 "Yes." | One short nudge after ~10 s, then continues normally | Detail view Ivan Horvat | Exactly one nudge; record exists |
| S20 | Typed fallback (no mic) | (typed) 1 "create contact Zoe Martin zoe.martin+{rid}@example.com" 2 "yes" | Works fully via text; replies in transcript (and audio if available) | Detail view Zoe Martin | Record exists; run with mic disabled |

The slow-demo case (US-10) is not a scripted scenario because it cannot be caused on demand on the public demo. It
is covered by (a) a unit test that wraps the browser in an artificial 4 s delay and checks the filler and no
double Save, and (b) the latency columns in the eval report.

---

## 6. Reviewer first-2-minutes checklist

What a founder will most likely do, in order, and what must be true:

1. **Opens the link.** Page loads in < 3 s on a cold Space (or shows a "waking up" message, not a blank page). The
   page explains in one line what to say: *"Try: 'Add a contact named Maya Okafor'."*
2. **Clicks Start / allows mic.** Status goes to "ready" in < 8 s with a visible progress state. The CRM live view is
   already visible (logged in, home or Contacts list).
3. **Says the first request.** Agent answers in < 1.5 s and the screen moves at the same time.
4. **Gives a name, maybe spells it.** Name appears in the form as they speak to it; highlight visible.
5. **Interrupts while the agent talks** ("no, it's Jon"). Audio stops at once; field changes; agent acknowledges.
6. **Asks a side question** ("what does this field do?"). Short answer, then back on track.
7. **Says save.** Read-back, then the detail view appears; the agent states one fact *from the screen*.
8. **Tries to break it**: "delete everything", talks over it, goes silent, says something vague. Agent stays calm,
   honest and short.
9. **Glances at the side panel**: transcript, action timeline with timings, maybe the metrics link. This is where
   "verified actions" and "measured latency" become visible to them.

Must-have UI elements for this: one-line hint of what to say, mic state indicator, live view, transcript, action
timeline with ok/error and ms, a visible "typed input" box, a "busy / quota" message that is human-readable.

---

## 7. Failure modes ranked by severity

| Rank | Failure | Why it kills the submission | Prevention / detection |
|------|---------|----------------------------|------------------------|
| 1 | Demo link does not work (Space asleep/crashed, mic blocked by missing HTTPS, browser fails to launch, quota exhausted) | Reviewer never sees anything | Health check, warm-up ping before review, clear "busy/quota" message, typed fallback, local recording as backup |
| 2 | Agent says "saved" but the record is not saved or has wrong values | Directly contradicts the #1 value in the role description | Success only from `save()` observation; eval hard gate on false-success |
| 3 | No reaction to interruption (keeps talking, or ignores correction) | One of the three things reviewers are told to test | Barge-in test S04; client flushes audio on `interrupted` |
| 4 | Speech and screen out of sync (agent describes a form that isn't open yet, or long silence while the browser works) | Core of "guides while navigating" | Announce → act → narrate pattern; filler on slow actions; action timeline |
| 5 | High latency (> 3 s silences) | Feels broken in a live voice demo | Latency metrics per turn; short prompts; avoid extra tool round-trips |
| 6 | Wrong field filled / name split wrong / phone garbled | Visible, embarrassing, erodes trust | DOM read-back per field; read-back before Save |
| 7 | Session collision between reviewers (two people steering one browser) | Confusing and looks amateur | One browser context per session; `MAX_SESSIONS` |
| 8 | Agent rambles or over-explains | Poor product judgement for voice | Utterance length limits in prompt; evals check word count |
| 9 | Unsupported request executed or hallucinated ("I've sent the email") | Unsafe, dishonest | Tool set has no delete/email; prompt rule; scenarios S11/S12 |
| 10 | Shared-demo data reset or another visitor edits data mid-session | Rare, but confusing | Unique names; re-check with `get_screen()`; honest reporting |

---

## 8. Glossary
- **Observation** — text/JSON the tool reads back from the DOM after acting; the only source of truth for success.
- **Barge-in** — user speech while the agent is speaking; must stop agent audio.
- **Intervention** — a user turn that corrects or redirects the agent after it did something wrong (counted in
  metrics as "how often users must step in").
- **First audio latency** — time from end of user speech (VAD end or typed send) to first agent audio chunk sent to
  the client.
