# How competitions are scored

This guide describes behavior when the relevant feature and account are enabled. Check the [dated live evidence](../../../AGENTS.md#current-access-and-live-evidence) before relying on its availability.

The scoring rules use daily returns after adjusting for deposits and withdrawals. Each competition publishes its scoring method, parameters and eligibility thresholds before entries open. A rule uses the lower of the scores for the full set of days and the set after trimming the best days.

## ras_t

The default method scales average daily return by volatility and the square root of the number of observations. The volatility floor prevents very low measured volatility from dominating. Both positive and negative swings count toward volatility.

## sortino_t

This method scales average daily return by downside volatility and the square root of the number of observations. It uses the same volatility floor. Positive swings do not increase downside volatility.

## return_over_mdd

This method divides total log return by maximum drawdown in log-return terms, subject to a drawdown floor. It rewards return earned with a smaller peak-to-trough loss.

## return_capped

This method ranks entries by total log return. The competition's maximum drawdown cap still applies as an eligibility threshold.
