# Phase 5b — Task 1 empirical spike: findings

**Status (Part 1, 2026-07-18): BLOCKED — the plan's assumed strategy (a)+(d) is refuted by
measurement. Reported for owner re-planning per brief Step 9. Do not build Tasks 2–11 against
(a)+(d) as currently specified.** This was subsequently resolved: `matchType=exact` + server-side
`collapse=timestamp:6` was ratified (see `criteria.toml` `[toxicity]` and `toxicity.CdxClient`'s own
docstring) and Phase 5b was built in full (282 tests, 3 skipped as of the live confirmation below).

**Date:** 2026-07-18. **Domain used throughout:** `cnn.com` (the ~25-year, heavily-crawled worst case
the brief calls for), plus a small scan of real defunct-dotcom domains and two invented never-registered
names. All calls went through `domainscout.ingest.make_client()` (truststore against the OS trust store —
a bare `httpx` call fails TLS verification on this box). All network calls needed
`dangerouslyDisableSandbox: true`; without it the tool sandbox silently drops the connection (hangs with
zero output rather than raising an error) instead of failing fast.

**Objective 2 (GSB): NOT YET RUN as of Part 1 (2026-07-18).** `.env` did not exist yet —
`GOOGLE_SAFE_BROWSING_API_KEY` was absent. Per instruction, objective 1 was completed in full without
waiting; GSB was left for a follow-up once the key landed. **UPDATE (2026-07-19, see Part 2 below):**
the key has since landed, but Objective 2 is still not confirmed working — the Safe Browsing API
itself is blocked at the Google Cloud project level for this key (HTTP 403). This is a BLOCKING
finding for live deployment; see Part 2 for full detail.

---

## 1. Measurements (all against `cnn.com` unless noted)

No rate-limit response (`429`) or `5xx` was observed across ~20 CDX requests in this session; archive.org's
CDX endpoint needs no key and none of these calls were throttled. This was **not** a dedicated saturation
test — the blocking questions took priority — so "no rate limit observed" should be read as "not yet hit,"
not "confirmed absent."

| # | Query | Status | Latency | Bytes | Rows (excl. header) | Oldest ts | Newest ts | Notes |
|---|---|---|---|---|---|---|---|---|
| A | `matchType=domain`, `collapse=timestamp:6`, `limit=5000`, `fl` incl. `urlkey` | 200 | 20.98 s | 1,832,609 | 5000 | 2000-01-15 | **2026-07-01** | `distinct_urlkeys=2768` |
| D | `matchType=domain`, `from=20240101`, `limit=5000`, no collapse | 200 | 4.09 s | 337,719 | 5000 | 2024-01-01 | **2024-01-07** | truncates 6 days into a 2.5-year window |
| neg | `matchType=domain`, `limit=-200`, no collapse | 200 | 4.53 s | 17,249 | 200 | 2025-04-07 | **2026-07-10** | `distinct_urlkeys=1` (see §2) |
| unc-5k | `matchType=domain`, no collapse, `limit=5000` | 200 | 0.89 s | 365,029 | 5000 | 2000-06-20 | **2001-09-24** | |
| unc-50k | `matchType=domain`, no collapse, `limit=50000` | 200 | 12.73 s | 3,650,741 | 50000 | 2000-06-20 | **2011-11-01** | 10× the rows bought ~10 more years |
| unc-∞ | `matchType=domain`, no collapse, **no limit** | — | **killed at 257 s+, did not complete** | — | — | — | — | see §3 |
| exact-neg | `matchType=exact` (apex only), `limit=-200` | 200 | 0.43 s | 14,235 | 200 | 2026-07-02 | **2026-07-18 (today)** | 129 distinct digests/200; spans only 16 calendar days |
| exact-∞ | `matchType=exact` (apex only), no collapse, no limit | 200 | 28.47 s | 72,169,551 (**72.2 MB**) | 1,042,676 | 2000-06-20 | **2026-07-18 (today)** | 211,710 distinct digests — a *single URL's* full history |
| exact-collapse | `matchType=exact` (apex), `collapse=timestamp:6`, `limit=5000` | 200 | 15.94 s | 22,301 | 311 | 2000-06-20 | **2026-07-01** | 198 distinct digests; 311 ≪ 5000, truncation never engages |
| exact-collapse-www | same, `url=www.cnn.com` | 200 | 17.42 s | 22,301 | 311 | 2000-06-20 | **2026-07-01** | **byte-identical** to the apex query — surprising, unresolved (§6) |

