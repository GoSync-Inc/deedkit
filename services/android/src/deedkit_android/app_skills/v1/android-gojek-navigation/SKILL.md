---
name: android-gojek-navigation
description: Observed facts for the GoJek Android app (com.gojek.app), mainly GoFood restaurant menus - where full dish text sits in the UI hierarchy, what overlays hide, and which routes are verified versus untested. Use when a phone goal involves GoJek, GoFood or GoPay.
---

# GoJek on Android

Facts from one read-only audit on version 5.82.1 (static metadata). Recheck if the screen differs.

## Restaurant menu lists (GoFood)

- On a restaurant's menu list, each dish row exposes `com.gojek.app:id/tvDishName` and
  `com.gojek.app:id/tvDishDescription`. The description node held the full text even when
  the screen showed it cut off with an ellipsis. Price and availability controls (plus button,
  quantity stepper, "Out of stock") belong to the same dish container.
- So names, descriptions, prices and displayed availability can be read from the hierarchy
  without opening each dish. Open a dish only when a field is missing, clipped in the text
  too, or the goal needs the dish page (adding, quantity, options).
- `visible-to-user=true` is not enough. A label further down the list existed in the hierarchy
  while the bottom cart bar covered it. Before tapping, compare bounds with overlays such as the
  cart bar; take a screenshot when visibility or the control state is ambiguous.
- An enabled plus or stepper shows displayed availability only, not checkout stock.
- "Search food in this resto" and a Menu section shortcut were present on the menu page but
  untested.

## Elsewhere

- Restaurant food is under GoFood, not GoMart (provisional, from earlier caller routes).
- The home **Pay** button is the GoPay wallet shortcut (provisional, caller-described), not a
  basket or checkout step.
- No deep link is verified for restaurants, menus or orders; declared manifest actions are
  untested candidates. Navigate in the app.

## Screenshots

Use them for missing, clipped, unlabeled or ambiguous fields, overlays, and evidence the
current caller asks for. A screenshot requirement from an earlier task does not carry over.
