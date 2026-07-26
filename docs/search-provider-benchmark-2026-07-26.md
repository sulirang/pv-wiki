# Exa vs Tavily retrieval probe — 2026-07-26

This directional probe used current products from the read-only GTI catalogue.
No API key or response body was persisted.

## Method

- Five brands and explicit catalogue models:
  - FoxESS `KH8`
  - SAJ `R5-5K-S2-15`
  - Huawei `SUN2000L-4.6KTL-L1`
  - Sungrow `SG50CX-P2`
  - LONGi `LR5-72HGD-585M`
- One identical query per provider and model:
  `manufacturer model datasheet technical specifications official`
- Five results requested.
- Exa used Search `type=auto` with bounded highlights.
- Tavily used advanced Search with three chunks per source.

## Results

| Measure | Exa | Tavily |
| --- | ---: | ---: |
| Mean search latency | 0.58 s | 2.94 s |
| Official-domain results in the top five | 16/25 | 14/25 |
| Queries with an official result at rank 1 | 4/5 | 3/5 |
| Queries with any official result | 4/5 | 5/5 |
| PDF-like results | 21/25 | 16/25 |
| Returned evidence characters | 71,735 | 50,090 |

Exa put an official datasheet first for FoxESS, SAJ, and Sungrow. Both
providers put an official Huawei specification page first. The strict
continuous-string metric undercounted Huawei because the official document
represents the model as a family range.

LONGi was the counterexample: Exa's first five results were third-party PDF
copies, while Tavily returned an official LONGi PDF at rank four. The worker's
existing local source authorization remains mandatory, so a high-ranking result
never becomes trusted evidence merely because Exa returned it.

## Decision

PV Wiki now uses Exa as its sole bounded Search and Contents provider. The
worker keeps its multi-round supplemental search, extracted-URL binding,
manufacturer-domain validation, exact evidence quoting, and fail-closed
publication gates. Query construction explicitly asks for official
manufacturer documents, and missing official evidence produces another bounded
search or a non-publish outcome instead of trusting a mirror.

Relevant Exa contracts:

- <https://exa.ai/docs/reference/search>
- <https://exa.ai/docs/reference/get-contents>
