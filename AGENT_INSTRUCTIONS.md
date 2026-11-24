# Paste this into the Codex or Claude TUI once per goal

Mission control already sees your agents' commands, edits, jobs and plan without their help.
This one instruction makes their *reasons* visible too, so you can skim decisions instead of
reading reasoning traces. Paste the block below into the TUI of each goal session
(or add it to the project's AGENTS.md / CLAUDE.md so every new session follows it).

---

From now on, whenever you make a decision that changes the approach, append one line to
`DECISIONS.md` in the project root, in this exact format:

`- YYYY-MM-DD HH:MM | <agent or sub-agent name> | DECISION: <what changed> | WHY: <one sentence> | SCIENCE: <effect on accuracy or validity, or "none"> | CLAUSE: <contract clause or amendment it relies on, or "needs approval">`

Write a line when you: change any calculation setting or method; move work to a different
cluster, queue or node; cancel or resubmit jobs; skip, defer or replace a validation step;
change what counts as "done"; or start or stop a sub-agent. Never change a scientific setting
marked "needs approval" until I confirm in this TUI. Keep each line under 300 characters.

Whenever you evaluate a model, run the real test (for DeePMD: `dp test` on held-out systems that
are not in training_data) and keep its full output: either let it print in your command output
or save it with `| tee <name>_test.log` next to the model. Never quote an accuracy number you
did not just measure, and say which systems and how many frames it was tested on.

Also call update_plan (Codex) or keep your todo list current (Claude) whenever a step starts
or finishes, so the plan on my dashboard stays accurate.

---

Mission control reads the last lines of `DECISIONS.md` on every cycle and shows them in the
"What happened" timeline with a purple *decision* tag.
