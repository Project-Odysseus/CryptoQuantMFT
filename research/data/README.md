# Research reference data

- `fomc_statements.csv`: scheduled FOMC statement dates 2021-2027, parsed on 2026-09-28 from
  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (the second day of each meeting; `*` on the Fed page
  marks meetings with economic projections). Statements are released at 14:00 New York time (`statement_utc`).
  Notation votes without a scheduled statement are excluded.
- CPI release dates are **not** included: bls.gov refused scripted requests (HTTP 403) and FRED/ALFRED timed out from
  this network. Add them by hand from https://www.bls.gov/schedule/news_release/cpi.htm (08:30 New York time) to
  complete H2's news split.
