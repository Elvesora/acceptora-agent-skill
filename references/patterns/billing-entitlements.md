# Billing, quota, and entitlement pattern

Apply to pricing, subscriptions, usage limits, plans, trials, credits, invoices, or feature access.

## Coverage prompts

- Verify displayed plan/price/currency/interval and the exact entitlement granted.
- Check free, trial, active, past-due, cancelled, expired, and grace states touched by the change.
- Exercise quota below/at/above limit and confirm fail-closed behavior where required.
- Verify webhook/event idempotency, out-of-order events, duplicate charges, and reconciliation.
- Check proration/tax/refund/invoice behavior only when in scope and safely testable.
- Confirm UI and direct API authorization enforce the same entitlement.
- Verify renewal/cancellation timing and timezone boundaries.
- Use provider sandbox data; never perform a real charge without explicit optional warning and approval.

Use billing-provider, entitlement, quota, webhook, and plan-config anchors. Treat uncertain charge/access behavior as high risk.
