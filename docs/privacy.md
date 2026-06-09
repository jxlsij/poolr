# Poolr Beta Privacy Notice

Poolr stores Telegram identity fields needed to run markets: Telegram user id, username, first name, market activity, bets, deposits, payouts, withdrawals, disputes, and admin audit fields.

Poolr validates Telegram Mini App `initData` on the backend. Raw initData, bot tokens, hashes, payment charge ids, and secrets must not be logged in full.

Public feed markets are app-origin markets. Group and private-chat markets are not listed in the global feed, but direct market detail is public-by-link for authenticated Telegram Mini App users. Treat group market questions as shareable with anyone who has the link until stricter access control is added.

Manual payout requests store TON wallet addresses and admin action metadata. Never send private keys or seed phrases to Poolr.
