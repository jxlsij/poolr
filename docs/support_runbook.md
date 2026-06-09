# Poolr Payment Support Runbook

## Intake

Ask for:

- Telegram user id.
- Market id, bet id, payout id, or withdrawal id.
- Telegram Stars charge id if available.
- Screenshot or short description of the issue.

Never ask for TON private keys or seed phrases.

## Triage

1. Check `/admin_stats` for pending disputes and withdrawals.
2. Check `/admin_withdrawals` for pending payout requests.
3. Check `/admin_disputes` for open disputes.
4. Confirm the market status and whether the 24-hour dispute window has elapsed.
5. For payment issues, reconcile `deposits.charge_id`, `bets`, `payouts`, `withdrawals`, and `ledger_entries`.

## Refunds

Use Telegram Stars refunds only for failed delivery, support-approved reversals, chargebacks, or disputes. Do not use refunds as the normal withdrawal rail.

## Manual Payouts

Pay the TON equivalent manually only after validating wallet format, amount, pending status, and admin authorization. Record the TON transaction hash through the admin callback.

## Escalation

Pause withdrawals if ledger entries, deposits, payout holds, or admin notifications disagree. Do not launch public paid traffic until legal/compliance review is complete.
