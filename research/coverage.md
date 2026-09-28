# Data coverage for H1-H3 (research window, holdout excluded)

Generated 2026-09-28 by `pit.coverage()`. Rows are available-at times; the holdout (2026-01-01 on) is locked and not shown.

| series | coin | start | end | rows | expected_step | max_gap | gaps_over_3_steps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| binance futures 1h | BTC | 2020-01-01 | 2025-12-31 | 52607 | 1:00:00 | 0 days 01:00:00 | 0 |
| binance spot 1h | BTC | 2019-09-01 | 2025-12-31 | 55500 | 1:00:00 | 0 days 06:00:00 | 5 |
| binance futures 5m | BTC | 2021-03-01 | 2025-12-31 | 508895 | 0:05:00 | 0 days 00:05:00 | 0 |
| deribit dvol | BTC | 2021-03-24 | 2025-12-31 | 41855 | 1:00:00 | 0 days 01:00:00 | 0 |
| quarterly basis (daily) | BTC | 2021-02-04 | 2025-12-31 | 1708 | 1 day, 0:00:00 | 13 days 00:00:00 | 10 |
| binance funding | BTC | 2019-09-10 | 2025-12-31 | 6914 | 8:00:00 | 0 days 08:00:00.047000 | 0 |
| binance open interest | BTC | 2021-11-01 | 2025-12-31 | 437694 | 0:05:00 | 0 days 10:30:00 | 22 |
| bybit funding | BTC | 2020-03-25 | 2025-12-31 | 6322 | 8:00:00 | 0 days 08:00:00 | 0 |
| bybit open interest | BTC | 2020-07-20 | 2025-12-31 | 47416 | 1:00:00 | 15 days 00:00:00 | 1 |
| binance futures 1h | ETH | 2020-01-01 | 2025-12-31 | 52607 | 1:00:00 | 0 days 01:00:00 | 0 |
| binance spot 1h | ETH | 2019-11-01 | 2025-12-31 | 54036 | 1:00:00 | 0 days 06:00:00 | 5 |
| binance futures 5m | ETH | 2021-03-01 | 2025-12-31 | 508895 | 0:05:00 | 0 days 00:05:00 | 0 |
| deribit dvol | ETH | 2021-03-24 | 2025-12-31 | 41855 | 1:00:00 | 0 days 01:00:00 | 0 |
| quarterly basis (daily) | ETH | 2021-02-05 | 2025-12-31 | 1707 | 1 day, 0:00:00 | 13 days 00:00:00 | 10 |
| binance funding | ETH | 2019-11-27 | 2025-12-31 | 6680 | 8:00:00 | 0 days 08:00:00.047000 | 0 |
| binance open interest | ETH | 2021-12-01 | 2025-12-31 | 429347 | 0:05:00 | 0 days 10:30:00 | 9 |
| bybit funding | ETH | 2020-10-21 | 2025-12-31 | 5693 | 8:00:00 | 0 days 08:00:00 | 0 |
| bybit open interest | ETH | 2020-10-21 | 2025-12-31 | 45541 | 1:00:00 | 0 days 01:00:00 | 0 |
| binance futures 1h | SOL | 2020-09-14 | 2025-12-31 | 46312 | 1:00:00 | 3 days 01:00:00 | 2 |
| binance spot 1h | SOL | 2020-09-01 | 2025-12-31 | 46732 | 1:00:00 | 0 days 05:00:00 | 3 |
| binance funding | SOL | 2020-09-13 | 2025-12-31 | 5881 | 8:00:00 | 0 days 08:00:00.047000 | 0 |
| binance open interest | SOL | 2021-12-01 | 2025-12-31 | 429340 | 0:05:00 | 0 days 10:30:00 | 11 |
| bybit funding | SOL | 2021-06-29 | 2025-12-31 | 5300 | 8:00:00 | 0 days 08:00:00 | 0 |
| bybit open interest | SOL | 2021-06-29 | 2025-12-31 | 39518 | 1:00:00 | 0 days 01:00:00 | 0 |

Notes: Binance 5-minute OI has zero-valued days (treated as missing); the quarterly basis has ~13-day gaps at some contract rolls (treated as missing after 2 days); SOL has no quarterly futures and no DVOL.
