# WorkAMA — DESIGN.md

> Brand contract for the WorkAMA enterprise AI platform. The coding agent treats this file as the source of truth when rendering any surface. Every `frontend-design` / OpenDesign workflow reads this first, then applies its own craft checks where this document is silent.
>
> Adapted for OpenDesign `design-system` mode. Sections: color, typography, layout, components.

---

## 1. Brand & Direction

**Product:** WorkAMA is a document-driven enterprise AI platform baseline (production-grade, not demo): OpenAI-compatible gateway (Go) + multi-tenant control plane (Python/FastAPI) + AMA-Chat/Work runtimes + billing + gVisor sandbox + observability + React multi-surface (web/miniapp/extension/desktop).

**Aesthetic direction — Precision Operational (chosen).** A calm-enterprise console that earns trust through structure, not decoration. Think **Linear's density + Stripe Dashboard's data clarity + an editorial touch of ink-on-stone**. No purple-blue gradients, no glass-cards-everywhere, no over-rounded blobs.

- **Mood:** trustworthy, precise, composed; moments of quiet confidence (hero/empty states) lifted by editorial type and subtle depth.
- **Density:** tools and dashboards are scannable and dense enough for repeated operational use; marketing surfaces can breathe and be more expressive.
- **Memorable quality:** "the console that feels like a well-made instrument — every control exactly where you expect it, with nothing shouting."

**Audience:** platform operators, workspace owners, developers, finance/compliance reviewers. They live in tables, logs, and config forms for hours — respect their attention.

**Anti-AI-slop guards (enforced):**
- No generic purple-blue gradient hero, no floating decorative blobs, no vague glass cards.
- No stock icon rows without labels; no placeholder lorem without marking it sample/pending.
- No interchangeable SaaS card grids that could belong to any product. Each surface earns its shape from its job.

---

## 2. Color

**Roles over hex.** Every color has a job. Accent is used sparingly and deliberately — never as a page background.

| Token | Value (light) | Role |
| --- | --- | --- |
| `--wama-accent` | `#6d5efc` | Primary action, active nav rail, focus within, selected state |
| `--wama-accent-600` | `#5b4ef0` | Hover for primary |
| `--wama-accent-700` | `#4a3fd6` | Pressed / active text on soft |
| `--wama-accent-soft` | `#eef0ff` | Tinted wells, selected rows, hover for ghost |
| `--wama-accent-contrast` | `#ffffff` | Text on accent |
| `--wama-sidebar` | `#0e1016` | Sidebar base (ink) |
| `--wama-bg` | `#f6f7f9` | Canvas |
| `--wama-surface` | `#ffffff` | Cards, panels, popovers |
| `--wama-surface-2` | `#fbfbfd` | Recessed wells, table head, filters |
| `--wama-border` | `#e8eaef` | Default stroke |
| `--wama-border-strong` | `#d9dce4` | Inputs, stronger dividers |
| `--wama-text` | `#161a22` | Primary text (ink) |
| `--wama-text-2` | `#2a3140` | Secondary text on surface |
| `--wama-muted` | `#4b5563` | Tertiary / helper |
| `--wama-success` | `#15803d` | Success text/icon |
| `--wama-warning` | `#b45309` | Warning |
| `--wama-danger` | `#b91c1c` | Danger |
| `--wama-info` | `#1d4ed8` | Info |

Dark theme remaps surface/bg/text to ink tints and lifts accent soft to translucent — see `apps/web/src/styles/theme.css` dark block.

**Usage rules:**
- Accent appears at most once per viewport as the dominant call-to-action; other actions are secondary/ghost.
- Data visuals: accent + muted neutrals first; semantic colors only for state.
- Never use accent soft as a full-page background — only for rails, wells, and selected states.

---

## 3. Typography

| Role | Family | Weight | Usage |
| --- | --- | --- | --- |
| UI sans | `Inter`, system fallback, PingFang SC | 400 / 500 / 600 / 700 | All console UI, tables, forms |
| Mono | `JetBrains Mono`, SFMono, Consolas | 400 / 600 | Code, IDs, tabular numbers, kbd |
| Display (hero only) | `Fraunces` or `Newsreader` (loaded via Google Fonts) | 700 / 800 | Landing H1, auth rail H2, empty-state headline — editorial moment, not console chrome |

**Scale (console):** eyebrow 11px / 0.06em uppercase; H1 22px/-0.02em/700; H2 14.5px/700; body 13-13.5px/1.55-1.68; small 11-11.5px; mono 11.5-12px; kbd 10-11px.

