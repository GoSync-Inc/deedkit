---
name: android-grab-navigation
description: Observed facts for the Grab Android app (com.grabtaxi.passenger) - launch, the background-location prompt, the guest home, the login gate in front of Food, and GrabMart grocery search (one store's catalogue versus finding a product across stores). Use when a phone goal involves Grab, GrabFood, GrabMart, buying groceries or Grab orders.
---

# Grab on Android

Facts from one read-only audit on version 5.430.0 (static metadata), signed out, and from the
retained screens of signed-in GrabMart runs on 2026-11-16 (app version not recorded). Recheck if
the screen differs.

## Observed route

- Resolve the launcher activity rather than assuming one; it resolved to
  `com.grab.pax.newface.common.alias.DefaultLauncherAlias`, and starting it opened Grab.
- At start Grab asked "Turn on background location access?". **Maybe later** closed it without
  granting the permission.
- The signed-out home showed **Food** (a text child of a clickable tile), a search field with
  content description "Open search", Account and other service tiles, and **Sign Up / Log In**
  at the bottom. Food category labels (Bestsellers, Nearby, ...) existed in the hierarchy but
  sat under the bottom login panel; they are not usable targets there.
- Tapping Food while signed out opened the login sheet "Let's get you in!", resource IDs
  `simple_guest_login_view_title`, `simple_guest_login_view_message`,
  `simple_guest_login_view_signup`, `simple_guest_login_view_login`.

## Auth barrier

The audit ran signed out and stopped at this sheet without logging in. Whether to log in
follows the current goal and the existing auth rules; there is no observed shortcut around it.

## GrabMart: finding a product

Observed on signed-in screens, 2026-11-16:

- The Home tab showed a search field labelled "Search place" or "Search places", and a Mart
  entry.
- Inside a store, the field "Search this store" searches that store's catalogue only. Searching
  for one product store by store kept ending in "No results" or "Out of stock".
- A delivery results list showed several merchant cards with product previews.

Not observed: whether the Home search or a Mart search finds a product across stores, how its
results look, and its selectors.

The owner's preference (2026-11-16): «А надо было просто в глобальном поиске искать курицу и
выбирать уже из магазинов, где она есть.» ("It should have searched for the chicken in the
global search and then chosen among the stores that have it.") So when a goal asks for a product
without naming a store, look first for a search that covers products across stores, and choose
among the stores that show it available. Search one store at a time when no such search is
available or its results do not fit the goal, or when the goal names the store.

## Unverified

Restaurants, menu text coverage, Orders/Activity and courier chat after login were not reached;
nothing is known about them yet. No deep link was tested.

On the observed screens the hierarchy labels were enough to find targets; that does not show
how complete long or clipped strings are. Take a screenshot when a field is missing or ambiguous.
