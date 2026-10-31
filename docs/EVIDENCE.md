# Evidence pack (sourced 2026-10-31)

Confidence: **H** = primary/reputable source checked; **M** = trade press / vendor; **L** = secondary or not cross-checked. Items marked NOT VERIFIED must not be presented as fact.

## Status quo cost (supplier side)
- **SME factoring: ~1–5% per month (typ. ~2.5%)** → ~30% effective APR (2.5%/30 days, Corpay) up to ~57% APR (4%/4 weeks, NerdWallet). **M**
  - https://www.corpay.com/en-SG/resources/blog/invoice-factoring
  - https://www.nerdwallet.com/business/loans/learn/invoice-financing
- **Reverse factoring priced off buyer credit: ~SOFR + 60–100 bps** for investment-grade buyers (illustrative SOFR+0.85% vs supplier's own SOFR+5.50%). **L** (CTP study guide; want a bank/academic source)
  - https://open-exam-prep.com/study-guides/ctp/trade-finance-supply-chain/supply-chain-finance
- **B2B card interchange ~2–3%**; Visa set B2B virtual-payment interchange at 2% (Oct 2025 schedule). **M**
  - https://www.corpay.com/en-SG/resources/blog/interchange-fees-card-rebates

## Reach
- **Global factoring > €4tn (2025)**, ~€4.04tn, +3.7% YoY (FCI via BCR, May 2026). **H/M**
  - https://bcrpub.com/?p=152600 · https://www.abfjournal.com/?p=171987
- **Supply chain finance ~US$2.65tn volume (2025)**, ~$2.46tn (2024); >$1tn funds in use (BCR World SCF Report). **M**
  - https://bcrpub.com/product/world-supply-chain-finance-report-2026/
- **C2FO: "over $400bn" funded to "100,000+ businesses"** (self-reported, 2025). **M**
  - https://c2fo.com/newsroom/cnbc-world-top-fintech-companies-2025/
- **Taulia network "more than two million businesses"** (SAP acquisition release, 2022). **H**
  - https://www.businesswire.com/news/home/20220126006120/en/SAP-to-Acquire-Leading-Working-Capital-Management-Company-Taulia
- **US commercial card purchase volume $2.23tn (2024)** (Nilson). **H**
  - https://nilsonreport.com/articles/us-commercial-cards-amex-mastercard-and-visa-in-2024/
- **MSME finance gap $5.7tn** formal (119 developing economies; ~$8tn incl. informal) (IFC / SME Finance Forum). **H** — check edition when citing.
  - https://www.smefinanceforum.org/news/11526737
- **Trade finance gap $2.5tn; SME rejection rate 41%** (ADB survey, Dec 2025); ADB recommends deep-tier SCF. **H**
  - https://www.adb.org/publications/adb-global-trade-finance-gap-survey

## Platform fees
- C2FO: supplier bids discount/APR; buyer/funder pricing custom, not public. **M**
- Taulia: share-of-discount / tech fee — only old/secondary sources. **L**
- PrimeRevenue, Demica, Orbian, Tradeshift fees: **NOT VERIFIED** (no public data).

## Why a neutral, auditable protocol (fraud)
- **Greensill (Mar 2021):** insurance not renewed → Credit Suisse froze ~$10bn SCF funds; insolvency. **H**
  - https://www.cnbc.com/2021/03/16/credit-suisse-flags-possible-charge-from-greensill-fallout.html
- **First Brands (Ch. 11, Sep 2025):** reportedly ~$2.3bn factored invoices + ~$682m SCF off balance sheet; **under investigation** for receivables possibly double-pledged/factored more than once. **Allegations only.** **M–H**
  - https://www.gtreview.com/news/americas/first-brands-faces-investigation-into-double-financing-of-receivables-inventory/
  - https://www.sfnet.com/detail-pages/article-detail/the-first-brands-collapse-a-cautionary-tale

## Accounting guardrails
- **FASB ASU 2022-04:** FY beginning after 15 Dec 2022 (rollforward after 15 Dec 2023). **H**
- **IAS 7 / IFRS 7 supplier finance amendments:** periods from 1 Jan 2024. **H**

## Precedents for open, operator-less networks
- **InvoiceNow (Peppol, Singapore):** voluntary from 1 May 2025; mandatory for new voluntary GST registrants from Nov 2025 / Apr 2026; extended to **all GST-registered businesses, phased Apr 2028–Apr 2031** (CoS 2026). **H**
  - https://www.iras.gov.sg/news-events/newsroom/committee-of-supply-2026--extension-of-gst-invoicenow-requirement-to-all-gst-registered-businesses-by-april-2031
- **OCEN (India):** open credit protocol; ~50k loans / ₹1,142 cr in 2025 YTD (self-reported, modest). **L**
- **TReDS (India):** RXIL ~₹80,500 cr, M1xchange >₹78,000 cr financed in FY25. **M**
- **Failed consortium platforms:** we.trade (closed Jun 2022), Marco Polo (insolvent Feb 2023), Contour (closed Oct/Nov 2023) — all shut when bank owners stopped funding. **H**
  - https://www.gtreview.com/news/fintech/exclusive-contour-to-shut-down-as-bank-shareholders-pull-funding/

## Prior art
- No open-source SCF protocol with buyer-signed undertakings + acknowledgement of assignment found (snippet-level search, not exhaustive).
- Centrifuge/Tinlake (AGPL, legacy since ~2023): DeFi tokenisation of real-world assets, not buyer-confirmed SCF.
- Hyperledger / Corda trade-finance samples: letters of credit, not SCF.
- FinverityOS (Finastra), FCIreverse, PrimeRevenue, TasConnect: proprietary.
