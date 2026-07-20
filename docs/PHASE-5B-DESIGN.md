# Phase 5b — Toxicity gate: design

**Status:** ✅ **APPROVED 2026-07-18** (owner review: Part A approved with 5 amendments, Part B approved as
written). Not yet built. · **Date:** 2026-07-18
**Companion docs:** [`CLAUDE.md`](../CLAUDE.md) · [`DECISIONS.md`](../DECISIONS.md) ·
[`docs/TECHNICAL-DESIGN.md`](TECHNICAL-DESIGN.md) §4.5, §9 · [`docs/PHASE-5A-DESIGN.md`](PHASE-5A-DESIGN.md)

> **Phase 5 is built in three sub-phases** (scope call, owner-delegated 2026-07-16):
>
> | Sub-phase | Component | External dep | Anthropic key? |
> |---|---|---|---|
> | 5a ✅ built | `comps.py` — NameBio comps grounding | NameBio free API | no |
> | **5b (this doc)** | `toxicity.py` — Wayback CDX + Safe Browsing gate | free Google Cloud API key | **no** |
> | 5c | `scoring/` — Haiku triage → Sonnet deep, Batch API | Anthropic key + `REPLICATE_API_TOKEN` | yes |

---

## Credential note (corrected 2026-07-18)

Google Safe Browsing **does require an API key**, obtained from a Google Cloud / Developer Console project.
It does **not** require a billing account — the v4 Lookup API is free within quota (~10k req/day, versus our
~30/day). Google's own *Get started* page: *"You need an API key to access the Safe Browsing APIs"* and
*"You need a Google Developer Console project in order to create an API key."*

There is **no keyless public Safe Browsing API**. The keyless Google properties nearby are consumer
surfaces — `safebrowsing.google.com`, the Transparency Report site-status page, in-browser protection —
none of which may be called programmatically. **Web Risk** is the enterprise sibling and *does* require
billing; it is not what we use.

`GOOGLE_SAFE_BROWSING_API_KEY` was already reserved in `.env.example`. Owner obtained the key 2026-07-18.

---

## Scope

**In:** a network-backed screen that (a) hard-rejects domains currently listed by Google Safe Browsing and
(b) derives a *history-shape signal bundle* from Wayback CDX capture metadata, exposed as a library plus a
debug CLI, ready for 5c to call between Tier-1 and Tier-2 and inject into the Tier-2 prompt.