**Never-archived** (`zqvoblitherox.com`, `fribbleplonktarn.com`), `matchType=domain`, no collapse/limit:
both `status=200`, ~1 s, **3 bytes**, raw body **exactly `[]`** — a bare empty JSON array, not a
header-only response. **This decides Task 4's empty-handling: treat `[]` as the zero-captures case, not a
malformed/short response.**

**Flip-candidate scan** (real defunct dot-com-bust domains, `matchType=exact` + `collapse=timestamp:6`,
lifetime vs. final-24-row tail digest churn):

| Domain | Rows | Span | Lifetime churn | Tail(24) churn | Ratio |
|---|---|---|---|---|---|
| webvan.com | 151 | 1999–2021 | 0.828 | 0.875 | 1.06 (flat) |
| kozmo.com | 106 | 1998–2022 | 0.245 | 0.375 | 1.53 (mild) |
| boo.com | 215 | 1999–2022 | 0.391 | 0.042 | 0.11 (inverse — went quiet, not toxic) |
| flooz.com | 212 | 1999–2026 | 0.571 | 0.167 | 0.29 (inverse) |
| **theglobe.com** | 221 | 1998–2026 | 0.281 | 0.500 | **1.78** ← selected |
| pseudo.com | 271 | 1997–2026 | 0.815 | 0.667 | 0.82 (noisy throughout) |

`theglobe.com` real capture: 2013–2019 alternates between one stable homepage digest and a 301-redirect
digest (low churn), then a genuine **~6-year archive gap** (2019-06 → 2025-03), then a burst of
distinct-every-capture churn from 2025-03 onward (one transient `403`, one `application/octet-stream`
mimetype) — reads as the domain changing hands / reactivating with new, actively-changing content. In the
**trimmed 80-row fixture actually shipped**, lifetime churn is 12/80=0.15, tail(24) churn is 12/24=0.5,
ratio **3.33** — a clean, real (not invented) divergence signal.

---

## 2. The deciding assertion, per query variant

**Question: for a ~25-year heavily-crawled domain, do the final two years actually appear in the response?**