**Rules:**
- Headlines are tight and tracked negative; body is generous for reading.
- Tabular numbers (`font-variant-numeric: tabular-nums`) for counts, pagination, metrics.
- Never use display font inside tables, forms, or dense lists — it is reserved for 2-3 moments per session.

---

## 4. Layout & Spacing

**Grid:** 12-col for marketing/docs; console uses sidebar 256px + topbar 58px + page gutter 30px (18px on mobile). Content measure capped ~880px for reading surfaces (chat transcript).

**Spacing scale:** 6 / 8 / 10 / 12 / 14 / 16 / 18 / 24 / 32 / 48. Prefer 12-18 for component padding, 14-18 for card gaps, 26-30 for page gutters.

**Density:**
- Dashboards: comfortable (16-18px card padding, 14px gap).
- Tables: compact but breathable (12-14px cell padding, 1px row dividers).
- Forms: stacked 15px gap, 2-col row at ≥560px.

**Radius:** sm 8px (inputs, table wells) / 12px (cards/panels) / 16px (auth card, command palette) / pill 999px (badges, pills).

**Shadow:** sm `0 1px 2px rgba(16,24,40,.06)` for cards; `0 4px 16px rgba(16,24,40,.08)` on hover; lg `0 16px 40px rgba(16,24,40,.14)` for modals/palette.

---

## 5. Components

**Buttons:** primary = accent fill + soft shadow; secondary = surface + strong border; ghost = transparent. 13.5px/600, 9×14 padding, 9px radius. Hover lifts border/background; active nudges 1px down; focus shows accent ring (`0 0 0 3px accent-soft`).

**Cards / Panels / KPI:** surface + border + sm shadow + 12px radius. Hover: shadow lifts + 1px translate. Dashboard stat adds a soft radial accent glow at top-right (`--wama-accent-soft`).

**Tables:** head = muted uppercase 11.5px on `surface-2`; rows 13px with 1px border dividers; hover = `surface-2`; selected = soft tint + 3px accent rail (`box-shadow: inset 3px 0 0 accent`). Numeric cols right-aligned with tabular nums.

**Tabs:** underline variant — transparent base, hover = surface-2, active = accent text + 2px accent underline. Counts as pill badges.

**Empty states:** centered, icon in soft well (52px, accent tint), headline 15px/700, body 13px/1.6, max 46ch, CTA below.

**Skeletons:** surface-2 base with shimmer (`linear-gradient(90deg, transparent, rgba(255,255,255,.55), transparent)` → translateX). Respects `prefers-reduced-motion`.

**Tooltips:** dark pill (12,20,26 bg), 11.5px/500, arrow, 140ms ease.

**Dropzone:** dashed strong border on surface-2; hover/dragover = accent tint + solid.

**Chat:** rail 288px, measure 880px; transcript uses alternating bubbles (surface for model with 14px radius / 4px tail; accent for user); typing dots and caret are the only looping motions.

---

## 6. Motion

- Durations: 120ms (hover), 150-180ms (reveal, shadow), 220ms (rise), 300-500ms (progress).
- Easing: `ease` for color/shadow, `cubic-bezier(.34,1.56,.64,1)` for check pop.
- Only `transform` + `opacity` for performance. Never animate `width/height/top`.
- Respect `prefers-reduced-motion: reduce` — disable rise, shimmer, ping, blink.

---

## 7. Iconography & Imagery

- Icons: 1.5px stroke, rounded caps; use Lucide. Size 16-18 for nav, 18-20 for card heads, 20-22 for empty states.
- Imagery: product screenshots over illustrations; when illustration is needed, use duotone with accent + ink, not multicolor gradients.

---

## 8. Accessibility

- Contrast: body text ≥7:1 on canvas; accent-strong `#3b2da0` hits 7.25:1 on canvas.
- Focus: every control shows a visible outer ring (accent soft 3px).
- Keyboard: tables are navigable, tabs are roving, cards are `button` or `a` with `focus-visible`.
- Screen readers: eyebrow + H1 hierarchy, badges announce via text, tables have `<caption class="sr-only">`.

---

## 9. What "good" looks like (self-review checklist, from `frontend-design`)

- [ ] Works at 320 / 760 / 980 / 1100 widths; no overflow, no clipped text.
- [ ] Every interactive element has hover / focus / active / disabled.
- [ ] Text fits containers at all locales (EN/ZH), no overlap.
- [ ] No AI slop: one clear direction, not generic gradients/cards.
- [ ] One memorable quality a user could describe after closing the page.
- [ ] `tsc --noEmit` green, vitest green, axe no critical.

