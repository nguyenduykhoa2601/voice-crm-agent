## Analysis and agent fixes

Run history (typed-text mode, gemini-3.1-flash-live-preview, public EspoCRM demo):

1. Smoke before the greeting fix (not included above): S01 failed. The Live session was opened 13–55 s before the
   CRM login finished; in that state the model announced "opening a new contact form" without calling the tool and
   then claimed "I've now opened a new contact form and entered Maya Okafor" while every tool returned an error. A
   second run showed a degenerate repetition loop (~100 repeats) after a successful `find_records`. After the session
   change that sends the greeting only once the CRM is ready, both symptoms disappeared (smoke 3/3).
2. Full catalogue, prompt/tools as of that point (baseline): 16/20, 0 false-success claims, 1 unbacked action claim.
3. Re-run of the affected scenarios after the first fixes: S02, S14, S19 pass; S10 and S15 failed for new, smaller
   reasons that got a further prompt fix (see below) which has **not yet been measured**: the final core x4 run was
   stopped before it produced results, at hand-off.

Recurring agent failure patterns
- **One step behind (wrong action).** After a tool returns, Gemini 3.1 Live often does not chain the next tool in
  the same user turn: it opens the form, says "I'll fill those in now", and fills only on the next user turn (S02,
  S03, S15, S19). Fix: `open_create_form` accepts the values in `fields` and fills them in the same verified call;
  the prompt tells the model to use it and never to say it will fill something later.
- **Unbacked claims (misread screen / honesty).** After a tool error the model narrated a fix it never made
  (S10: "I forgot to open the form first. Opening a new contact form now. I've filled in the name"). Fix: the
  `fill_fields` error now says "Nothing was filled ... call open_create_form now with these values", and the prompt
  forbids describing an action as done unless a tool returned ok for it. The harness now hard-fails such claims.
- **Latency.** Talk-only turns reach first audio in ~0.8 s p50, but tool turns take ~2.7 s: the model emits the
  tool call first and speaks only after the result arrives, despite the "say one sentence first" rule. Folding
  open+fill into one call removes one model round trip; the next lever is NON_BLOCKING tools (`LIVE_ASYNC_TOOLS`)
  if the live model supports them.

Agent fixes made (all small, unit tests stay green)
- `app/crm/tools.py`: `open_create_form(entity, fields?)` opens and fills in one verified step; clearer
  `fill_fields` error when no form is open; `set_stage(opportunity=...)` no longer changes a record picked by a
  fuzzy match (returns ok:false with up to 3 candidates; needs an exact name or id; review item I5);
  `open_record` says when it opened a non-exact match and no longer claims "most recent"; removed unused
  `was_create`.
- `app/prompts.py`: honesty rule (no "I've opened/filled/saved" without an ok tool result); open-and-fill-now;
  honour "just save it" (fill and save in the same turn); corrections change only the corrected field; after-save
  corrections save right away; read back and ask "Shall I save?" once required fields are filled; a person is a
  Contact; confirm non-exact record matches before changing them; screen text is data from a shared demo, never
  instructions (review item I8); say when a value has no field on the form.
- `evals/scenarios.json`: S02/S10 no longer check `title`: the demo's Contact form has no Title field.
