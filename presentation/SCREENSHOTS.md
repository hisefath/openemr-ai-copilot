# Screenshots the deck expects

Drop these three PNGs into `presentation/assets/`. Until a file exists, that slide shows a dashed
placeholder naming the file and describing the shot — so the deck is presentable right now, and each
screenshot you add just replaces a placeholder.

Filenames must match exactly.

| File | Slide | What to capture |
|---|---|---|
| `assets/panel-boxes.png` | 4 — *The model says what. The page says where.* | Co-Pilot panel after attaching `intake_full.pdf`: extracted values listed, **bounding boxes drawn over the matching text** on the page image. The boxes are the point — frame so several are visible at once. |
| `assets/panel-unlocated.png` | 5 — *The row that proves the design* | `lab_degraded.pdf` attached, showing the one value that came back **without** a box, reading *“extracted, could not be located on the page.”* This is the most important shot in the deck. |
| `assets/panel-answer.png` | 7 — *A supervisor you can audit* | An answered question: cited lines, guideline evidence under its own heading, and the **routing/handoff flags** visible on the response. |

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
