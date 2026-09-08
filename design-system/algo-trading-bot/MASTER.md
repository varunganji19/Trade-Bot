# Design System Master File

> **LOGIC:** When building a specific page, first check `design-system/pages/[page-name].md`.
> If that file exists, its rules **override** this Master file.
> If not, strictly follow the rules below.

---

**Project:** Algo Trading Bot
**Updated:** 2026-09-06
**Category:** Financial Dashboard (FastAPI single-page app, inline CSS in `bot/dashboard.py`)
**Design Dials:** Variance 4/10 (Balanced / Modern) | Motion 4/10 (Standard) | Density 9/10 (Dense / Dashboard)

---

## Theme Architecture (3 modes)

Swapped via `[data-theme]` on `<html>`, set **before first paint** by a boot script in
`<head>` (localStorage key `algo-theme`; falls back to OS `prefers-color-scheme`).
Light is the default. Components only ever consume CSS tokens — never raw hex.

| Theme | `data-theme` | Feel | Background |
|-------|--------------|------|------------|
| Light (default) | `light` (absent) | White-blue/green | `#F6F8FB` |
| Dark | `dark` | Grayish slate finish | `#0F1218` |
| AMOLED | `black` | True black `#000` for OLED panels | `#000000` |

### Semantic color roles (identical meaning in every theme)

- **Blue `--color-primary` (#2563EB)** — interactive chrome: active tab, primary
  buttons (Start/Add/Send), focus rings, links, strategy names.
- **Green `--color-accent` / `--color-pos`** — money-in, success, engine running,
  positive P&L. `.btn-success` (Deposit) is green in ALL themes.
- **Red `--color-destructive` / `--color-neg`** — money-out, danger, negative P&L.
  `.btn-danger` (Withdraw / Reset / delete) is red in ALL themes.
- The decisions terminal (`.term`) stays a dark code-block in every theme
  (deliberate, like shadcn docs code blocks) via dedicated `--term-*` tokens.

### Contrast rules

- Light: green text/buttons use green-700 `#15803D` (5.0:1 on white), never green-500.
- Dark/AMOLED: green buttons use `#22C55E` bg + `#052E16` text (9.2:1);
  danger stays `#DC2626` + white (4.8:1).
- All body text ≥ 4.5:1 against its surface in every theme.

---

## Typography

- **UI font:** Fira Sans · **Numeric/mono:** Fira Code (tabular-nums for prices)
- Loaded via Google Fonts `@import` at the top of the inline stylesheet.

---

## Component Specs (shadcn-inspired, token-driven)

- **Topbar:** sticky, translucent `--header-bg` + 10px backdrop blur; gradient
  logo chip (`--brand-grad`, the one fixed-brand element); 3-state theme switch
  (sun / moon / half-circle) with `aria-pressed` states.
- **Cards:** `--radius-lg` (12px), 1px border + `--shadow-sm`; stat cards lift
  `-2px` with a blue-tinted border on hover. Balance card gets `--hero-grad`
  radial green/blue glows.
- **Buttons:** primary (blue) with hover lift + shadow-md; `.btn-success`
  (deposit), `.btn-danger` (withdraw/reset), `.btn-secondary` (surface + border),
  `.btn-ghost`. All transitions 200ms; `:active` presses back down.
- **Tabs:** underline style, hover pill in `--color-muted`, active in blue.
- **Inputs:** card-colored bg, focus ring `0 0 0 3px var(--ring-soft)`.
- **Tables:** themed `--row-hover`; soft tags (`--pos-soft/--neg-soft/--blue-soft`).
- **Toasts/modals:** themed via `--overlay`, `--shadow-lg/xl`.

## Smooth scrolling

- `html { scroll-behavior:smooth; scroll-padding-top:118px; scrollbar-gutter:stable }`
- Slim themed scrollbars everywhere (`scrollbar-width:thin` + webkit rules).
- `setView()` smooth-scrolls to top on tab switch (auto under reduced motion).
- `prefers-reduced-motion: reduce` kills all animation/scroll-behavior globally.

## Anti-Patterns (Do NOT Use)

- ❌ Raw hex in components — tokens only (one of the three theme blocks owns colors)
- ❌ Emojis as icons — SVG (Lucide-style inline sprite)
- ❌ Instant state changes — always 150–300ms transitions
- ❌ Low contrast text — 4.5:1 minimum per theme
- ❌ Making deposit/withdraw anything other than green/red
- ❌ Light-theme green `#22C55E` as text on white (fails contrast — use `#15803D`)

## Pre-Delivery Checklist

- [ ] New UI elements consume tokens, so all 3 themes work without extra CSS
- [ ] Chart.js colors updated via `applyChartTheme()` when adding canvases
- [ ] Focus states visible (`--color-ring`), 44px touch targets on controls
- [ ] Verified in light + dark + black (theme switcher, no FOUC on reload)
- [ ] Responsive: 375px, 768px, 1024px, 1440px; no horizontal scroll
