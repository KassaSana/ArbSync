# Live soak report - 2026-09-05

> Historical evidence note: This report predates ARB-002. Binance.US subscribed to USDT
> markets, but the then-current normalizer rendered them as `*-USD`; these legacy labels
> do not establish USD/USDT parity.

- Started: `2026-09-05T22:05:04.921390+00:00`
- Ended: `2026-09-05T22:10:04.976535+00:00`
- Requested duration: `300.0s`
- Actual duration: `300.0s`
- Sample interval: `5.0s`
- Successful samples: `57`
- Ready samples: `55/57` (96.5%)
- HTTP failures: `4`
- Opportunities observed: `0`
- Background task failures: `0`

## Adapters

| Exchange | Events during run | Reconnects | Gaps | Max message age | Last error |
| --- | ---: | ---: | ---: | ---: | --- |
| binance | 1760 | 0 | 0 | 874 ms | - |
| coinbase | 29001 | 0 | 0 | 61 ms | - |
| gemini | 2042 | 0 | 0 | 1010 ms | - |

## Book eligibility

| Book | Eligible samples | Ineligible reasons | p95 age | Max age | Recoveries |
| --- | ---: | --- | ---: | ---: | --- |
| binance:AAVE-USD | 57/57 | - | 5282 ms | 9579 ms | - |
| binance:AVAX-USD | 57/57 | - | 4718 ms | 10469 ms | - |
| binance:BTC-USD | 57/57 | - | 2063 ms | 2938 ms | - |
| binance:DOT-USD | 55/57 | too_old=2 | 23766 ms | 38453 ms | 10.1s |
| binance:ETH-USD | 57/57 | - | 1750 ms | 2438 ms | - |
| binance:LINK-USD | 57/57 | - | 4609 ms | 14313 ms | - |
| binance:LTC-USD | 57/57 | - | 1657 ms | 1891 ms | - |
| binance:SOL-USD | 57/57 | - | 2188 ms | 4282 ms | - |
| binance:UNI-USD | 57/57 | - | 1797 ms | 2765 ms | - |
| coinbase:AAVE-USD | 57/57 | - | 843 ms | 1203 ms | - |
| coinbase:AVAX-USD | 57/57 | - | 578 ms | 1250 ms | - |
| coinbase:BTC-USD | 57/57 | - | 79 ms | 406 ms | - |
| coinbase:DOT-USD | 57/57 | - | 750 ms | 1125 ms | - |
| coinbase:ETH-USD | 57/57 | - | 140 ms | 250 ms | - |
| coinbase:LINK-USD | 57/57 | - | 454 ms | 735 ms | - |
| coinbase:LTC-USD | 57/57 | - | 172 ms | 531 ms | - |
| coinbase:SOL-USD | 57/57 | - | 140 ms | 312 ms | - |
| coinbase:UNI-USD | 57/57 | - | 375 ms | 547 ms | - |
| gemini:AAVE-USD | 57/57 | - | 2110 ms | 3844 ms | - |
| gemini:AVAX-USD | 57/57 | - | 1891 ms | 2937 ms | - |
| gemini:BTC-USD | 57/57 | - | 953 ms | 1016 ms | - |
| gemini:DOT-USD | 57/57 | - | 14329 ms | 23703 ms | - |
| gemini:ETH-USD | 57/57 | - | 1016 ms | 1672 ms | - |
| gemini:LINK-USD | 57/57 | - | 3125 ms | 4985 ms | - |
| gemini:LTC-USD | 57/57 | - | 1047 ms | 2187 ms | - |
| gemini:SOL-USD | 57/57 | - | 1344 ms | 2843 ms | - |
| gemini:UNI-USD | 57/57 | - | 9718 ms | 24828 ms | - |

## Process memory

- Samples: `57`
- Minimum RSS: `108.80 MiB`
- Maximum RSS: `116.25 MiB`
- Mean RSS: `112.87 MiB`
- Start-to-end change: `-2.48 MiB`

## Sampling failures

- 2026-09-05T22:06:30.831775+00:00: RemoteProtocolError: Server disconnected without sending a response.
- 2026-09-05T22:06:40.878209+00:00: RemoteProtocolError: Server disconnected without sending a response.
- 2026-09-05T22:07:46.372999+00:00: ReadError: 
- 2026-09-05T22:09:06.911969+00:00: RemoteProtocolError: Server disconnected without sending a response.
