# Compliance: what this project does not do, and what to ask a professional

> **This is not legal or tax advice.** It is a checklist written by the
> project (with AI assistance) to take to a qualified adviser: a lawyer
> familiar with SEBI regulation and a chartered accountant. Facts below were
> checked on 2026-10-02 against the sources listed; laws change, and only a
> professional can say how they apply to you. (Roadmap V8.)

## What the project does not do today

| It does not | Why it matters |
|---|---|
| Trade real money | Paper trading only; the roadmap stops at an exchange **testnet** (V6). Any real-money step is the owner's separate decision. |
| Manage anyone else's money | No pooled accounts, no custody, no third-party funds: portfolio-management and fund rules are not triggered by the code as it stands. |
| Sell or publish trade signals | The bot's decisions stay in its own journal; nothing is broadcast to other people. |
| Give investment advice | The validator grades a backtest's statistics. Its report says "This is a statistical check of a backtest, not investment advice", and no strategy is presented as profitable. |
| Hold user data | No accounts, no sign-ups, no analytics. A waitlist (V4) or a shared verdict store (V7) would change this. |

## Disclaimers in the product

- README: "Paper trading only. No strategy here is presented as
  profitable." The dashboard does **not** yet carry this sentence.
- Fast book labelled "Experimental — no proven edge" in the dashboard and
  README (roadmap 0.2).
- Validator report footer: not investment advice.
- **To add before any public launch** (ask the adviser for wording): the
  paper-only statement on the dashboard itself, a terms-of-use page, a privacy notice, and a risk warning on any page that
  shows strategy performance.

## Questions for the adviser

### Securities regulation (India — SEBI)

1. Does publishing a "robust / fragile / likely overfit" verdict on a
   user's strategy fall under the **SEBI (Investment Advisers) Regulations,
   2013** or the **SEBI (Research Analysts) Regulations, 2014**? Does it
   change if the service is free, paid, or applied to named securities?
2. SEBI's circular of 4 February 2025 on retail participation in
   algorithmic trading (SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013;
   implementation extended to 1 October 2025) sets roles for brokers and
   **algo providers**. If the validator or this bot is ever offered for
   Indian equities or derivatives, does the project become an "algo
   provider" that must be empanelled with an exchange?
3. Crypto assets are not SEBI-regulated securities. Does that remove SEBI
   questions for a crypto-only product, or do other regulators apply?
4. Would sharing the sample report (the project's own strategies, all
   "likely overfit") on Reddit or Discord (V4) count as research or
   advertisement under any of the above?

### Tax (India)

5. Paper and testnet trading create no taxable gain. If real-money crypto
   trading is ever started: income from transferring virtual digital assets
   was taxed at **30%** (old s. 115BBH) with **1% TDS** (old s. 194S) under
   the Income-tax Act, 1961. The **Income-tax Act, 2025** applies from
   1 April 2026; secondary sources report the same rates under renumbered
   sections (194 and 393(1)). Confirm the current sections, rates and how
   losses are treated (they could not be set off under the old regime).
6. If the validator is sold, how is the revenue taxed, and is GST
   registration needed at the expected scale?

### Data licences

7. **Yahoo Finance via yfinance** supplies the forex data. yfinance's own
   README says Yahoo's API "is intended for personal use only". Can a paid
   or public product use it at all, or must forex data come from a licensed
   vendor?
8. **Binance market data** (public REST and websocket, recorded by V7):
   do Binance's terms allow storing it, redistributing derived datasets, or
   selling a product built on it?
9. Are the exchange's terms different for testnet API use (V6)?

### Users and personal data

10. A waitlist (V4) collects email addresses; a verdict store (V7) collects
    strategy metadata. What does the **Digital Personal Data Protection Act,
    2023** require (notice, consent, retention, deletion), and is the
    planned anonymisation enough to keep verdicts out of scope?

### Licence and liability

11. The code is MIT-licensed and the validator reads freqtrade's export
    format without using freqtrade's GPL-3.0 code. Is that boundary sound
    for a paid hosted service (the open-core idea in ROADMAP)?
12. If a user loses money after a "robust" verdict, what liability exists,
    and what limitation-of-liability terms are needed?

## Before each step, check

| Step | Ask first |
|---|---|
| Testnet adapter (V6) | Q9 |
| Order-book recorder (V7) | Q8 |
| Publishing the sample report (V4) | Q1, Q4 |
| Waitlist or verdict store (V4, V7) | Q10 |
| Charging for validation | Q1, Q6, Q7, Q11, Q12 |
| Any real-money trading | Q2, Q3, Q5 |

## Sources (accessed 2026-10-02)

- SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025):
  <https://www.cse-india.com/upload/upload/Feb_042025.pdf>; extension to
  1 Oct 2025 as summarised at
  <https://www.mondaq.com/india/commoditiesderivativesstock-exchanges/1581380/sebi-circular-safeguarding-retail-investors-in-algorithmic-trading>
- Section 115BBH: <https://www.incometaxindia.gov.in/w/section-115bbh>
- Income-tax Act, 2025: <https://en.wikipedia.org/wiki/Income-tax_Act,_2025>;
  renumbering as reported (secondary) at
  <https://www.patronaccounting.com/blog/crypto-vda-taxation-income-tax-act-2025-rules>
- yfinance disclaimer: <https://github.com/ranaroussi/yfinance>
