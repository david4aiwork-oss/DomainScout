# Phase 5b — Task 1 empirical spike: findings

**Status: BLOCKED — the plan's assumed strategy (a)+(d) is refuted by measurement. Reported for
owner re-planning per brief Step 9. Do not build Tasks 2–11 against (a)+(d) as currently specified.**

**Date:** 2026-07-18. **Domain used throughout:** `cnn.com` (the ~25-year, heavily-crawled worst case
the brief calls for), plus a small scan of real defunct-dotcom domains and two invented never-registered
names. All calls went through `domainscout.ingest.make_client()` (truststore against the OS trust store —
a bare `httpx` call fails TLS verification on this box). All network calls needed
`dangerouslyDisableSandbox: true`; without it the tool sandbox silently drops the connection (hangs with
zero output rather than raising an error) instead of failing fast.

**Objective 2 (GSB): NOT YET RUN.** `.env` does not exist yet — `GOOGLE_SAFE_BROWSING_API_KEY` is absent.
Per instruction, objective 1 was completed in full without waiting; GSB is left for a follow-up once the
key lands.

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
