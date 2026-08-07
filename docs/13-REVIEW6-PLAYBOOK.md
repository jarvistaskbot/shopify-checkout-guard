# Review #6 Playbook — September 4, 2026 (treat as final attempt)

Goal: reviewer #6 finds zero valid rejection reasons. Every failure mode
observed in reviews #1-#5 is eliminated and re-verified before submission.

## Rejection history and status

| # | Date | Cited | Root cause | Status |
|---|------|-------|-----------|--------|
| 1 | Jul 15 | 2.3.3 | root URL served JSON after install | fixed (3f028fe) |
| 2 | Jul 15 | audit | billing 422, reinstall loop, 15 other P0/P1 | fixed (48f06e6) |
| 3 | Jul 17 | 4.5.4 | onboarding Slack wall + no credential in instructions | fixed (1e0c638 skip path) |
| 4 | Jul 21 | 4.5.4 | webhook = write-only credential, reviewer could not see Slack | fixed (00bf099 alert history) |
| 5 | Aug 7 | 4.5.5 | provided Slack login non-functional (device verification code) | fixed (bb0e314 credential-free) |

Pattern: #3-#5 all died on the credentials axis. bb0e314 abolishes
credentials entirely; 4.5.4/4.5.5 apply only to apps that require them.

## FROZEN testing instructions (do not edit after Sep 1)

1. The app requires no accounts or login credentials. Authentication is
   entirely via Shopify OAuth. Install on any development store; you land on
   the CheckoutGuard onboarding page.
2. Press "Skip for now" (connecting Slack is optional).
3. Choose any plan and approve the charge (test mode on development
   stores - you will not be billed).
4. On the dashboard, press "Send test alert". The alert appears immediately
   in the "Recent Alerts" panel - click "View alert content" to see the full
   alert message and its status.
5. Optional: to also see external Slack delivery, open "Update alert
   settings", paste this webhook (no Slack account needed):
   <REVIEW_SLACK_WEBHOOK_URL - kept out of the repo; stored in the Partner
   Dashboard submission form and in the assistant's project memory>
   Press "Send test alert" again and the panel shows "Delivered to Slack".
6. Background monitoring tracks orders and checkouts via webhooks and alerts
   on anomalies after a 7-day baseline period.

No accounts or credentials are required.

Form settings: "My app doesn't require an account" checkbox = CHECKED
(now factually true). Screencast URL = updated video (see below).

## Dress rehearsal protocol (run Sep 1-3, replays all observed reviewer behavior)

On a FRESH development store, in order:
- [ ] Install via /?shop= link -> lands on onboarding (not JSON, not error)
- [ ] Press "Skip for now" -> billing plans
- [ ] Pick Scale plan -> approve test charge -> dashboard
- [ ] "Send test alert" with NO Slack -> banner "Test alert generated";
      Recent Alerts shows Generated badge + "View alert content" shows full text
- [ ] Open alert settings, enter INVALID webhook `https://hooks.slack.com/services/DEMO`
      -> test alert -> clear error banner (not a crash) [reviewer #5 did this]
- [ ] Enter REAL review webhook -> test alert -> "Delivered to Slack" in panel
      + message visible in #alerts
- [ ] Press test alert twice fast -> rate-limit banner shows countdown seconds
      [reviewer #4 died on silent refusal]
- [ ] Reinstall without uninstalling (open install link again) -> lands on
      dashboard, no onboarding loop [reviewer #4 pattern]
- [ ] Uninstall -> app/uninstalled webhook processed, no errors in logs
- [ ] Reinstall after uninstall -> billing re-prompt works (no cancelled-forever loop)
- [ ] Server-side: VPS logs clean during entire rehearsal (no tracebacks)
- [ ] Repeat identical pass on Sep 3 as final freeze check

## Listing audit (before Sep 1)

- [ ] App store listing content: no claims about features that are not live
      (no OOS, no multi-store, no email-alert claims unless SendGrid configured)
- [ ] Screencast: re-record 3-4 min matching the NEW flow exactly
      (install -> skip -> billing -> test alert -> Recent Alerts panel ->
      optional webhook -> Delivered to Slack). Old video shows obsolete flow.
- [ ] Pricing section matches live plans ($29/$79/$199/$399, 14-day trial)

## Change freeze

- Sep 1 -> review completion: NO production deploys, NO config changes,
  NO listing edits except the frozen instructions + screencast URL.
- Review Slack workspace + webhook must stay untouched (webhook verified live
  before submit).

## Submission day (Sep 4)

1. Assistant runs pre-submit verification (health, /?shop 302, skip 302,
   GDPR 401x3, webhook 200, prod HEAD matches expected commit).
2. Paste FROZEN instructions into App testing information. Save.
3. Resubmit. Notify assistant -> live log watch for reviewer session.

## Evidence file (for any dispute)

- Review #5 reviewer session trace: store i9j3i0-kj.myshopify.com, Aug 7
  18:0x-18:3x UTC; alert_deliveries rows show test alert DELIVERED 18:26 UTC.
- Suspension emails: Jul 21 (until Aug 4), Aug 7 (until Sep 4), ref 123831.