| Variant | Reaches the tail (timestamp)? | Representative of the domain's actual content? | Verdict |
|---|---|---|---|
| **A** — server-collapse, `matchType=domain` | **Yes** (2026-07-01) | **No** — `distinct_urlkeys=2768`; the 5000-row budget is spent across thousands of different URL blocks, each collapsed independently. Confirms the design doc's collapse-semantics concern: this is URL diversity, not content volatility, being sampled. | Reaches the tail **by luck** (the root urlkey sorts early and is itself long-lived), not by design. Not trustworthy in general. |
| **D** — `from=20240101` + `limit=5000` | **No** — truncates at 2024-01-07, 6 days into a 2.5-year window | N/A (never gets there) | **Fails outright.** Row-limit truncation defeats time-bounding under `matchType=domain`, because rows stay sorted by urlkey-then-timestamp regardless of the `from=` filter. |
| **negative limit** (`-200`), `matchType=domain` | **Yes** (2026-07-10) | **No** — `distinct_urlkeys=1`, and it is not even a content page: **100% of the 200 rows are one static asset**, `com,cnn,zion)/skunk/loader/snowplowloader.js` (an analytics loader), selected purely because its urlkey sorts alphabetically last among cnn.com's crawled URLs. | Reaches the tail in time, but the content is degenerate — a single arbitrary subresource carries zero page-history signal. **This is a distinct failure mode from D's**, not the same one: D fails to reach recent time; negative-limit reaches recent time but returns content that cannot support `HistoryShape` at all. |
| **`matchType=exact` (apex) + negative limit** | **Yes** (today) | Yes, but only for a **16-day window** — cnn.com's current crawl density is so high that `-200` doesn't reach back 24 months. | Calendar reach of a negative limit is **crawl-density-dependent**, not a fixed quantity — a constant `N` in config cannot guarantee a 24-month tail across domains of different crawl density. |
| **`matchType=exact` (apex) + server-side `collapse=timestamp:6`** | **Yes** (2026-07-01, current month) | **Yes** — single urlkey, so adjacent-row collapse is semantically correct (no cross-URL contamination); 311 rows total, nowhere near the 5000 cap, so truncation never becomes a live concern. | **Works cleanly** for the apex URL. Loses deep-subdomain/subpath history (this is the design doc's own listed tradeoff for option (b)) — not evaluated for whether that loss matters to the shape computation. |

---

## 3. Payload cost of client-side bucketing (option (a))

- `matchType=domain`, uncollapsed, `limit=5000`: 365 KB / 0.89 s, but only reaches **2001** (see §2).
- `matchType=domain`, uncollapsed, `limit=50000` (10×): 3.65 MB / 12.73 s, reaches **2011** — 10× the rows
  bought ~10 more years out of 26. Confirms the design doc's own prediction verbatim: *"raising the limit
  does not fix it, it only moves the cliff."*
- `matchType=domain`, uncollapsed, **no limit at all**: did not complete. The process was left running
  257+ seconds (background), with CPU time of only ~1.7 s over that span — i.e. it was network/server-bound,
  not stuck in client code. It was killed rather than left indefinitely, per instruction, once it had
  clearly blown past every other measurement in this table by an order of magnitude with no end in sight.
  A 90 s per-request `httpx` timeout on a separate attempt of the same query did **not** fire — the server
  evidently trickles bytes steadily enough that no single read stalls past the read-timeout, so total
  duration is effectively unbounded from the client's perspective.
- For scale: **even restricting to a single URL** (`matchType=exact`, the bare apex, no collapse, no
  limit) cnn.com alone returns **72.2 MB / 1,042,676 rows** in 28.47 s. The full `matchType=domain` set
  (every crawled subpath across 26 years of a top-tier news site) is plausibly many times larger again.

**Conclusion: uncollapsed client-side bucketing over `matchType=domain`, as literally specified, is
unmanageable for the worst-case domain this spike was told to test against.** This does not mean every
domain behaves this way — most expired/dropped candidates will have vastly fewer captures than CNN — but
the brief specifically asked for the worst case, and the worst case fails hard.

---

## 4. Strategy verdict

**Query strategy decided: NEITHER (a) NOR (d) AS SPECIFIED. STOP — refuted, per brief Step 9.**

Both trigger conditions in Step 9 are met simultaneously:

1. **Uncollapsed payloads are unmanageably large** — confirmed in §3 (unbounded query didn't complete in
   257+ s; the single-URL equivalent alone is 72 MB).
2. **`from=` does not reliably bound the tail** — confirmed in §2, query D (truncates 6 days into a
   2.5-year window).

A third, unanticipated failure mode was also found: **negative limit**, which the design doc treated as
roughly interchangeable with `from=` under option (d), fails in a *different* way — it reliably reaches
recent timestamps but returns content that is not representative of the domain (a single arbitrary
subresource under `matchType=domain`). So both halves of "(d)" — the `from=` variant and the negative-limit
variant — fail, each for a different reason.

**Per the brief: this is a legitimate BLOCKED outcome, not a failure of the spike.** I have not adopted an
alternative. The one candidate that measured cleanly on every axis — `matchType=exact` (apex, possibly
+`www.`) with **server-side** `collapse=timestamp:6` (§2, §1 rows "exact-collapse"/"exact-collapse-www") —
is recorded here as an observation for the owner's evaluation, **not as a decision**:

- It fixes collapse semantics completely (single urlkey per query, so adjacent-row grouping is legitimate).
- It sidesteps the truncation question entirely, because collapsing shrinks 1M+ raw rows down to ~311 —
  nowhere near any row-count cap, so there is no cliff to fall off.
- Its cost is a **predictable, one-time server-side computation** (~16–28 s per apex query for the absolute
  worst-case domain; most candidates will be much faster), not an unbounded client-side download.
- Its tradeoff, already named in the design doc as option (b)'s cost, is **loss of deep-subdomain/subpath
  history** — whether that loss is acceptable for the shape computation is a design question for the owner,
  not something this spike should decide.

This candidate was *not* independently re-verified across a broad domain set, and the `www.` vs. apex
identity (§6) is unexplained — both are reasons it should be evaluated, not adopted, before Tasks 2–11
proceed.

---

## 5. GSB (objective 2): NOT YET RUN

`.env` does not exist at the time of this spike; `GOOGLE_SAFE_BROWSING_API_KEY` is unset. No GSB request
was made — there is nothing to report for the empty-batch shape (`{}`), the match shape, or the
empty-`platformTypes` silent-false-clean check. This should be completed as a follow-up once the key lands;
it does not block or bear on the CDX strategy verdict above (the two legs are independent per the design
doc's architecture).

---

## 6. Surprises

1. **`matchType=exact` on `www.cnn.com` returned byte-identical results to the bare apex `cnn.com`** — same
   311 rows, same timestamps down to the second. Either archive.org's "exact" match canonicalizes away the
   `www.` prefix for this domain, or this is a coincidence of how cnn.com's crawls happen to be recorded.
   Unresolved; worth checking against a domain where `www.` and apex are known to differ before relying on
   it.
2. **Query A (the spec's original suspect query) actually reached the tail** (newest 2026-07-01), which is
   the opposite of what the "does row truncation drop the newest captures" framing predicted. The reason
   turned out to be domain-specific luck: cnn.com's root urlkey (`com,cnn)/`) sorts alphabetically before
   any deeper path and is itself a long-lived, frequently-crawled URL, so its own monthly-collapsed captures
   span the full 26 years before the row budget is exhausted by other URLs. This is not something the
   pipeline could depend on for an arbitrary domain — it happened to work here for a structural reason that
   won't generalize to domains whose homepage isn't the dominant, earliest-sorting urlkey.
3. **Negative limit under `matchType=domain` is 100% one static JS asset**, not a spread of pages — a much
   more severe degeneracy than expected. It answers "is timestamp X reached" correctly while being
   completely useless for `HistoryShape`.
4. **The tool sandbox silently drops these network calls** rather than erroring — a first attempt hung with
   zero output for several minutes before being killed and re-run with `dangerouslyDisableSandbox: true`,
   which is why some of this spike's early measurements look like non-responses in the raw process history.

---

## 7. Fixtures written

All under `tests/fixtures/`, in the `[header_row, data_row, data_row, ...]` CDX-JSON shape, `fl` =
`timestamp,statuscode,mimetype,digest` (4 columns; no `urlkey` — that field was only needed for this
spike's own diagnosis of collapse contamination, not for the shape computation itself).

- **`cdx_longlived.json`** — real `cnn.com` data, **every 4th row** of the `matchType=exact` + apex +
  `collapse=timestamp:6` capture (§1, row "exact-collapse", 311 rows), downsampled to 78 data rows while
  preserving the full 2000-06-20 → 2026-05-01 span. Chosen because it is the cleanest real capture
  available (small, no cross-URL contamination) — **not** an endorsement of the query strategy, since that
  question is still open (§4). Downsampling method (every 4th row) is recorded here for auditability.
- **`cdx_never_archived.json`** — real, unmodified response for the invented name `zqvoblitherox.com`:
  the literal bytes `[]`. Confirms the empty case is a bare empty array, not a header-only response.
- **`cdx_tail_flip.json`** — **real data, not synthetic** — the final 80 rows of the real `theglobe.com`
  capture (§1 flip-candidate scan). Shows a genuine late-life divergence (tail digest-churn 0.5 vs.
  lifetime 0.15 within the trimmed fixture, ratio 3.33), found rather than hand-built, per the brief's
  preference for a real example when one turns up.

`gsb_empty.json` / `gsb_match.json` — **not created**; objective 2 did not run (§5).

---

## 8. What's next

This spike is **BLOCKED** pending owner re-planning of the CDX query strategy (brief Step 9). Recommend the
owner review §4's candidate (`matchType=exact` + server-side collapse) alongside the two open questions in
§6 (the `www.`/apex identity, and whether losing subdomain-path history is acceptable) before Tasks 2–11
proceed. GSB (objective 2, §5) can proceed independently and does not need to wait on this decision.

---

## Part 2 — Safe Browsing + live end-to-end confirmation (2026-07-19)

**Context:** Phase 5b's code is complete and reviewed (282 tests pass, 3 skipped, confirmed again at
the end of this session with no regressions). `.env` now has a `GOOGLE_SAFE_BROWSING_API_KEY`. This
session exercises the assembled system against real APIs — Safe Browsing (never run before this
session) and a live end-to-end pass over `toxicity.screen()` against real Wayback CDX data.

> **BLOCKING FINDING — Safe Browsing API access is non-functional for the configured key.**
> Every real call to `threatMatches:find` this session returned **HTTP 403**, not a successful
> response. Two different real error bodies were observed across four raw attempts (spaced ~2
> minutes apart, to rule out enablement-propagation lag per Google's own suggestion in the error
> text):
> - `reason: API_KEY_SERVICE_BLOCKED` — "Requests to this API ... are blocked" (an API restriction
>   on the key itself excludes Safe Browsing), and
> - `reason: SERVICE_DISABLED` — "Safe Browsing API has not been used in project ... or it is
>   disabled. Enable it by visiting
>   `https://console.developers.google.com/apis/api/safebrowsing.googleapis.com/overview?project=<redacted>`
>   ..."
>
> The key format itself is well-formed (39 chars, `AIza` prefix, no stray whitespace/quoting from
> `.env` parsing) — this is **not** a key-typo or `load_dotenv()` bug. It is a **Google Cloud Console
> configuration problem on the owner's project**: the Safe Browsing API needs to be enabled for the
> project backing this key (and/or the key's API-restriction allowlist needs Safe Browsing added).
> This is outside what a coding session can fix — it needs the owner to act in Cloud Console. No
> project number or the key itself is recorded here beyond what Google's own error text already
> names; the key was never printed at any point in this session.
>
> **Consequence:** every domain screened this session had its GSB leg fail, so every live end-to-end
> verdict was pulled to `unknown_error` (see Part 2's precedence note under B2 below) rather than
> whatever it would otherwise have been. **This degrades safely** — `unknown_error` is never `pass`,
> so nothing this session risked a live domain silently clearing the hard-reject gate — but it also
> means the gate is **not currently functional** for its actual purpose, and Objective 2 (A1–A4)
> could not be completed against real successful GSB responses.

### Part A — Safe Browsing spike results

**A1 (clean-batch shape) and A2 (match shape): BLOCKED.** Both raw calls (ordinary domains for A1;
Google's official test URL `http://malware.testing.google.test/testing/malware/` for A2) returned
403 as described above, not a `{}` or a `matches` payload. No genuine clean-batch or match-shape
response was ever received.

**A3 (URL echo format — the highest-risk check): UNTESTABLE this session, NOT confirmed either
way.** `GsbClient.check(["malware.testing.google.test"])` was run against the real endpoint; it
raised (folded into `unknown_error`, same as every other domain) because the underlying `_find` call
403'd. **The specific question — whether Google echoes `threat.url` in a form containing `//<domain>/`
so `GsbClient._find`'s substring match actually fires — remains OPEN.** This is distinct from, and
does **not** resolve, the code's existing assumption; it simply could not be exercised against real
data this session. It must be re-verified (rerun this exact A2/A3 pair) the moment the API access
issue above is fixed, **before** relying on GSB's hard-reject leg in production.

**A4 (empty-`platformTypes` guard):** Not exercised against a live response for the same reason —
there was no successful response to compare against. The pre-send guard in `GsbClient._find` (refusing
to send when any of `threatTypes`/`platformTypes`/`threatEntryTypes` is empty) is unit-tested and was
not touched; this session adds no new live evidence for or against it.

**A5 (fixtures):** `tests/fixtures/gsb_empty.json` / `gsb_match.json` were **NOT created**. The
instruction was to save them from the *real* A1/A2 responses; no real success response exists to save
without fabricating one, which would misrepresent verified data as observed. Left absent, exactly as
Part 1 left them, but now for a different, more specific reason (API access blocked, not "key absent").

### Part B — Live end-to-end confirmation

All CDX calls went through the real `toxicity.CdxClient` against `https://web.archive.org/cdx/search/cdx`
via `domainscout.ingest.make_client()`. Because GSB is blocked (above), several checks below use a
**stub `GsbClient`** that simulates a *working* GSB returning "not listed" — this is called out
explicitly at each use; it isolates the real, unmodified CDX/`decide()` logic against real Wayback
data so the invariants could still be checked honestly, without faking GSB's own behavior or silently
patching production code.

**B1 — `cnn.com` (long-lived domain).** `python -m domainscout screen --domain cnn.com --json`
(real GSB, which 403'd) produced a fully populated `HistoryShape`: lifetime span **26.03 years**
(2000-06-20 → 2026-07-01), 311 monthly-sampled captures, `digest_churn=0.637`, `status_mix`
`{2xx: 189, 3xx: 106, other: 16}`. A tail block **was** produced (24 captures, 2024-08 → 2026-07,
span 1.91y) with a real divergence: `churn_ratio=0.589`, `status_shift=-0.274`,
`mime_shift=-0.361`, `captures_per_year_ratio=1.05`. Numbers land close to Part 1's `exact-collapse`
measurement (311 rows), as expected — same query strategy, one day later. Verdict was `unknown_error`
(GSB leg failed), not a reflection of the CDX leg, which succeeded cleanly.
**Verdict: PASS — HistoryShape mechanics work correctly against real, heavily-crawled data.**

**B2 — `qzxkvbnmplkjhgfd.com` (never-archived name).** Two runs, for the reason above:
- **Real, unmodified CLI** (`--no-cache`, real GSB): returned `verdict=unknown_error`, `history=null`.
  This is because `decide()`'s precedence puts `errors` ahead of `shape is None` — a GSB failure masks
  what would otherwise be `unknown_no_history`. This is the *actual* current behavior of the deployed
  system while the GSB key is broken.
- **Stub-GSB isolation** (real `CdxClient` against real archive.org, GSB replaced with a stub that
  returns "not listed" for everything, i.e. what a *working* GSB would return for this domain):
  `verdict=unknown_no_history`, `reason="no wayback captures - absence of evidence, not evidence of
  anything"`, `history=None`.
**Verdict: the ratified invariant HOLDS in the code — confirmed against real Wayback data, with GSB
neutralized because it cannot currently succeed for real.** It is **not yet confirmed** through the
literal unmodified end-to-end path, because that path cannot currently produce anything but
`unknown_error` for *any* domain until the Part-A blocker is fixed. Either way, the specific failure
mode the brief worried about (a never-archived name silently reading as `pass`) **did not occur** in
any run this session.

**B3 — cache hit fidelity.** Ran with real `CdxClient` (instrumented with a call-counter, not just
timing) + stub GSB (so the verdict is cacheable — a real GSB failure produces `unknown_error`, which
`VerdictCache.put` never persists by design, so the cache-hit path would otherwise be unreachable
right now):
- **RUN1 (miss):** 28.53 s elapsed, `cdx.fetch` called once (two real GETs inside it), `verdict=pass`.
- **RUN2 (hit, fresh `VerdictCache` object reloaded from disk):** **0.0 s** elapsed, `cdx.fetch`
  called **zero** times.
- `history` JSON blocks: **identical** between the two runs. `gsb_currently_listed`: **identical**
  (`false`/`false`). `history` on the hit run is **non-null**, populated exactly as on the miss.
**Verdict: PASS — the final-review cache-hit-fidelity fix holds under real data; no regression.**

**B4 — apex/www question.** Scanned live HTTP redirect behavior first to find genuinely diverging
pairs (`python.org` apex 301→www, `www.python.org` 200; similar asymmetric patterns confirmed for
7 more modern domains). Then queried real CDX for apex vs. `www.` on **15 real domains total this
session** (`python.org`, `stackoverflow.com`, `npmjs.com`, `wordpress.com`, `medium.com`, `eff.org`,
`archive.org`, `github.com`, plus the 6 defunct-dotcom domains from Part 1's flip-scan re-tested here:
`webvan.com`, `kozmo.com`, `boo.com`, `flooz.com`, `theglobe.com`, `pseudo.com`) — **every single one**
returned **byte-identical** `(timestamp, digest)` series for apex vs. `www.`, matching Part 1's
`cnn.com` "surprise" (§6.1). Combined with `cnn.com`, that is **16/16 real domains with zero observed
apex/www divergence** under `matchType=exact` + server-side collapse. **This resolves Part 1's open
question #6:** archive.org's CDX appears to canonicalize `www.`/apex to the same record for `exact`
match, at least for every domain tested across two sessions — this is not a coincidence specific to
`cnn.com`. **Practical implication:** `CdxClient`'s two-GET-per-domain merge has not, in 16 real
domains, actually merged two *different* series — every "merge" so far has deduped down to exactly
the single-host row count. `python.org` **did** show a real `2xx`/`3xx` mixture (219/69/26 in
lifetime, 18/6 in tail) — but since apex and `www.` were identical, this mixture is **intrinsic to
one canonical series** (the archive genuinely recorded the same URL as both a 200 and a 301 at
different crawl times), **not** the two-host alternation contamination the code comments describe.
**No case of the specifically-described contamination mechanism was observed.** This doesn't prove it
can't happen — 16 domains is not exhaustive, and the code's defensive two-GET design is cheap
insurance — but the originally-suspected failure mode did not materialize in any real sample.

**B5 — cp1252 / scheduled-task path.** `PYTHONIOENCODING=cp1252 python -m domainscout screen
--domain python.org --no-cache > out.txt 2>&1` (non-`--json`, full human-readable output, real GSB
403 included) → **exit code 0**, no `UnicodeEncodeError`, no traceback. Confirmed structurally too:
both `domainscout/toxicity.py` and `domainscout/commands.py` are 100% ASCII source (checked
programmatically), which is exactly why this passes.
**Verdict: PASS — no encoding regression found.**

**B6 — CDX request counts / latency / errors, this session.** Roughly 44 real CDX GET requests
across B1–B4's domains (raw counts, not amplified — `CdxClient.fetch` issues 2 GETs/domain; the B4
per-host scans issued 2 more each). **Zero 429s, zero 5xx** observed, consistent with Part 1's "not
yet hit, not confirmed absent." Latency ranged from **~0.5 s** (never-archived domains, and low-traffic
apex/www pairs like `npmjs.com`) up to **16.15 s** for a clean single-shot GET (`www.wordpress.com`,
re-measured directly to isolate from retry logic) — and **36.2 s** for the *wrapped* (`CdxClient._get`,
which includes automatic retry+backoff) call to the same host, which is higher than any single-shot
figure measured on the same domain moments later (14.5 s / 16.2 s apex/www). This is consistent with
one attempt landing close to the configured `cdx_timeout=20.0` and triggering a retry — plausible, not
directly captured via a timeout-event log, so recorded as an inference, not a confirmed cause. Either
way: **the configured 20 s `cdx_timeout` sits close enough to real observed single-request latency for
a busy modern site that an occasional retry under load would not be surprising**, which the design
already tolerates (`cdx_max_retries=3`) but is worth having on record as measured, not assumed.

### Part 2 summary

| Check | Verdict |
|---|---|
| A1 clean-batch shape | BLOCKED — 403, no real response captured |
| A2 match shape | BLOCKED — 403, no real response captured |
| **A3 URL echo format (highest-risk)** | **UNTESTABLE — GSB access itself is blocked; the echo-format question is still open, not resolved either direction** |
| A4 empty-platformTypes guard | Not exercised live (no successful response to test against) |
| A5 fixtures | Not created — no real success data exists to save |
| B1 long-lived domain | PASS — real HistoryShape correctly populated |
| **B2 never-archived domain** | **Invariant holds in the code (confirmed via real-CDX + stub-GSB isolation); NOT yet confirmed via the literal unmodified live path, which currently degrades to `unknown_error` instead because of the GSB blocker — never `pass`, so no unsafe outcome occurred** |
| B3 cache hit fidelity | PASS — miss/hit payloads agree, hit makes 0 network calls |
| B4 apex/www question | Real mixing observed (`python.org`) is intrinsic to one series, not host-merge contamination; 16/16 real domains showed apex≡www, resolving Part 1's open question |
| B5 cp1252 path | PASS — exit 0, no UnicodeEncodeError |
| B6 CDX limits | 0 429/5xx across ~44 requests this session; latency 0.5–16.2 s single-shot, one 36.2 s wrapped/retry-inflated outlier |

**Overall: the toxicity gate's CDX/history-shape half is confirmed working correctly against real
data, including the cache-hit-fidelity fix. The GSB/hard-reject half cannot be confirmed at all this
session — it needs a Google Cloud Console fix (enable the Safe Browsing API / adjust API-key
restrictions for the project behind the current key) before A1–A4 can be completed, and A3 in
particular must be re-run and resolved before the gate is trusted to catch a real listed domain.**
