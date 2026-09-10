# Design mockups — approval gate

Open these files in a browser. No server required.

| File | What it shows |
|---|---|
| [tokens.html](tokens.html) | Palette, type scale, status vs brand, components |
| [analyst.html](analyst.html) | Production analyst shell: topbar, turn anatomy, bottom composer |
| [admin.html](admin.html) | Admin: health rail, sticky jump nav, auto-refresh |

## What to approve or reject

1. Token palette (keep existing teal/navy — not a rebrand of hues)
2. Analyst topbar: brand + one health + user menu (mode switch gone)
3. Composer at bottom
4. Turn: outcome line → collapsed SQL → result → details drawer
5. Admin: summary rail + jump nav + global refresh

## What happens next

Implementation starts on `feature/ui-design-system` only after explicit approval
or a short revision list. Target version **5.0.0**.
