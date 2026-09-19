# Dashboard UX refresh — 18 September 2026

## Direction

A calmer paper-trading workspace. Preserve the eight existing sections and
backend trading behavior. Prioritize account performance, operating state,
and the next useful action; move supporting explanations into disclosure.

References researched from primary sources:

- [Ghostfolio](https://github.com/ghostfolio/ghostfolio): minimalist portfolio
  presentation, dark mode, and mobile-first usability.
- [Grafana dashboard guidance](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/best-practices/):
  overview-to-detail hierarchy and consistent semantic colors.
- [Carbon data tables](https://carbondesignsystem.com/components/data-table/usage/):
  search/filter toolbars, stable dense rows, and pagination.
- [TradingView order workflow](https://www.tradingview.com/support/solutions/43000786671-how-to-place-an-order-on-tradingview/):
  review consequential position actions before submitting them.

These inform our own implementation; no third-party UI code or paid template
is copied, and no new frontend framework is introduced.

## Implementation plan

1. Retain top navigation, add a clear section title/purpose and persistent
   paper-account label, and make active navigation accessible.
2. Emphasize four overview metrics, give the equity chart more space, condense
   engine controls, and offer watchlist/lab next steps for an empty account.
3. Poll shared engine state on every section. Show update freshness and an
   explicit stale-data state on failure; keep connection loss distinct from
   engine stop. Prevent repeated start/stop submissions while pending.
4. Add symbol/text search, status and strategy filters, matching counts, and
   25-row pagination to trade history. Preserve filters across refreshes.
5. Use an explicit position-close confirmation with keyboard focus handling.
   Keep reset and market confirmations consistent and accurately scoped.
6. Apply restrained spacing, typography, surfaces, focus states, and responsive
   layouts in both light/dark themes. Measure the sticky header rather than
   assuming it remains one row on phones.

## Acceptance checks

- Real browser at desktop and 390px width, in light and dark themes; no body
  overflow and no overlap between sticky header and navigation.
- Navigate all sections; current section is announced and header state stays
  current outside Overview/Portfolio.
- Search, filter, and paginate fixture trade data; distinguish empty history
  from no matching records; retain filters on refresh.
- Confirm/cancel position closure, keyboard-trap dialogs, and restore focus.
- Simulate failed API reads and recovery without showing stale data as live.
- Run existing Python tests, lint, browser script checks, and artifact capture.

Browser verification uses deterministic sample responses and a local preview
server. It does not start engines, alter the real journal, or contact exchanges.