**Out:** Tier-1/Tier-2 scoring and prompts (5c); fetching or parsing archived page *content*; backlink-anchor
analysis (**deferred** — DECISIONS #6, no good free API); writing `candidates` rows; the prior-drop-count
quality signal (**5c owns it** — see *Forward-carried to 5c*).

### Boundary — a library, not a pipeline stage

5b ships `toxicity.py` (screen + cache) and one CLI subcommand. It does **not** write `candidates`, and it
does **not** run as a standalone daily stage.

The gate runs *between* Tier-1 and Tier-2, and **Tier-1 does not exist until 5c** — Tier-1 is what decides
which ~30 domains are worth screening. Shipping a stage now would mean screening all ~3,500 daily filter
survivors against rate-limited APIs to serve the ~30 that eventually matter, which is precisely the
critical-path problem the between-tiers sequencing exists to avoid (TDD §4.5). So 5c calls `screen()` on its
Tier-1 survivors, exactly as it calls 5a's `lookup()`.

*This mirrors 5a's ratified "library, not a stage" boundary. The one asymmetry: toxicity verdicts, unlike
comps, ARE per-domain state — hence the file cache below rather than pure recomputation.*

---

## Architecture

**Module:** `domainscout/toxicity.py`. **Zero new dependencies** — `httpx` and `truststore` are already
runtime deps (Phases 2/4/5a); everything else is stdlib.

**Network lives ONLY in the two client classes.** All decision logic is pure and separately testable — the
5a discipline that kept the suite at zero network calls.

```python
screen(
    domains: Sequence[str],
    *,
    cdx: CdxClient,          # injected -> tests never hit the network
    gsb: GsbClient,          # injected
    criteria: Criteria,
    cache: VerdictCache | None = None,
    now: datetime | None = None,   # injected -> deterministic TTL tests
) -> list[ToxicityVerdict]
```

Batch-shaped, not single-domain, because of the GSB batching below.

**Returns one verdict per input domain, in input order** — callers may zip results against their input list.

**Cache interaction with batching:** a cache hit that is still within TTL (and whose `collapse` matches)
short-circuits **both** legs for that domain — no CDX call, and the domain is **excluded from the GSB batch
entirely**. Only cache-missing domains are assembled into the GSB request. This matters during 5c
development, when the same ~30 domains get re-screened repeatedly.

**Pure helpers** (no I/O, directly unit-tested): `parse_cdx(payload) -> list[Capture]`,
`compute_shape(captures, ...) -> HistoryShape`, `decide(gsb_result, shape, errors) -> (verdict, reason)`.

**TLS:** both clients are built through the `ingest.make_client()` pattern — `truststore.SSLContext`
verifying against the **OS trust store**, not certifi. This box MITMs HTTPS with a private root CA; certifi
fails here. Non-negotiable, and the reason Phases 2/4/5a all work.

### The two legs are deliberately asymmetric

| Leg | Call pattern | Role |
|---|---|---|
| **Google Safe Browsing v4** | `POST threatMatches:find` — up to **500 URLs per request**, so an entire day's ~30 domains (×2 schemes = 60 URLs) fit in **one** call | **Hard reject** on any match |
| **Wayback CDX** | **2 GETs per domain** (apex + `www.`, each `matchType=exact` + server collapse), no batching available | **Graded signal** → Tier-2 |

GSB's batching means the whole day's blocklist check costs a single request, so its rate-limit surface is
effectively nil and **CDX is the only pacing concern** (~1 req/s, own backoff).

**Why asymmetric** (owner-ratified): a Safe Browsing match is a *factual blocklist hit* needing no judgement,
so it rejects deterministically and cheaply, before any Sonnet tokens are spent. History *shape* is a
genuine judgement call — a two-year gambling stint eight years ago on an otherwise clean fifteen-year
business domain is not something a threshold should decide — so it becomes context for Tier-2. This also
honours TDD §9, which explicitly wants history shape as a **quality** signal feeding drop-reason inference,
not as a binary.

---

## Data model (`models.py`, alongside `CompsContext`)

**`ToxicityVerdict`**: `domain`, `verdict`, `reason`, `gsb_currently_listed`, `gsb_threat_types`,
`gsb_checked_at`, `history` (`HistoryShape | None`), `screened_at`, `collapse`, plus `to_json()` for 5c
prompt injection.

**`HistoryShape`**: computed **twice** over the same CDX response, plus a divergence block —

| Block | Contents |
|---|---|
| `lifetime` | `first_capture`, `last_capture`, `span_years`, `capture_count`, `distinct_years`, `max_gap_years`, `digest_churn` (distinct digests ÷ captures), `status_mix`, `mime_mix` |
| `tail` | the same metrics over the final `tail_window_months` **anchored on `last_capture`**, not on today |
| `divergence` | `churn_ratio` (tail ÷ lifetime), `status_shift`, `mime_shift`, `captures_per_year_ratio` |

**Divergence metrics are defined precisely** (they are otherwise readable two ways):

| Metric | Definition |
|---|---|
| `churn_ratio` | `tail.digest_churn / lifetime.digest_churn` — `None` if the denominator is 0 |
| `status_shift` | `tail_2xx_proportion - lifetime_2xx_proportion` — signed, range −1.0…+1.0 |
| `mime_shift` | `tail_texthtml_proportion - lifetime_texthtml_proportion` — signed, same range |
| `captures_per_year_ratio` | `tail.captures_per_year / lifetime.captures_per_year` |

A late-life flip typically shows as `churn_ratio` well above 1.0 (content changing far faster than the
domain's own historical norm), often with a negative `mime_shift` as real pages give way to thin
redirect/parking responses. **5b computes and reports these; it does not threshold them** — interpretation
is Tier-2's job, per the asymmetric-legs decision.

### Why the tail window exists (owner amendment, 2026-07-18)

Lifetime aggregates **structurally cannot show the pattern this phase is assigned to detect.** A domain with
twelve clean years and eighteen months of gambling at the end — the classic spam-then-drop signature —
produces entirely respectable lifetime numbers: long span, healthy capture count, modest overall digest
churn. The flip lives in the tail, and whole-life averaging dilutes it toward invisibility.

Computing the same bundle over the final window and treating the **divergence** as the signal recovers it,
while staying strictly within capture metadata — no snapshot fetching, no keyword lists, no language
detection, same single CDX response, just bucketed by timestamp.

**Tail is anchored on `last_capture`, not on today**, so a domain that died in 2015 has a 2013–2015 tail.
"Late-life" means late in *the domain's* life.

**Guards:** if the tail holds fewer than `tail_min_captures` (default 3), or if `span_years` is shorter than
the tail window (tail would equal lifetime), `divergence` is `None` — never a fabricated ratio from two data
points. `digest_churn` division is zero-guarded.

---

## Verdict enum and precedence

Four values. The `unknown` split is **structural, not prose in the `reason` field** (owner amendment) —
5c's prompt must be able to branch on it, and the re-screen logic needs something to key on.

```
gsb listed              -> reject               terminal; wins over everything
gsb or cdx errored      -> unknown_error        transient ignorance; re-screened next run
cdx ok, zero captures   -> unknown_no_history   stable absence; re-screening won't change it
otherwise               -> pass
```

All non-`reject` verdicts **proceed to Tier-2**, carrying their reason.

### Partial results survive a partial failure

**The verdict reflects the worst leg; the data reflects every leg that succeeded.**

If GSB succeeds (not listed) but CDX times out, the verdict is `unknown_error` — *and*
`gsb_currently_listed=false` with `gsb_checked_at` still ride along, because they were legitimately
observed. Conversely a successful `HistoryShape` survives a GSB failure. The dataclass permits this; stating
it explicitly forecloses the obvious wrong implementation, which nulls the whole verdict on any error and
silently throws away work that was already paid for. Covered by a dedicated test row.

`unknown_no_history` and `unknown_error` mean genuinely different things and have different futures.
The first is stable, informative absence — for an invented secondary-track brandable it is mildly
*reassuring* ("young or never-noticed name"). The second is transient ignorance and is eligible for
re-screen. Collapsing them into one string would leave 5c unable to tell them apart.

---

## Three ratified invariants

These are the failure modes most likely to go quietly wrong. All three were flagged in design and
**ratified as stated** by the owner.

**1. No Wayback history is `unknown_no_history` — not clean, not toxic.**
This is 5a's *"a missing keyword is absence of evidence, not a $0 comp"* rule applied to history.
Invented brandable names on the secondary track will **routinely** have zero captures. If never-archived
collapsed into either "clean" or "suspicious", we would systematically mis-score exactly the class of names
this pipeline exists to find.

**2. A network failure must never read as clean.**
CDX timeout, 5xx, or GSB error ⇒ `unknown_error` with the failure recorded — never a silent `pass`.
`unknown_error` still **proceeds** to Tier-2 rather than failing closed: archive.org is genuinely flaky, and
failing closed means one bad archive day silently empties the digest, which is invisible in a way a
false-positive never is.

**3. A clean GSB result is a snapshot, not a guarantee.**
GSB lists *currently* flagged URLs. A dropped domain that served malware in 2019 may well have aged off the
list. The field is therefore named **`gsb_currently_listed: false`** — and the word `clean` and the word
`safe` appear **nowhere** in the emitted JSON. Field names are prompts too: 5c must not be able to present
this as verified-safe even if a future prompt author forgets the caveat.

### Scope limit — the GSB leg is a **host-level** check (measured 2026-07-20)

Not an invariant but a boundary on what invariant 3's snapshot actually covers, so it sits here rather than
in a footnote. `_find` probes the bare forms `http://{domain}/` and `https://{domain}/`. GSB v4 expands a
lookup URL into host-suffix / path-prefix combinations, and `http://d/` expands only to `d/` and `d` — so a
blocklist entry stored at `d/some/path/` **cannot** match. Confirmed live: a host with an active MALWARE
listing at a path returned `currently_listed=False` for its bare forms.

`threatMatches:find` takes URLs, not hosts; there is no "is anything under this host listed?" query, so this
is an API boundary rather than a bug in the probe. **Practical meaning:** wholly-malicious domains (commonly
host-listed) are caught; **path-scoped** listings — the characteristic shape for *compromised legitimate
sites*, which is a real slice of this pipeline's target population — are not. How often that bites real
expired .coms is **unmeasured**; the confirming test host is synthetic and path-listed by construction.

**Owner ruling (2026-07-20): accept and document.** Path-scoped toxicity falls to the CDX history-shape
signal and Tier-2 judgement. The alternative — injecting the top-N CDX-observed paths as extra
`threatEntries` — was rejected for 5b because it would couple the two deliberately-independent legs and
force a re-budget against the 500-URL cap. Reconsider if Phase-6 outcomes show path-scoped misses mattering.

---

## Caching

`data/toxicity_cache.json`, keyed by domain, **gitignored** (new `.gitignore` entry alongside
`data/namebio_*`). Written via temp-file + `os.replace`, with the swap `OSError` caught — 5a hit a real
Windows AV file-lock during rename.

Two rules carry the design:

**`unknown_error` is NEVER written to the cache.** Not TTL-0 — simply never persisted. A transient failure
then *cannot* be misconfigured into stickiness, which any numeric TTL eventually can.

**Every entry records the `collapse` value it was computed under.** On read, an entry whose `collapse`
differs from current config is treated as a **miss**. This makes "thresholds are calibrated to this collapse
setting" self-enforcing rather than a comment somebody has to notice (owner amendment).

Per-verdict TTLs mirror Phase 4's `recheck_days` shape:

| Verdict | TTL | Rationale |
|---|---|---|
| `reject` | 30 d | **deliberately slow to forgive** — NOT permanent state. GSB listings do age off, so a domain rejected on day 1 that delists on day 10 stays invisible until the TTL expires. Defensible at our volumes (a recently-listed domain is a legitimately scary candidate regardless), but read this as a long TTL, never as a terminal flag. |
| `pass` | 14 d | GSB listing can change; shape barely does |
| `unknown_no_history` | 30 d | stable absence |
| `unknown_error` | *never cached* | always retried next run |

---

## Config — `[toxicity]` in `criteria.toml`

Measured limits land here as comments, in the house style established by `[rdap]` and `[comps]`.

```toml
[toxicity]
cdx_base_url = "https://web.archive.org/cdx/search/cdx"
                                 # HTTPS, not HTTP: this box MITMs TLS and the whole codebase is
                                 # hardened for the proxied-TLS path via truststore. One plaintext
                                 # call would be both unencrypted AND on a differently-behaving
                                 # code path from everything we've tested.
cdx_collapse = "timestamp:6"     # One capture/month. ALL shape thresholds and any Tier-2 prompt
                                 # calibration are relative to this sampling. capture_count and
                                 # digest_churn are properties of the SAMPLED series, not the raw
                                 # archive (arguably better: raw counts are dominated by
                                 # crawl-frequency artifacts). Changing it invalidates every cached
                                 # verdict - by design, since entries record their collapse and
                                 # miss on mismatch.
                                 # Applied SERVER-side (legitimate: one urlkey per query, so
                                 # adjacent-row collapse IS monthly collapse), then again
                                 # client-side over the merged two-host set.
cdx_match_type = "exact"         # MEASURED 2026-07-18 (spike, see PHASE-5B-SPIKE.md). NOT "domain":
                                 # matchType=domain spreads the row budget across thousands of
                                 # urlkeys (cnn.com: 2,768), so collapse samples per-URL-block and
                                 # digest_churn reads URL DIVERSITY as content volatility. Two exact
                                 # queries (apex + www.) are issued and merged instead - see
                                 # CdxClient. Deep-subpath history is deliberately given up; it was
                                 # the contamination, and shape is the story of the front door.
cdx_limit = 5000                 # Never engages under the ratified strategy - the worst-case domain
                                 # measured collapses to 311 rows. Kept as a runaway guard only.
cdx_timeout = 20.0
cdx_max_requests_per_sec = 1.0
cdx_max_retries = 3
tail_window_months = 24          # the late-life flip window, anchored on last_capture
tail_min_captures = 3            # below this, divergence is None rather than noise

gsb_base_url = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
gsb_batch_size = 250             # API cap is 500 URLs; we send 2 schemes x domain
gsb_timeout = 15.0
gsb_threat_types = ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE",
                    "POTENTIALLY_HARMFUL_APPLICATION"]
gsb_platform_types = ["ANY_PLATFORM"]     # v4 REQUIRES all three lists. An empty or omitted list
gsb_threat_entry_types = ["URL"]          # returns NO MATCHES rather than erroring - a silent
                                          # false-clean, exactly the failure invariant 2 exists to
                                          # prevent. Asserted in the spike and in tests.

[toxicity.cache_days]
reject = 30
pass = 14
unknown_no_history = 30
# unknown_error is deliberately absent - it is NEVER cached.
```

**GSB queries both `http://` and `https://` forms per domain** (owner amendment). Canonicalization usually
makes a host-level entry match either scheme, but at 60 URLs against a 500 cap it is free insurance for the
case where it does not.

---

## `.env` loading

**5b is the first code in the repo to need a secret.** `.env.example` has existed since Phase 1, but nothing
reads it. A ~15-line stdlib loader lands in `config.py`:

- parse `KEY=VALUE`, skip blanks and `#` comments, strip optional surrounding quotes
- **never overwrite an already-set `os.environ` variable** — a real environment variable beats the file,
  which is what makes Task Scheduler and CI overrides work
- absent `.env` is not an error

*Rejected:* `python-dotenv` (a 5th runtime dep for a flat format we fully control, with none of the quoting
or interpolation edge cases that justify the library); and os.environ-only (would make `.env.example` a
lie and turn every manual run into a setup ritual).

---

## CLI

```
screen --domain X [--domains a.com,b.com] [--no-cache] [--json] [--dry-run]
```

Unlike 5a's local-only `comps --domain`, **this subcommand does hit the network.** `--dry-run` reports the
calls it would make without making them. `--domains` exercises the GSB batch path.

**Errors:** missing `GOOGLE_SAFE_BROWSING_API_KEY` → clean stderr message + exit 1 (5a's `CompsCacheMissing`
precedent — never a raw traceback). GSB **403** (bad key / quota exhausted) is reported distinctly from
**400** (malformed request — our bug), because they need opposite responses from the operator.

**All runtime output is ASCII.** Carrying forward 5a's final-review lesson, where a single `⚠️` was the only
non-ASCII runtime print and crashed on redirected cp1252 stdout — the exact Task Scheduler path this will
run under.

---

## Testing

Zero network in the suite; live smoke marked `@pytest.mark.skip` (5a precedent, which currently carries 2
skipped smokes). **Fixtures are captured during the spike** so they are real CDX payloads rather than
invented ones.

The tests that matter are the ones guarding the traps identified above:

| Test | Guards |
|---|---|
| never-archived → `unknown_no_history`, never `pass` | invariant 1 |
| CDX timeout → `unknown_error`, never `pass` | invariant 2 |
| GSB `{}` → clean-snapshot, never parsed as an error | v4 omits `matches` entirely on a clean batch |
| GSB match → `reject`, threat types recorded | hard-reject leg |
| GSB ok + CDX error → `unknown_error` **with `gsb_currently_listed` still populated** | partial results survive partial failure |
| CDX ok + GSB error → `unknown_error` **with `HistoryShape` still populated** | same, mirrored |
| request body always carries non-empty `platformTypes` and `threatEntryTypes` | an empty list returns no matches instead of erroring — silent false-clean |
| tail query returns the domain's final window even when the lifetime query truncates | the tail-window guarantee (spike objective 1) |
| changed `collapse` → cache miss | self-enforcing calibration |
| `unknown_error` never persisted to cache | retry path stays live |
| tail-flip fixture → divergence detected where lifetime aggregates look respectable | the whole point of the tail window |
| tail below `tail_min_captures` → `divergence is None` | no fabricated ratios |
| `.env` does not clobber an already-set env var | override precedence |
| non-ASCII never reaches stdout (cp1252 regression) | 5a's cron-path crash |

---

## Pre-build empirical spike (~30 min)

Mandated as a pre-task, mirroring the NameBio spike that materially changed 5a's design. This project
documents **measured** limits, not documented ones — a discipline that has now paid twice.

### Spike objective 1 — ✅ RESOLVED 2026-07-18: (a)+(d) REFUTED; strategy is two collapsed exact queries

**Ran 2026-07-18 (commit `fb13325`). Full measurements: [`docs/PHASE-5B-SPIKE.md`](PHASE-5B-SPIKE.md).**
The spike hit its Step-9 stop condition and refused to improvise; the owner re-planned from its evidence.

**What failed:**

| Strategy | Measured outcome |
|---|---|
| (a) uncollapsed `matchType=domain` | **Unmanageable.** The *single-URL apex alone* is 72.2 MB / 1,042,676 rows / 28 s. The domain-wide unbounded query did not complete in 257 s+, and a 90 s `httpx` read-timeout never fires — the server trickles bytes steadily, so total duration is unbounded from the client's side. `limit=50000` (10×) bought ~10 more years out of 26: *raising the limit only moves the cliff*, confirmed verbatim. |
| (d) `from=` bounding | **Fails outright.** `from=20240101&limit=5000` truncates at **2024-01-07** — six days into a 2.5-year window. Rows stay urlkey-then-timestamp sorted regardless of the filter, so time-bounding does not defeat row-truncation. |
| (d′) negative limit | **A third failure mode, unanticipated.** Reaches recent timestamps, but under `matchType=domain` returns **100% one static JS asset** (alphabetically-last urlkey). Answers "is the tail reachable" correctly while carrying zero page-history signal. Its calendar reach is also crawl-density-dependent, so a constant `N` cannot guarantee a 24-month tail across domains. |

**Correction to this document's own review-2 reasoning:** the claim that `matchType=exact` "does not fix it,
since ascending truncation still eats the tail" was **over-generalized**. The mechanism was right, but
server-side collapse on a *single* urlkey shrinks 1M+ rows to ~311 — far below any cap — so the truncation
precondition never holds. Option (b) was dismissed on reasoning that stops applying once collapse is in play.

**✅ RATIFIED STRATEGY (owner, 2026-07-18): two `matchType=exact` queries per domain — the bare apex and
`www.<domain>` — each with SERVER-side `collapse=timestamp:6`, merged and de-duplicated, then bucketed
monthly client-side over the merged set.**

- Server-side collapse is **semantically legitimate here**, unlike under `matchType=domain`: one urlkey per
  query means adjacent-row grouping *is* monthly grouping. The contamination that motivated client-side
  bucketing does not exist within a single-host query.
- **Truncation never engages** — ~311 rows against a 5,000 cap for the worst-case domain measured.
- **Client-side `bucket_monthly` is still applied**, now to the *merged* two-host set. Merging two
  independently-collapsed series can re-introduce two rows in the same month, so the client-side pass
  remains necessary — and at ~600 rows it is free. It also makes the result independent of merge order.
- **Why both hosts:** apex-only was the spike's own candidate, but for a domain whose apex merely 301s to
  `www`, the shape would be computed from a redirect history and *nothing would look wrong* — a 301-only
  history reads as a thin but valid shape. The second query costs one small request and removes that
  silent-failure class.
- **Loss of deep-subpath history is accepted, and is arguably a feature.** Sub-page URL diversity is
  precisely the signal that was inflating `digest_churn` into false content-volatility. Domain history
  *shape* is the story of the site's front door.
- **One host failing does not fail the domain** — if either query succeeds, its captures are used. Only a
  failure of *both* raises `CdxError`. (Mirrors the partial-results rule.)

**Open, carried forward (neither blocks the build):** `www.cnn.com` returned **byte-identical** results to
the apex — either CDX canonicalizes the `www.` prefix or cnn.com is a coincidence; the two-query strategy is
correct either way (a duplicate merge is a no-op), but it should be checked against a domain where the hosts
are known to differ. And no CDX saturation test was run: *"no 429 observed"* means **not yet hit**, not
confirmed absent.

**Settled for free by observation:** a never-archived domain returns the literal bytes `[]` — a bare empty
array, **not** a header-only response.

<details>
<summary>Original objective-1 statement (superseded, kept for the record)</summary>

**The design's tail window rests on an assumption the naive query does not satisfy.** Raised in owner review
2026-07-18; the spike decides the resolution and the plan proceeds from its answer.

CDX returns rows sorted by **urlkey first, then timestamp ascending**, and `collapse` operates on
**adjacent** rows. Two consequences:

- **Collapse semantics are wrong as specified.** With `matchType=domain`, `collapse=timestamp:6` yields
  roughly one capture per month *per URL block* — not one per month across the domain's life. Multi-URL
  domains would show inflated `capture_count` and inflated `digest_churn`, because different pages
  legitimately have different digests. That is **URL diversity being misread as content volatility** — and
  content volatility is exactly what the flip detector keys on.
- **Row-count truncation cannot safely bound the tail (severe).** `limit=5000` truncates in the returned
  order, so a heavily-crawled domain can exhaust its budget on alphabetically-early URLs and never reach
  large parts of the timeline. **And ordering alone does not fix this:** because timestamps run *ascending*
  within a block, any row-count truncation drops the **newest** captures first — precisely the window this
  phase exists to examine. The late-life flip detector would fail **silently**, and worst on the big, old,
  interesting domains where a flip matters most. Raising the limit does not fix it; it only moves the cliff.

**Options to evaluate, in the spike:**

| # | Approach | Assessment |
|---|---|---|
| (a) | Keep `matchType=domain`, drop **server-side** collapse; fetch fields-limited rows (`fl=timestamp,statuscode,mimetype,digest`) and bucket monthly **client-side** | Fixes collapse semantics completely and makes `cdx_collapse` a local constant rather than a trusted server behaviour. Does **not** by itself fix truncation. Payload cost must be measured. |
| (b) | `matchType=exact` on the apex + a second query for `www.` | Loses deep-subdomain history, and — contrary to first impression — **does not fix truncation**, since ascending order still drops the tail. |
| (c) | Probe whether CDX `sort` / pagination options behave well enough for time-ordered output | The documented-but-unreliable corner. Worth measuring, not worth depending on. |
| (d) | **Two queries per domain:** a lifetime query (metrics degrade gracefully under truncation) **plus a tail query bounded by `from=`/`to=` or a negative `limit`** | ← **current prior.** Bounding the tail by *time* rather than *row count* makes its presence a guarantee, not a hope. 60 calls/day at our volume is nothing. |

Combining **(a) + (d)** is the expected landing point: client-side bucketing for honest semantics, a
time-bounded second query for a tail that cannot be truncated away. The spike confirms or refutes it.

**The assertion that decides this:** for a ~25-year heavily-crawled domain, **verify the final two years
actually appear in the response.** If they do not, the tail window is measuring nothing.

**CDX, remaining measurements:** ~20 real domains including (a) one **very long-lived, heavily-crawled**
domain archived since the late 1990s, for worst-case payload size and latency; (b) a deliberate
**never-archived invented name**, to confirm the zero-capture path; (c) something flip-shaped if one can be
found. Record rate limits, error modes, payload sizes, and pagination behaviour.

</details>

### Spike objective 2 — ✅ RESOLVED 2026-07-20: shapes confirmed; empty-list premise measured

> Ran against the real API once the Console blocker cleared — see `PHASE-5B-SPIKE.md` Part 3 (A1–A4).
> Clean batch is a bare `'{}\n'`; a match carries `{threatType, platformType, threat:{url},
> cacheDuration, threatEntryType}`; the URL echo is byte-identical to what was sent (A3, the phase's
> top residual risk, closed favourably). The empty-list premise below holds, with one correction: the
> silent false-clean occurs on empty **`threatTypes`** and **`platformTypes`**, while an empty
> **`threatEntryTypes`** still returned its match (the API appears to default it). The guard refuses
> all three anyway — refusing a harmless case is free, and the defaulting is undocumented.
> The same run surfaced the **host-level-only** scope limit recorded above.

Confirm the batch cap and quota behaviour, plus two specific shapes:

- **Empty vs. match responses.** A clean batch returns bare `{}` with no `matches` key — verify the parser
  treats absent-key as *clean-snapshot*, never as a malformed response.
- **The exact request body.** v4 requires **all three** of `threatTypes`, `platformTypes`, and
  `threatEntryTypes`. An empty or omitted list returns **no matches rather than an error** — a silent
  false-clean, which is the exact failure mode invariant 2 exists to prevent. Confirm live that a
  deliberately-empty list returns no matches, so the guard test is written against observed behaviour rather
  than assumption.

Findings land in `criteria.toml` comments and become test fixtures.

---

## Forward-carried to 5c

Recorded here so it does not evaporate between sub-phases.

1. **Prior-drop count** — `COUNT(*) WHERE domain=? AND lifecycle_status='dropped'` over closed rows, at
   **candidate-selection time**, injected into Tier-2 context alongside the toxicity verdict. Deliberately
   *not* in `toxicity.py`: it is a **quality** signal, not a toxicity one, it needs no network, and it
   already sits in the DB that 5c's selection query must read anyway. Putting it in `screen()` would break
   that function's clean network-signals-only boundary (owner ruling, 2026-07-18).
2. **`gsb_currently_listed` must be presented as a snapshot**, never as verified-safe — and, per the scope
   limit measured 2026-07-20, as a **host-level** snapshot. A `false` means "this host is not itself listed
   right now", **not** "nothing under this host is listed". The Tier-2 prompt must not let the model round
   that up to a clean bill of health.
3. **Tail-vs-lifetime divergence** interpretation belongs in the Tier-2 prompt.
4. **`unknown_error` and `unknown_no_history` must be weighted differently** — transient ignorance vs.
   stable, mildly reassuring absence.
5. **HumbleWorth's triple is three sale channels at P50 / P97.5 / P99.25**, not a low/mid/high band
   (inherited from 5a; a prompt presenting it as one range invites the model to reconcile numbers that were
   never in tension).

---

## Deferred / not in 5b

- **Backlink-anchor sanity** — DECISIONS #6, no good free API.
- **Archived page content** of any kind (fetching, keyword-scanning, or feeding snapshot text to Sonnet).
  Rejected in favour of metadata-only + tail divergence: ~4 extra archive fetches per domain, HTML text
  extraction, and a toxic-keyword list that is both a maintenance burden and a false-positive generator
  (a *pharmacy* domain is not a toxic domain).
- **Toxicity columns on `candidates`** — follows from the library boundary; 5c persists what it needs.
