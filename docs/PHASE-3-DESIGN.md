# Phase 3 — Rules filter: design

**Status:** ✅ **BUILT 2026-07-14.** Brainstormed + built via superpowers (9-task TDD plan).
See **Build notes** at the end for the two execution-time refinements (dict split floor ≥3; the
`pronounce_min_score = -4.0` "mash-only" calibration + the revised survivor-volume expectation).
Parent design: `docs/TECHNICAL-DESIGN.md` §4.1 (module layout), §4.3 (rules filter), §5 (schema).

**Goal:** Deterministic, **no-network**, fully-logged tunable filter over the open `candidates` Phase 2 landed.
Classify each into a primary/secondary track, score it on dictionary commonness (`wordfreq`) and phonotactic
pronounceability (our own n-gram model), and write a track-specific pass/fail + reason + both raw scores — cutting
~thousands/day → **~50–200 survivors/day** for Phase 4. Charset/length are already permanent-gated at ingestion; Phase 3
handles only the **tunable** gates.

---

## Scope

**In scope:**
- Primary/secondary **classification** by label length.
- **Dictionary gate** (graded `zipf_frequency`, whole-word + best 2-way split).
- **Pronounceability gate** (n-gram phonotactic model, `pronounce.py`, **log-space** score).
- **Track-specific** pass/fail composition (owner-approved), with a `[primary] allow_invented` knob.
- Persist `track`, `dict_score`, `pronounce_score`, `filter_pass`, `filter_reason`, `filtered_at`.
- `filter` CLI (idempotent, `--recompute` re-filter path) + a `build-ngrams` maintenance command.
- `wordfreq` as the 3rd runtime dependency.

**Out of scope (deferred):**
- RDAP / lifecycle / drop dates → Phase 4. Filter never touches `lifecycle_status`.
- AI scoring, toxicity, comps → Phase 5. Filter never touches `tier1_score`/`tier2_scores`/…
- Emerging-vocabulary detection — `wordfreq` is frozen at 2024; micro-trend terms are Tier-2's Google-Trends job, not this gate's.

---

## Locked decisions (from this brainstorm)

