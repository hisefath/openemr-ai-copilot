# REGRESSION_MR.md — the eval gate blocking a regression

Evidence for `EVAL_GATE.md` §5 and Derek's 2026-09-23 request:

> *"A link to a merge request in your repo where you introduced a regression and the pipeline blocked it.
> The blocked merge request matters most. It's how we see your gate actually fire without running it
> ourselves."*

| | |
|---|---|
| **Branch** | `regression/eval-gate-demo` @ `6e63a4d` — one commit off `main` |
| **Pipeline** | [#30850](https://labs.gauntletai.com/sefathchowdhury/openemr-agentforge/-/pipelines/30850) — **failed** |
| **Blocking job** | `agent:gate`, stage `gate`, failed in 46 s |
| **Merge request** | _to be opened — see "A note on the MR" below_ |

---

## The regression

One line of the vision extraction prompt, `agent/copilot/extract.py`:

```diff
- "Copy each value EXACTLY as printed — keep '<0.01', 'negative', 'trace', and never round or convert.\n"
+ "Copy each value as printed, rounding to two decimal places where that reads more cleanly.\n"
```

It is chosen to be the most realistic regression an LLM application can suffer. It touches no logic, adds
no dependency, breaks no type, and reads in review like a small readability improvement. There is nothing
about it that looks wrong.

It would corrupt every extracted lab value. `<0.01` is a real result, and rounding it either invents
precision the source never had or drops the inequality entirely — the difference between "undetectable" and
"0.01". The same edit turns `5.4` and `5.44` into the same number.

---

## What the pipeline did

```
(success) • 00m 06s   test     cases:wellformed
(success) • 00m 47s   test     agent:tests             ← unit tests PASS
(success) • 03m 09s   test     agent:ships-standalone
(failed)  • 00m 46s   gate     agent:gate              ← the eval gate blocks it
                      deploy   agent:deploy            ← never runs
                      deploy   alerts:deploy           ← never runs
                      verify   agent:verify            ← never runs

Pipeline state: failed
```

**The unit tests pass.** That is deliberate and it is the point: this regression is invisible to type
checking and to every assertion in 408 tests. Only the eval gate catches it.

From the `agent:gate` job log:

```
cache miss: AD-03: model-facing surface changed on call #1 (recorded 590c94c618a1e64c, now bac1bd81a188e1a2)
cache miss: CT-01: model-facing surface changed on call #1 (recorded 590c94c618a1e64c, now bac1bd81a188e1a2)
cache miss: EX-01: model-facing surface changed on call #1 (recorded 590c94c618a1e64c, now bac1bd81a188e1a2)
… 15 cases in total across AD, CT and EX

GATE FAILED
ERROR: Job failed: exit code 1
```

### Why it fails

Recordings are keyed on a hash of the **model-facing surface** — model, system prompt, tools, `output_config`
— never on the case content. Editing the prompt changes that hash, so every recorded response for those
cases is no longer evidence of what the model would now do. **A cache miss is a hard failure, never a silent
pass.** You cannot quietly change a prompt and keep a green build.

Deployment is stopped by stage ordering, not by a flag: `agent:gate` is in the `gate` stage and both deploy
jobs are in `deploy`, so GitLab will not start them while the gate is red.

---

## What building this demo exposed

Worth stating, because it is the most useful thing in this file.

**The first attempt at this exact regression passed.** All 55 cases, all eight rubrics at 1.000.

The extraction instruction was being appended as a trailing *user text block*, and `replay.py` deliberately
excludes `messages` from the surface hash — message content is the per-case payload and is supposed to vary.
So the instruction sat outside the hash. Editing it left every recording valid and the gate reported green,
while the prompt guarding the Week 2 core feature had been rewritten to corrupt its output.

`EVAL_GATE.md` had claimed *"edit a prompt and every case is a cache miss."* That was true of `llm.py`'s
`SYSTEM_PROMPT` and false of the one that mattered most.

Fixed in **`5034644`** by moving the instruction into `system`, where the existing hash already reaches — no
change to `replay.py` needed, and `system` is the correct place for an instruction constant across every
document. A test now asserts the instruction is in `system` and not in `messages`, with the reason written
down, so the gate cannot be blinded the same way again.

**A second iteration was needed too.** That new test initially pinned a sentence from the prompt, so the
regression branch failed `agent:tests` and `agent:gate` was *skipped* — the pipeline blocked, but the gate
never fired, which is weaker evidence and worse test design. Pinning prompt wording in a unit test
duplicates what the surface hash does properly and pre-empts it. Loosened in **`84b4b45`** to assert
placement rather than wording. Wording is the gate's job.

Three iterations, and the middle one found a hole that had been in the gate all week.

---

## A note on the MR

Merge requests are currently **disabled** on this project:

```
$ glab api projects/sefathchowdhury%2Fopenemr-agentforge
id 2024 | merge_requests_enabled: False | project_access.access_level: 10
```

The branch is pushed and its pipeline has already run and failed, so the evidence above stands on its own —
pipeline [#30850](https://labs.gauntletai.com/sefathchowdhury/openemr-agentforge/-/pipelines/30850) is
public to anyone who can see the project. Once merge requests are enabled, the MR opens from:

```
https://labs.gauntletai.com/sefathchowdhury/openemr-agentforge/-/merge_requests/new?merge_request%5Bsource_branch%5D=regression%2Feval-gate-demo
```

and will show the same failed pipeline attached, blocking the merge.

---

## Reproducing it locally

```bash
git checkout regression/eval-gate-demo
python evals/w2/run_gate.py ; echo "exit=$?"     # exit=1, 15 cache misses
cd agent && python -m pytest -q tests            # 408 passed — the tests do not catch it
```
