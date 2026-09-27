# Screenshots the deck expects

What each deck shows, and where it came from. A slide whose image file is missing degrades to a labelled
placeholder rather than a broken-image icon, so the deck always presents — but as of now nothing is missing.

Filenames must match exactly.

| File | Status | Where |
|---|---|---|
| `assets/panel-extract.png` | captured | index 4 · final 3 — top crop: `4 of 4 values located on the page` |
| `assets/panel-review.png` | captured | index 4 · final 3 — bottom crop: the review queue with Approve / Reject |
| `assets/panel-answer.png` | captured | index 7 · final 5 — the answered question, incl. the chart-resident injection |
| `assets/panel-consent.png` | captured | index 20 · final 12 — OpenEMR's scope consent screen |
| `assets/panel-boxes.png` | superseded | the uncropped original; kept, not referenced |
| `assets/panel-wrongtype.png` | spare | a lab PDF read with the intake schema: `0 of 0 values located` |

**Neither deck has a placeholder left — both are presentable and recordable as they stand.**

Two slides make their point with a drawn figure instead of a screenshot, which is a deliberate choice
rather than a gap: index 5 (the un-located row) renders the ambiguous lab line with both occurrences of
`5.1` marked, which shows *why* the value cannot be boxed in a way a screenshot never could; and index 21
shows the dashboard's identity bar and six cards.

`panel-extract` / `panel-review` are two crops of one capture. The band between them held the page
render, which was failing at capture time — an `<img src>` cannot send an Authorization header, so the
endpoint answered 401, and the CSP `default-src 'self'` would then have blocked the blob: fallback too.
Both are fixed in the working tree but not yet deployed, so the band is cut rather than shown.

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