1. **Track-specific gating.** Primary (label ≤ `primary_max_length` = 8) vs secondary (9–12).
2. **`[primary] allow_invented` knob, default `true`.** Length-only classification would send short invented brandables
   (`zylo`, `quivo`) to the primary track, whose dictionary-only rule rejects them — a dead zone (invented ≤8 chars is the
   one category *both* tracks' logic would drop). With `allow_invented=true`, primary mirrors secondary's OR-logic; set it
   `false` for dictionary-purist mode. Deliberate + tunable, not an emergent side effect of `classify`.
3. **Dictionary segmentation = whole-word + best 2-way split, `min`-combine.**
   `dict_score = max(zipf(label), max over split points of min(zipf(left), zipf(right)))`; the winning segmentation
   (`red+fox`) is recorded in `filter_reason`. Combine op is a `[dictionary] combine` knob (default `min` — "both parts
   must be real words").
4. **Pronounceability = mean log conditional probability (geometric mean), log space.** *Not* the arithmetic mean of raw
   probabilities: that lets one common trigram mask junk (`xqzking` free-rides on `-ing`) and is length-unstable where
   `allow_invented` makes the gate decisive (primary ≤8). Mean-log-prob penalizes any bad trigram proportionally, is
   length-consistent, and (with smoothing) stays finite — still a "whole-word average," in the right space. `score()`
   returns a **negative** value; `pronounce_min_score` is a log-space floor **set by calibration** (decided before
   calibration so the recorded value survives).
5. **Discrete score columns + `filtered_at`** (not a JSON blob, not packed into `filter_reason`) — most queryable for the
   Phase-8 tuning UI. `filtered_at` is the idempotency guard (future-proof vs later migrations; records *when*).
6. **n-gram tables store integer counts** (not probabilities): byte-deterministic in git (sorted keys, exact integers),
   smoothing math in one tested place at load, smoothing constant out of the artifact. Embedded `_meta`.
7. **No `filter_log` table** (YAGNI) — per-domain `filter_reason` + the printed run summary give auditability.
8. **No network** anywhere in Phase 3 — `wordfreq` data is local; tables are a committed package artifact.

---

## Architecture

New / changed modules (TDD §4.1 layout):

```
domainscout/
  filters.py                # Phase 3: classify · dict_score · decide · filter_candidates (DB loop)
  pronounce.py              # n-gram phonotactic scorer: build_tables · load · score (log space)
  pronounce_tables.json     # tracked PACKAGE DATA (integer counts + _meta) — NOT under gitignored data/
  db.py                     # +4 columns, idempotent migration, set_filter_result() helper   (modified)
  commands.py, __main__.py  # real `filter` + `build-ngrams` subcommands                     (modified)
  config.py, criteria.toml  # +[primary].allow_invented, +[dictionary].combine; pronounce min_score → log-space (modified)
```

### Gate pipeline — `filters.py` (pure functions + one DB loop)

- **`classify(label, criteria) -> str`** — `"primary"` if `len(label) <= criteria.primary_max_length` else `"secondary"`
  (every label is already ≤12 from ingestion, so this is just the 8/9 boundary).
- **`dict_score(label, criteria) -> tuple[float, str]`** — returns `(score, segmentation)`:
  - `whole = zipf_frequency(label, "en")`; candidate segmentation `label`.
  - for each split point `i` where **both** parts have length ≥ 3: `combine(zipf(left), zipf(right))` (default `min`).
    *(Raised from ≥2 at build time — see Build notes: wordfreq gives 2-letter fragments substantial zipf,
    so a ≥2 floor let consonant-mash like `thng`→`th`+`ng` falsely clear the dict gate.)*
  - `score = max(whole, best split combine)`; `segmentation` = the winner (`label` or `"left+right"`).
- **`pronounce_score(label) -> float`** — delegates to `pronounce.score` (log space; see below).
- **`decide(track, dict_score, seg, pronounce_score, criteria) -> tuple[bool, str]`** — the approved rule:
  - `dict_ok = dict_score >= criteria.zipf_min`; `pron_ok = pronounce_score >= criteria.pronounce_min_score`.
  - **primary:** `pass = dict_ok or pron_ok` if `criteria.primary_allow_invented` else `pass = dict_ok`.
  - **secondary:** `pass = pron_ok or dict_ok`.
  - **`filter_reason` names the admitting/failing gate** (the histogram you tune against):
    - pass via dict → `"{track} dict={dict_score:.2f} {seg}"` (e.g. `secondary dict=3.36 reno+plumber`)
    - pass via pronounce (dict not ok) → `"{track} pronounce={pronounce_score:.2f}"`
    - reject → names what failed, e.g. `"reject primary: not dictionary (dict=1.10<3.0)"` or
      `"reject secondary: dict=2.86<3.0, pronounce=-6.2<-4.5"`.
- **`filter_candidates(conn, criteria, *, recompute=False, limit=None, dry_run=False) -> FilterCounts`** — selects open
  candidates (`lifecycle_status NOT IN` closed) with `filtered_at IS NULL` (or **all** open rows when `recompute`), runs the
  pipeline, and (unless `dry_run`) writes the 6 filter fields via `db.set_filter_result`. Returns a tally
  (processed / passed / by-track / top reject reasons) for the printed summary.

### Pronounceability model — `pronounce.py`

- **`build_tables(top_n=50000) -> dict`** — from `wordfreq.top_n_list("en", top_n)`, keep word **types** matching
  `^[a-z]+$` (unweighted — the *shape* of valid English words, not usage frequency). Boundary-pad each word `^^word$`
  and count trigrams (+ their 2-char context totals). Emit a dict of **integer counts** + `_meta`, JSON with **sorted keys**:
  ```json
  {
    "_meta": {"top_n": 50000, "wordfreq_version": "…", "built": "YYYY-MM-DD",
              "alphabet": "a-z + '^' start + '$' end", "smoothing": "add-one at load, V=27"},
    "trigram_counts":  {"^^a": N, "the": N, …},
    "context2_totals": {"^^": N, "^a": N, …}    // denominators for P(c3|c1c2)
  }
  ```
- **Load (lazy singleton) + smoothing at load** — add-one (Laplace): `P(c3|c1c2) = (trigram+1) / (context2_total + V)`,
  `V = 27` (26 letters + end `$`). Smoothing lives here, in code, not in the artifact.
- **`score(label) -> float`** — boundary-pad `^^label$`; return the **mean of log P** over the `len(label)+1` trigram
  positions. **Trigram-uniform for all lengths** (no separate bigram path for short labels — two scoring spaces would put
  short primary ≤8 labels on a different scale than long ones, reintroducing the length-inconsistency `allow_invented`
  makes decisive; boundary padding + smoothing already make short labels well-defined). Log space → always ≤ 0, finite.
- **`build-ngrams` CLI** regenerates `pronounce_tables.json` (dev/maintenance; not in the daily cron). Byte-deterministic
  save (sorted keys, integer counts) so a rebuild diff = real corpus change, never float jitter.

---

## Schema changes (`db.py`) — single-authority migration

`filter_pass` (BOOLEAN) and `filter_reason` (TEXT) **already exist** (Phase 1). Phase 3 adds **4 new columns**:

| column | type | note |
|--------|------|------|
| `track` | TEXT | `'primary'` \| `'secondary'` |
| `dict_score` | REAL | best whole/split zipf |
| `pronounce_score` | REAL | mean log-prob (negative) |
| `filtered_at` | TIMESTAMP | idempotency guard + when |

Both paths converge:
- **Fresh DBs:** the 4 columns are in the `CREATE TABLE` DDL.
- **Existing DBs:** `init_db` runs an idempotent `_migrate(conn)` — for each new column, `ALTER TABLE candidates ADD
  COLUMN …` guarded by a `PRAGMA table_info(candidates)` presence check. `init_db` stays the **single schema authority**
  (create + migrate); `filter` assumes `init-db` was run (same contract as `ingest`). After `init_db`, all **6** filter
  columns are present.

**`--recompute` invariant (stated to prevent a future "should it cascade?" debate):** recompute rewrites **only** the 6
filter columns; it **never** clears downstream (`tier1_score`, `tier2_scores`, `value_range`, `verified_at`, `scored_at`,
outcomes). Downstream phases select `WHERE filter_pass = 1`, so a row demoted by a threshold change simply **stops flowing
forward** — no cascade, and its historical scores/outcomes are retained.

---

## CLI

```
python -m domainscout filter [--criteria criteria.toml] [--recompute] [--limit N] [--dry-run]
python -m domainscout build-ngrams [--top-n 50000] [--out domainscout/pronounce_tables.json]
```
- `filter` uses the global `--db`. Default processes `filtered_at IS NULL` open rows; `--recompute` = all open rows (the
  post-tuning re-filter path); `--dry-run` computes + prints the summary, writes nothing.
- Prints a per-run summary: `processed / passed / primary·secondary split / top reject reasons`.
- `build-ngrams` is a maintenance command (rebuild when bumping `wordfreq` or `--top-n`); its output is committed.

---

## Config & dependency changes

- **`pyproject.toml`:** `dependencies = ["httpx", "truststore", "wordfreq"]` (3rd runtime dep — powers *both* the dict gate
  and the n-gram corpus). Package-data include for `domainscout/pronounce_tables.json`.
- **`criteria.toml`:**
  - `[primary] allow_invented = true` (new; default true).
  - `[dictionary] combine = "min"` (new; `min` | `mean`).
  - `[pronounceability] min_score` — **semantics change to a log-space floor** (negative). The Phase-1 `0.02` is obsolete;
    the value is **set by the build calibration step** (below) and committed with a rationale comment.
- **`config.py`:** add `primary_allow_invented: bool` and `dictionary_combine: str` to `Criteria`
  (defaults `True` / `"min"` when the keys are absent, for backward-compat). `pronounce_min_score` stays a float (now negative).

---

## Testing strategy (TDD: red → green → commit per task)

- **`classify`** — boundaries: 8 → primary, 9 & 12 → secondary.
- **`dict_score`** — whole-word (`apple` high), best 2-way split (`redfox` → `red+fox`, score = `min`), non-word (`xqzk` ≈ 0),
  returns the winning segmentation; fragments < 3 chars never win (both parts ≥ 3; regression test pins the floor so
  2-letter wordfreq noise like `th`/`ng` can't re-admit consonant-mash).
- **`pronounce.score`** — tests inject a **small deterministic fixture table** (no dependency on the 50k build) and assert:
  real-word > invented-pronounceable > keyboard-mash ordering; smoothing (an unseen trigram is finite, not `-inf`).
- **`score` scale-contract** — on the fixture: every score **finite and ≤ 0** (log space) and **monotonic** across the
  ordered sample. Pins the space so an arithmetic↔log refactor can't pass the ordering tests while shifting threshold semantics.
- **`build_tables`** — small synthetic word list → known integer trigram counts + boundary padding; `_meta` present.
- **`decide` matrix** (all six meaningful branches) — primary dict-pass; primary invented-pass (`allow_invented=true`);
  primary invented-**reject** (`allow_invented=false`); secondary pronounce-only; secondary dict-only; secondary both-fail.
- **DB migration** — `init_db` on a pre-Phase-3 schema adds the 4 columns idempotently (PRAGMA path); all 6 present after.
- **Integration** (temp DB) — seed candidates → `filter_candidates` → 6 fields correct per row; idempotent re-run no-ops
  (`filtered_at IS NULL`); `--recompute` reprocesses **and never touches downstream columns**; `--dry-run` writes nothing.
- **CLI** — `filter --db <tmp>` on a seeded DB creates results + prints the summary; `build-ngrams --out <tmp>` writes a
  sorted-key integer-count JSON.
- **No network** in the suite (wordfreq data local; tables = fixture/synthetic).

## Build-time real-data confirmations (per "test each phase with real data")

1. **`wordfreq` place-name spike — DONE 2026-07-14** (recorded): common words all ≥ 3.86 (zipf_min 3.0 is sane); big cities
   mostly clear 3.0 but `plano` 2.92 / `provo` 2.86 dip below; all services clear (`hvac` 3.03 lowest); invented words =
   0.00. **Consequence:** with `min`-combine, a geo+service name whose city dips below 3.0 fails the *dict* gate but is
   re-admitted on the **secondary** track via pronounceability (logged as `pronounce=`, not `dict=`). Also: the ≤12
   ingestion ceiling means only *shorter* combos land (`renoplumber` 11, `wacohvac` 8; literal `austinplumber` = 13 never lands).
2. **Build real tables + confirm artifact size** (expect low-single-digit MB for ~19k trigram contexts; gzip only if larger).
3. **Filter a real ingested batch** (re-ingest a live feed date via Phase 2), eyeball survivor count vs the 50–200 target,
   spot-check a sample of pass/reject classifications by track, then **calibrate `pronounce_min_score`** in log space from
   the observed score distribution (place the invented names we want — `zylo`/`quivo`/`brixly`/`vantor` — clearly above
   keyboard-mash, hit the volume target) and commit the chosen value + rationale into `criteria.toml`.

---

## Self-review

- **Placeholders:** none — the one to-be-determined number (`pronounce_min_score`) is explicitly a calibration output of
  build step 3, in the (now log) space fixed by decision #4, not an arbitrary shipped constant.
- **Consistency:** reuses Phase-1 `filter_pass`/`filter_reason`; adds 4 columns via the single-authority `init_db` migration;
  `decide` reasons match the stored `dict_score`/`pronounce_score`; the `min`-combine + track-specific OR-logic match the
  spike findings; no `lifecycle_status`/downstream writes (Phase 4/5 boundaries intact).
- **Scope:** one phase — classification + two tunable gates + persistence + CLI; no RDAP/scoring bleed-in.
- **Isolation:** pure scoring (`classify`/`dict_score`/`pronounce.score`/`decide`) separated from the DB loop
  (`filter_candidates`) and the table build (`build-ngrams`); each independently testable with fixtures, no network.

---

## Build notes (2026-07-14)

Built via the 9-task TDD plan (`docs/superpowers/plans/2026-07-14-phase-3-rules-filter.md`). 91 tests pass, no network in
the suite. Two refinements were made against real data and ratified by the owner:

**1. Dictionary split-part floor raised ≥2 → ≥3 chars.** `wordfreq` assigns 2-letter fragments substantial zipf
(`th`=4.2, `ng`=3.9, `aa`=4.01), so a ≥2 floor let consonant-mash clear the dictionary gate via a bogus split
(`thng`→`th`+`ng`, min 3.9 ≥ 3.0). The ≥3 floor kills that noise and loses no genuine multi-word target (real combos —
`red+fox`, `plano+hvac` — use ≥3-char words). A regression test (`test_dict_score_no_two_char_fragment_noise`) pins it.

**2. `pronounce_min_score` calibrated to −4.0 — a MASH-ONLY gate — and the survivor-volume target revised.**
Calibrated on the 2026-07-11 live feed (3,717 candidates through the ingestion gate):

- The **dictionary gate alone passes 472** (122 primary ≤8-char gems + 350 secondary) — already **above** the design's
  "~50–200/day" figure before pronounceability adds anything.
- Real expired .coms are overwhelmingly pronounceable (pronounce-score median −2.94, p10 −3.81, p5 −4.04), so the
  pronounceability OR-gate is a **wide net**, especially for the secondary (9–12) track.
- The trigram model **cannot separate borderline invented from borderline mash** — they score identically (good `zylo`
  −3.88 / `nuvex` −3.61 / `hvac` −3.79 vs mash `vgkxq` −3.88 / `bwqkx` −3.92). Only unambiguous mash is cleanly below
  (`xqzk` −4.23, `qwrtz` −4.11, `ktzzr` −4.30).
- **Consequence:** "~50–200/day" and "keep invented-name recall" are mutually exclusive at this stage — a floor tight
  enough to reach ~200 (≈ −2.2) also rejects the invented names the secondary track exists to catch. Volume control past
  mash-removal belongs to the downstream **Tier-1 (Haiku) triage**, which is the next designed funnel.
- **Owner decision (2026-07-14):** set the floor to **−4.0 (mash-only, max recall)**. Survivor sweep for reference:
  −2.5→818, −2.75→1,388, −3.0→2,085, −3.5→3,064, **−4.0→3,498** (primary 1,162 / secondary 2,336; 219 mash rejected).
  The "~50–200/day" figure in this doc and `CLAUDE.md` is therefore a **post-Tier-1** expectation, not a rules-filter one.
  The floor is a one-line `criteria.toml` tunable; the Phase-6 outcome loop can retune it against real results.

**Artifact:** `domainscout/pronounce_tables.json` = **74 KB** (47,973 word types, top-50k after the `^[a-z]+$` filter);
integer counts, sorted keys, `_meta` embedded. Well under the size ceiling — no gzip needed. Shipped as package data.
