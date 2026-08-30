# Accenture Hackthon:
## Topic is patient triage.ai, this will be an assistance to the nurse in sorting of the patients in OT

---

## Running the whole system

Everything runs on one machine over loopback. No hosting, no public URLs.

```
8080  intake      Express      form -> Mistral -> initial schema
8000  grounding   FastAPI      ground + re-triage + queue feed
5173  queue UI    Vite         nurse-facing queue
```

`main` has all three modules and the wiring. A pull is not quite enough,
though — five things are deliberately **not** in git and have to be created
once on whichever laptop is running the demo:

```bash
git clone <repo> && cd Patient_triage

npm install                            # 1. intake server deps
cd retriage-demo && npm install && cd ..   # 2. queue UI deps

python -m venv .venv                   # 3. python 3.12 (3.14 lacks some wheels)
.venv/Scripts/python.exe -m pip install -r requirements.txt

python scripts/fetch_corpus.py         # 4. the protocol PDFs

# 5. the Mistral key, in a .env at the repo root or its parent:
#    mistral_api=<key>
```

Then:

```bash
python scripts/run_all.py
```

That starts all three, waits for each port, and opens the intake form and the
queue UI. Fill the form; the patient appears in the queue within five seconds.
`--no-open` skips the browser, `python scripts/run_all.py grounding` runs one
service alone, Ctrl-C stops everything.

Why those five are not committed: the key must never be in the repo; the
corpus is third-party clinical PDFs whose provenance you should confirm
yourself (see `scripts/fetch_corpus.py`); `node_modules` and `.venv` are build
output.

### Working on your own module

You do not need to run the whole system to work on your part. Your branch
holds only your own files, which is fine while you are heads-down — but it
means your module has no one to talk to. When you want the integrated thing,
`git merge main` into your branch, or just run it from `main` on the one
machine doing the demo.

---

## The grounding module (`grounding_module/`)

Takes the initial assessment produced upstream — a reception form turned into a
schema by an LLM — and returns a triage judgement in which **every clinical
claim is either computed deterministically or quoted from a protocol document
with a page number a nurse can turn to**.

```python
from grounding_module import ground
import json

result = ground(json.load(open("schema_initial_example.json")))
result["grounded_esi"]                 # 1 (most urgent) .. 5
result["concerns"][0]["evidence"]      # verbatim quotes + printed page numbers
result["_audit"]                       # what the verifier stripped or repaired
```

Input contract: `schema_initial_example.json`.
Output contract: `grounded_schema_example.json`, plus a `news2` block and
`_audit`.

### Setup

```bash
python -m venv .venv                      # python 3.12 (3.14 lacks some wheels)
.venv/Scripts/python.exe -m pip install -r requirements.txt
python scripts/fetch_corpus.py            # downloads the protocol PDFs
```

The Mistral key is read from `MISTRAL_API_KEY` or `mistral_api`, in the
environment or a `.env` outside the repo. It is never committed.

### How it works

```
initial schema
   ├── lookups[]  → BM25 over the corpus → printed pages → verbatim text
   ├── vitals     → news2.py (deterministic, never the model)
   └── both       → Mistral structured output → verifier → grounded JSON
```

Four design decisions that are load-bearing. Change them only knowing why they
are there:

**NEWS2 is computed in code, never asked of the model.** The observation chart
(Chart 3) is a spatial grid; PyMuPDF returns the point values and the value
bands as two unaligned runs, and Mistral OCR skips it entirely as an image.
The score therefore *cannot* be grounded by retrieval. Asked directly, the
model returned 4, then 7 with two compensating errors in the workings, for a
patient whose true score is 7. RAG handles the prose thresholds, which extract
cleanly; the arithmetic is a fixed table in `news2.py`.

**NEWS2 refuses anyone under 16.** Adult bands read normal infant physiology as
extreme — a well 3-month-old scores 10. It returns `applicable: false` with a
reason rather than a confident, meaningless number.

**Page numbers are the ones printed on the paper.** Derived per document by
`pagemap.py`, which reads each page's folio. A fixed offset does not work:
NEWS2 numbers 23 pages of front matter in roman numerals before restarting at
arabic 1, which produced citations like "page -4".

**Everything the model emits is verified against the retrieved text.**
Structured output guarantees the JSON's shape and nothing about its truth:
given a required `evidence[]` field and no evidence, the model invents
plausible page numbers. The verifier rejects citations to pages that were never
retrieved, repairs spliced quotes onto the real span, corrects page
misattribution, drops anything it cannot find, nulls time targets the citations
do not state, and flags conclusions that contradict the threshold they cite.

### Verification

