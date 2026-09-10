# Live soak report - 2026-09-05

> Historical evidence note: This report predates ARB-002. Binance.US subscribed to USDT
> markets, but the then-current normalizer rendered them as `*-USD`; these legacy labels
> do not establish USD/USDT parity.

- Started: `2026-09-05T22:14:15.030306+00:00`
- Ended: `2026-09-05T22:15:45.075798+00:00`
- Requested duration: `90.0s`
- Actual duration: `90.0s`
- Sample interval: `5.0s`
- Successful samples: `19`
- Ready samples: `19/19` (100.0%)
- HTTP failures: `0`
- Opportunities observed: `0`
- Background task failures: `0`

## Adapters

| Exchange | Events during run | Reconnects | Gaps | Max message age | Last error |
| --- | ---: | ---: | ---: | ---: | --- |
| binance | 468 | 0 | 0 | 889 ms | - |
| coinbase | 8214 | 0 | 0 | 33 ms | - |
| gemini | 655 | 0 | 0 | 913 ms | - |

## Book eligibility

| Book | Eligible samples | Ineligible reasons | p95 age | Max age | Recoveries |
| --- | ---: | --- | ---: | ---: | --- |
| binance:AAVE-USD | 19/19 | - | 5047 ms | 5281 ms | - |
| binance:AVAX-USD | 19/19 | - | 5750 ms | 7937 ms | - |
| binance:BTC-USD | 19/19 | - | 2485 ms | 4156 ms | - |
| binance:DOT-USD | 19/19 | - | 16469 ms | 19000 ms | - |
| binance:ETH-USD | 19/19 | - | 1156 ms | 1953 ms | - |
| binance:LINK-USD | 19/19 | - | 6813 ms | 7563 ms | - |
| binance:LTC-USD | 19/19 | - | 1969 ms | 2516 ms | - |
| binance:SOL-USD | 19/19 | - | 1765 ms | 1781 ms | - |
| binance:UNI-USD | 19/19 | - | 2250 ms | 4781 ms | - |
| coinbase:AAVE-USD | 19/19 | - | 406 ms | 735 ms | - |
| coinbase:AVAX-USD | 19/19 | - | 484 ms | 782 ms | - |
| coinbase:BTC-USD | 19/19 | - | 188 ms | 282 ms | - |
| coinbase:DOT-USD | 19/19 | - | 500 ms | 719 ms | - |
| coinbase:ETH-USD | 19/19 | - | 94 ms | 94 ms | - |
| coinbase:LINK-USD | 19/19 | - | 594 ms | 1188 ms | - |
| coinbase:LTC-USD | 19/19 | - | 125 ms | 172 ms | - |
| coinbase:SOL-USD | 19/19 | - | 156 ms | 157 ms | - |
| coinbase:UNI-USD | 19/19 | - | 360 ms | 813 ms | - |
| gemini:AAVE-USD | 19/19 | - | 2766 ms | 5922 ms | - |
| gemini:AVAX-USD | 19/19 | - | 1922 ms | 3844 ms | - |
| gemini:BTC-USD | 19/19 | - | 875 ms | 922 ms | - |
| gemini:DOT-USD | 19/19 | - | 6813 ms | 10672 ms | - |
| gemini:ETH-USD | 19/19 | - | 875 ms | 922 ms | - |
| gemini:LINK-USD | 19/19 | - | 907 ms | 1531 ms | - |
| gemini:LTC-USD | 19/19 | - | 922 ms | 1703 ms | - |
| gemini:SOL-USD | 19/19 | - | 907 ms | 1578 ms | - |
| gemini:UNI-USD | 19/19 | - | 2844 ms | 6594 ms | - |

## Process memory

- Samples: `19`
- Minimum RSS: `101.89 MiB`
- Maximum RSS: `104.76 MiB`
- Mean RSS: `104.04 MiB`
- Start-to-end change: `+0.90 MiB`
