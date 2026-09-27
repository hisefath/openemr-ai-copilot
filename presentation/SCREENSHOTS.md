# Screenshots the deck expects

Three of the five are captured. Drop the rest into `presentation/assets/`. Until a file exists, that slide shows a dashed
placeholder naming the file and describing the shot — so the deck is presentable right now, and each
screenshot you add just replaces a placeholder.

Filenames must match exactly.

| File | Status | Slide | What to capture |
|---|---|---|---|
| `assets/panel-boxes.png` | **captured** (re-shoot advised) | index 4 · final 3 | Intake form attached, 4 of 4 values located, review queue with Approve/Reject. **The page render itself was 401ing when this was shot** — fixed in `documents.js`/`panel.js`, so re-shoot after a deploy to get the actual bounding boxes on screen. |
| `assets/panel-answer.png` | **captured** | index 7 · final 5 | Answered question: deterministic HIGH drug–allergy conflict, per-line citations, `not in rule set, not checked`, and the chart-resident prompt injection rendered as data. |
| `assets/panel-consent.png` | **captured** | index 20 · final 10 | OpenEMR's consent screen for the confidential client — every requested scope enumerated and revocable, `Api:oemr: True` under Identity Information. |
| `assets/panel-unlocated.png` | **still needed** | index 5 | `lab_degraded.pdf` attached with the dropdown set to **Lab PDF** (not Intake form), showing the value that came back **without** a box: *"extracted, could not be located on the page."* This is the most important shot in the deck. |
| `assets/dashboard.png` | **still needed** | index 21 | The React dashboard: identity bar plus all six cards — Allergies, Problem List, Medications, Prescriptions, Care Team, Vitals. Launch at `/dashboard/launch` from the chart. |
| `assets/panel-wrongtype.png` | spare | — | A lab PDF read with the *intake* schema: `0 of 0 values located`, `Nothing left to review`. Not wired into either deck; correct behaviour, but not the un-located-row point. |

Both demo PDFs are already on your Desktop (`intake_full.pdf`, `lab_degraded.pdf`). If you need to
regenerate them:

```bash
cd ~/Desktop/gauntlet_workbench/openemr-ai-copilot && docker run --rm -v "$PWD":/repo -w /repo agentforge-agent-w2 python -c "
import sys; sys.path.insert(0,'/repo/evals/w2')
import fixtures
for n in ('intake_full','lab_degraded'): open(f'/repo/{n}.pdf','wb').write(fixtures.get(n)); print('wrote', n)
" && mv intake_full.pdf lab_degraded.pdf ~/Desktop/
```

## Capture tips

- **Crop to the panel**, not the whole browser. Chrome, tabs and the OS menu bar add nothing and
  shrink the part that matters.
- **Retina capture** (`Cmd+Shift+4`, then drag) is already 2×; no need to upscale.
- Keep all three at a **similar width** so the deck doesn't jump between slides.
- Nothing here is real patient data — it's the synthetic demo set — but avoid catching the OpenEMR
  admin username in frame out of habit.

## Before you record

```bash
cd ~/Desktop/gauntlet_workbench/openemr-ai-copilot && KEY=$(grep '^ANTHROPIC_API_KEY=' agent/.env | cut -d= -f2-) && VOY=$(grep '^VOYAGE_API_KEY=' agent/.env | cut -d= -f2-) && docker run --rm -v "$PWD":/repo -w /repo -e ANTHROPIC_API_KEY="$KEY" -e VOYAGE_API_KEY="$VOY" agentforge-agent-w2 python tools/live_smoke.py
```

Expect `4/4 live paths accepted`. This is the check that would have caught the `output_config` bug —
the one the deck's climax is about — so running it before recording is both a real precaution and a
nice thing to be able to say you did.
