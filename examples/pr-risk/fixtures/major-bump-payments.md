# Upgrade stripe SDK to v12 and migrate charge flow to PaymentIntents

The `stripe` Python package jumps from 7.x to 12.x. v8 removed the legacy
`Charge.create` flow we still use, so this PR migrates `billing/charge.py` to
`PaymentIntent.create` with automatic payment methods.

Changes:
- bump `stripe` in `pyproject.toml`
- `charge_customer()` now creates a PaymentIntent and confirms it inline
- amount is still passed in minor units; added a guard for zero/negative amounts
- refund path switched to `Refund.create(payment_intent=...)`

Tested manually against the Stripe test mode dashboard.
