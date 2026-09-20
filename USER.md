# USER.md — the user, in brief

**Why this file and [USERS.md](USERS.md) both exist:** the assignment names both. The Stage 4 hard gate asks for
`./USERS.md` ("a markdown document (./USERS.md) defining your target user, their workflow, and specific use cases"),
while the Submission Requirements table asks for `./USER.md`. Rather than guess which one is graded, the repository
ships both: **USERS.md is the source of truth** (full persona, workflow, six use cases with "why an agent", and the
traceability table every capability in [ARCHITECTURE.md](ARCHITECTURE.md) points back to). This file is the short
version, so it stands on its own if it is the one you opened.

## The user

**Dr. Maya Chen, outpatient primary care physician** at a small multi-provider clinic running OpenEMR.

- ~2,000 adult patients, mostly chronic disease; 20 visits a day, 15–20 minutes each.
- **~90 seconds between rooms**, usually standing, laptop already open to the next chart.
- **Near-zero tolerance for invented facts** (a hallucinated allergy can harm a patient); moderate tolerance for
  omissions *if the agent says what it couldn't check*.
- Past ~10 seconds she walks into the room without the answer.
- She is not the only one in the EHR: a rooming nurse and front-desk staff have narrower permissions, so the agent
  must know who is asking.

**Not the user:** patients, billing staff, administrators. The agent is **read-only** — no diagnosis, no prescribing,
no writing to the chart.

## Use cases

| UC | She asks | Why an agent, not a screen | What it justifies building |
|---|---|---|---|
| UC1 | "Brief me on this patient." | The chart shows everything with equal weight; she needs the few lines that matter today, each proven | Selection and ranking |
| UC2 | "Is it safe to start amoxicillin?" | Joins allergies, medications and labs through clinical rules, from a question phrased in context | Deterministic rules engine |
| UC3 | "What changed since her last visit?" | A cross-record, cross-time diff no single chart view shows | Dated records, "recorded on" |
| UC4 | "Show me the trend on that creatinine." | Her second question depends on the first answer | **Multi-turn conversation** |
| UC5 | "Flag anything on today's schedule." | Schedule → each patient → labs, meds, allergies → rules | **Tool chaining** |
| UC6 | Another patient's name, or injected text in the chart | Free text is a new path to PHI; the agent must hold the boundary OpenEMR holds | Patient lock, refusals, audit |

Full detail — workflow timings, what "done" looks like per use case, what the agent must refuse, and the
capability-to-use-case traceability table — is in **[USERS.md](USERS.md)**.
