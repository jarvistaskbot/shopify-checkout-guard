# UI/UX Audit

---

## 1. Pages Overview

| Page | Route | Purpose | Auth |
|---|---|---|---|
| Onboarding | GET/POST /onboarding | First-time Slack setup | None (should require session) |
| Demo | GET /demo | App Store review simulation | None (public) |
| Privacy | GET /privacy | Privacy policy | None (public) |
| Dashboard | GET /dashboard | Incident monitoring | None (CRITICAL gap) |
| Billing success | GET /billing/callback | Post-billing confirmation | None |
| Billing declined | GET /billing/callback | Declined billing | None |
| Billing error | GET /billing/error | Error state | None |
| Billing activated | GET /billing/activated | Partner bypass confirmation | None |

---

## 2. Onboarding Page Analysis

**Purpose:** After OAuth, collect the merchant's Slack Incoming Webhook URL.

**What works:**
- Simple two-field form (Slack URL + optional email)
- Inline instructions for finding the Slack webhook URL
- Green Shopify-styled submit button
- Responsive layout (centered, max-width 560px)

**UX Gaps:**

| Gap | Impact |
|---|---|
| No Slack URL format validation (HTML5 type="url" or server-side) | Merchant saves broken URL, alerts silently fail, merchant thinks app is working |
| No "Test alert" button after save | Merchant has no confirmation their Slack is working until an incident fires |
| Shop name shown as raw domain, not store name | "my-store.myshopify.com" is technical jargon, not the merchant's business name |
| No loading state on form submit | Submit button does nothing visible for 1-2s during redirect |
| No error page if POST fails | DB error causes an unhandled 500, no user-facing error message |
| Email field label says "optional" but no indication of how it's used | Merchants may skip it not understanding value |

**Mobile:** At viewport <400px, the form layout holds but input fields may be cramped.

---

## 3. Demo Page Analysis

**Purpose:** Allows Shopify reviewers to see the app experience without a real install.

**What works:**
- Two-state flow (form → success) is easy to understand
- Clearly marked as "demo" with disclaimer note
- Simulates the actual onboarding appearance

**UX Gaps:**

| Gap | Impact |
|---|---|
| Form uses GET method (data visible in URL) | Demo data appears in browser history; minor |
| Success state copy ("30 minutes", "7-day baseline", ">50%") partially inaccurate vs v2 detection | Misleading for reviewers who test the actual app |
| No return to "App Store listing" link | Reviewer flow dead-ends |

---

## 4. Dashboard Page Analysis

**Purpose:** Incident monitoring for active merchants. The core retention-driving page.

**What works:**
- Four summary stats (checkouts, orders, conversion rate, incidents count)
- Status banner (calibrating / active / clear) with clear visual differentiation
- Incidents table with type, status badge, duration, estimated impact
- Resolved vs Active badge distinction (red/green)
- "Update alert settings" link at bottom

**UX Gaps:**

| Gap | Impact | Priority |
|---|---|---|
| No auth on the page | CRITICAL security issue | CRITICAL |
| Table breaks on mobile (<600px) | Dashboard is unusable on phones | HIGH |
| No incident detail — what caused it? | Merchants click "Checkout Funnel" and get no more info | HIGH |
| No trend chart (conversion rate over time) | Merchants can't see if they're improving | HIGH |
| No "mark resolved" button for payment_failure incidents | These never auto-resolve — stuck forever | HIGH |
| "Calibrating" state shows nothing useful | 7-day wait with no data visible is jarring | MEDIUM |
| `_fmt_dt` shows UTC timestamps | Merchants likely want their local timezone | MEDIUM |
| `estimated_revenue_loss_per_min` shown as $/hr (× 60) | Not labeled clearly — merchant may not know it's extrapolated | MEDIUM |
| No pagination on incidents table | LIMIT 50 hard-coded — merchant loses older incidents | LOW |
| No real-time refresh | Incidents require a page reload to update | LOW |
| Conversion rate: "—" when no checkouts | Could be more helpful ("No checkout activity yet") | LOW |

**Calibrating banner:** Shows "N day(s) until baseline is ready" — good. But during calibrating, all stats are still shown (checkouts, orders, conversion). Merchants might act on this data without understanding it's too early to be meaningful.

---

## 5. Billing Pages Analysis

**Success page:** Clean, shows trial end date, sets expectations. ✅  
**Declined page:** Minimal. Provides a "Try again" link. ✅  
**Error page:** Shows "contact support" with no actual support link or email. Should link to `support@checkoutguardalerts.com`. ⚠️

All billing pages have the XSS issue with `{shop}` in HTML (see AUDIT-SECURITY.md).

---

## 6. Privacy Policy Analysis

The privacy policy page is well-written and covers required elements. However:
- Claims "stored encrypted" — false (plaintext)
- Claims "retained for 30 days" — false (no cleanup jobs)
- Contact email: `support@checkoutguardalerts.com` — not referenced anywhere else; unclear if monitored

---

## 7. Theme Extension UX

**Merchant experience:** The extension is a block merchants must manually add in the Shopify Theme Editor. There's no automated activation. Steps:
1. Go to Online Store → Themes → Customize
2. Find a product or cart template
3. Add section → "CheckoutGuard Error Tracker"
4. Save

**UX gap:** No in-app guidance for adding the Theme Extension. The onboarding page doesn't mention it. Merchants may complete onboarding without ever adding the extension, causing JS error detection to never work.

**Recommendation:** Add a post-onboarding step: "Step 2: Activate error tracking. Add the CheckoutGuard block to your cart and product templates [link to Shopify Theme Editor]."

---

## 8. Non-Embedded vs Embedded Trade-off

The app uses `embedded = false` — all pages open in new browser tabs/windows, not inside the Shopify admin iframe.

**Pros:**
- Simpler development (no App Bridge SDK required)
- No cross-origin iframe complexity
- Works with any browser/context

**Cons:**
- Context switch: merchant leaves Shopify Admin to see incident dashboard
- Shopify App Store **recommends** embedded apps; reviewers may flag this
- Can't use Polaris (Shopify's design system) properly without App Bridge
- Merchant session/auth is harder without App Bridge session tokens

**Shopify's position:** As of 2024, embedded apps are strongly preferred but not strictly required. Non-embedded apps can pass review but may receive feedback to move toward embedded. For a monitoring app that merchants check infrequently, non-embedded is arguably fine UX.

---

## 9. Accessibility

- All pages use system font stack — readable ✅
- Color contrast: Shopify green (#008060) on white — passes WCAG AA ✅
- No `aria-label` on form inputs — screen readers rely on `<label for="...">` which is correctly used ✅
- `lang="en"` on all HTML documents ✅
- No keyboard trap issues (standard form behavior) ✅
- Missing: skip-to-content links, focus indicators on buttons

---

## 10. Overall UX Assessment

The UI is clean, minimal, and appropriate for an MVP. The core problem is missing functionality:

1. Dashboard has no auth (security, not UX, but blocks launch)
2. No guidance for Theme Extension activation
3. No incident detail view
4. No trend visualization
5. Payment failure incidents permanently stuck open
6. Mobile table layout broken

For a $29/month SaaS, the UI needs at minimum: auth, incident detail, mobile table, and a clear post-install flow that guides merchants through Theme Extension activation.
