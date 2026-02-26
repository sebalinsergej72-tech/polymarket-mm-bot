# polymarket-mm-bot

Streamlit market-making bot for Polymarket CLOB with inventory guardrails and Railway-ready deployment settings.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Required environment variables

- `PRIVATE_KEY` - private key (64 hex, optional `0x` prefix).
- `SIGNATURE_TYPE` - one of `0`, `1`, `2`.

## Optional environment variables

- `FUNDER_ADDRESS` - required when `SIGNATURE_TYPE=1` or `2`.
- `WALLET_ADDRESS` - wallet for portfolio/PnL pull from Data API. If omitted, bot tries `FUNDER_ADDRESS` then signer address.
- `ENABLE_SELL_INVENTORY_GUARD` - `true/false`, default `true`.

## What is improved

- SELL inventory guard: bot avoids placing SELL if available inventory is insufficient.
- Buy-side collateral checks from CLOB balance/allowance.
- Position and exposure risk limits from UI controls.
- Portfolio metrics in UI: collateral, portfolio value, cash/realized PnL, cycle health.
- Retries/backoff for market-data requests and non-blocking logs.

## Railway production profile

Repository includes `railway.toml`:

- healthcheck path: `/_stcore/health`
- restart policy: `on_failure`
- headless Streamlit start command

Also keep one instance only (no horizontal scaling), otherwise multiple bot instances can quote against each other.

## Safety notes

- Bot cancels/replaces quotes every cycle.
- Start with low `order_size`, strict notional limits, and high `refresh_sec`.
- Validate on tiny size before increasing exposure.