```bash
.venv/Scripts/python.exe sim/run_cases.py       # six cases end to end
.venv/Scripts/python.exe sim/test_grounding.py  # adversarial grounding tests
.venv/Scripts/python.exe sim/viewer.py          # visual flow, sim/flow.html
```

The grounding tests are the ones that matter. The audit line only proves
citations *resolve*; the **counterfactual** test rewrites the thresholds in
every retrieved page and checks the output follows the documents rather than
the model's training. If that test ever fails, the system is reciting, not
grounding.

Last full run: 6/6 cases clean, 28/28 citations verbatim on the page they cite,
4/4 grounding tests passing, 3–7s per patient.

### Known gaps

- **ESI retrieval is weaker than NEWS2.** The handbook's worked vignettes
  keyword-match well and crowd out the criteria pages.
- **Table content is unreachable** by text extraction or OCR. Anything living
  only in a grid has to be encoded like the NEWS2 bands.
- **Sepsis timing is ungrounded.** "Antibiotics within 60 minutes" is correct
  practice but appears nowhere in the corpus — the word *antibiotic* is absent
  from NEWS2 entirely — so `time_to_treatment_minutes` comes back `null` for
  sepsis. Adding NICE NG51 would fix it.
- **No paediatric physiological tool.** NEWS2 correctly refuses under-16s and
  nothing replaces it; only the ESI paediatric criteria apply.
- **ESI v4 is superseded** by v5 (ENA, 2023), and the v4 PDF was sourced from a
  mirror rather than AHRQ. Confirm provenance before clinical use.
- **Six test cases** — enough to prove the mechanism, not coverage.

### Layout

| path | what it is |
|---|---|
| `grounding_module/` | the shipped module |
| `scripts/fetch_corpus.py` | downloads the protocol PDFs |
| `corpus/` | the PDFs (gitignored) |
| `sim/` | local debug tools — runner, REPL, viewer, tests (gitignored) |

---

## Other Modules

### Intake Service (`app.js` & `views/`)
- **Purpose**: The entry point for the triage workflow, replacing traditional paper forms or basic EMR text boxes.
- **Technologies**: Node.js, Express, Embedded JavaScript templating (EJS).
- **Workflow**: 
  1. A frontend form captures unstructured chief complaints, vital signs, allergies, and mechanisms of injury.
  2. Submitting the form triggers an API call to Mistral.
  3. The LLM acts as an initial parser, reading the clinical narrative and formatting it into a rigid JSON structure conforming to the strict schema (`schema_initial_example.json`).
- **Why it matters**: It normalizes messy, human-entered clinical shorthand into clean, structured data ready for deterministic gates and the semantic retrieval module.

### Nurse Queue UI (`retriage-demo/`)
- **Purpose**: The real-time situational awareness dashboard for triage nurses.
- **Technologies**: React, Vite, TypeScript, TailwindCSS.
- **Features**:
  - **Live Polling**: Automatically polls the backend (port 8000) for new patient assessments.
  - **Dynamic Retriage Timers**: Displays visual countdown timers for each patient (e.g., 15 minutes, 30 minutes, 60 minutes) depending on their assigned ESI level and physiological stability.
  - **Urgency Visualization**: Highlights ESI 1 (immediate) and ESI 2 (emergent) patients aggressively so they are never lost in a busy waiting room.
  - **Fallback Mechanics**: Smoothly degrades to static sample data if the live backend connection drops or is temporarily unreachable.

### Red-Flag Gate (`red_flag_gate.py`)
- **Purpose**: A deterministic, fail-closed safety net that intercepts patients *before* they reach the LLM Grounding Module.
- **Design Philosophy**: "No LLM hallucinations for life-threatening conditions." 
- **How it works**:
  - Takes the structured JSON and evaluates it sequentially through a gauntlet of **16 hardcoded clinical rules**.
  - **Tier 1 rules (ESI 1)**: Checks for absolute emergencies like pulselessness, apnea, severe hypoxia (SpO2 < 90), active seizures, unresponsiveness, or neonatal fever.
  - **Tier 2 rules (ESI 2)**: Checks for high-risk mechanisms (stroke signs, severe burns, airway swelling), pediatric vital sign boundaries based on precise age bands, and adult NEWS2 aggregate scores (via `grounding_module/news2.py`).
  - If any rule matches, the gate immediately returns an ESI score and hardcoded citations, completely bypassing the slower retrieval pipeline for instant escalation.
- **Failsafes**: If critical physiological fields (like age or consciousness level) are missing from the intake data, it safely "fails closed" (returns `no_match` with `low_confidence`) to ensure the patient is safely routed through the full retrieval/LLM pipeline rather than being wrongly downgraded.
