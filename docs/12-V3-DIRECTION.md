# V3 Direction — Probes for Small Stores, Stats for Big Ones

**Status:** Agreed direction 2026-07-17. Announce with v3 after App Store approval.

---

## 1. The Problem V3 Solves

CheckoutGuard's current detectors are statistical: they compare a live window against a
baseline. That works when the baseline has volume. A 20-orders-per-day store is
statistically dead at the order level — no threshold on order counts can distinguish
"broken checkout" from "slow Tuesday" fast enough to matter.

Two consequences drive the v3 design:

1. **Move upstream where the volume is.** The same 20-orders/day store has ~80
   checkout-starts and thousands of sessions per day. Degradation shows up in those
   denser signals hours before order counts can prove anything.
2. **For the smallest stores, even upstream signals are thin.** They need an active
   canary — a synthetic probe that walks the checkout and verifies it works — not
   passive statistics.

## 2. Product Framing (segmentation)

> Probes for small stores, stats for big ones. Same dashboard.

- Big stores get statistical detection nearly for free (their own data is dense);
  they are not the paying core.
- Small stores cannot self-detect breakage from their stats — they are exactly the
  segment that pays for the canary.
- This shapes pricing/ICP: the probe is the flagship feature of the paid tier for
  low-volume merchants; the statistical suite is the volume-tier feature.

## 3. Feature A — CUSUM Slow-Bleed Detector (implemented)

Single-window thresholds miss slow bleed: 25–35% down never trips a 50% threshold, in
any window, ever. CUSUM accumulates deviation across hours instead of judging each
window alone: "running ~30% under expectation for ~6 straight hours" fires even though
no single hour looks alarming.

Implementation (detector #7, `slow_bleed` — `services/detector.py`):

- **Signal:** checkout-starts per completed hour (`checkout_created` events) — the
  densest signal we already ingest via webhook. Orders remain the slow confirmation.
- **Expectation:** 28-day same-weekday, ±1h hour-band baseline (same approach as the
  order-silence baseline), falling back to a 7-day hour-band average.
- **Statistic:** one-sided CUSUM on the shortfall ratio, updated once per completed
  hour: `S += max(0, (1 - SLACK) - observed/expected)` when under, decays by
  `RECOVERY_DECAY` when at/above expectation. `SLACK = 0.15` ignores normal jitter.
- **Alert:** `S >= 0.90` — calibrated so ~30% under expectation sustained for ~6 hours
  fires (6 × (0.30 − 0.15) = 0.90). A severe 65%+ collapse still fires in ~2 hours via
  accumulation, but the fast detectors (#1/#2) own that case anyway.
- **Resolve:** `S <= 0.20` closes the incident (reuses the incident lifecycle).
- **Sparse-store guard:** skip hours where expected < 1.0 checkout-starts/hour.
- State lives on `merchants` (`cusum_stat`, `cusum_updated_at`) — migration
  `008_cusum_slow_bleed.sql`.

## 4. Feature B — Synthetic Payment-Render Probe (spec, build next)

The canary for small stores. A headless session walks: product → add to cart →
checkout → shipping → **assert the payment section renders**. It never completes a
purchase — no orders created, no payment fraud systems triggered. This catches broken
themes, app conflicts, and checkout-extension failures (~90% of silent breakage).

Design constraints (agreed):

- **Cadence:** every 15–30 min per store; rotate a real product; behave like a browser
  (real UA, cookies, human-ish pacing).
- **Stop at payment-render.** Never submit payment in production.
- **Analytics suppression is a launch blocker, not a nice-to-have.** Synthetic
  sessions fire the merchant's GA4/Meta pixels and corrupt their conversion + ad
  optimization data — a churn/liability event. Approach, in order of preference:
  1. Shopify-recognized bot signals (known-bot UA) so Shopify analytics tags the
     session as bot traffic;
  2. a probe query param + documented GA4/Meta exclusion filter created during
     onboarding;
  3. block pixel network requests at the browser layer (`googletagmanager`,
     `connect.facebook.net`, etc.) as defense-in-depth.
  Must be validated on a test store before any production probe runs.
- **Infra:** Playwright container beside the API on the VPS; per-merchant probe config
  (product handle, enabled flag, cadence) on `merchants`; probe outcomes recorded as
  events so probe failures flow through the same incident + alert pipeline.
- **Failure semantics:** N consecutive probe failures (N=2) → incident
  `probe_checkout_broken`; a success resolves. Single failures are logged, not alerted
  (transient CDN/theme hiccups).

The other 10% (actual payment processing) is intentionally out of scope for
production: a periodic full run against a dev store with the test gateway covers the
mechanics; gateway-side outages surface via the order-silence detector.

## 5. Rollout

1. **Now:** Feature A ships in the codebase behind normal detector flow (additive,
   no schema-breaking changes; safe while App Store review is pending).
2. **After app approval:** announce v3 = slow-bleed detection + synthetic probe.
3. Probe build starts as its own increment: infra + analytics-suppression validation
   on a test store first, then per-merchant rollout.

## 6. Explicitly Not Doing

- Alerting on add-to-cart rate now — clean ATC needs the Web Pixels extension;
  checkout-start is already ingested and is dense enough. ATC can be added later as a
  second CUSUM signal with zero design changes.
- Completing purchases in production probes — never.
- Widening single-window thresholds to "help" small stores — that only trades misses
  for false alarms; CUSUM + probe is the correct split.
