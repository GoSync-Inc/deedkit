---
name: android-grab-navigation
description: Observed facts for the Grab Android app (com.grabtaxi.passenger) - launch, the background-location prompt, the guest home and the login gate in front of Food. Use when a phone goal involves Grab, GrabFood or Grab orders.
---

# Grab on Android

Facts from one read-only audit on version 5.430.0 (static metadata), signed out. Recheck if the
screen differs.

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

## Unverified

Restaurants, menu text coverage, search, Orders/Activity and courier chat after login were not
reached; nothing is known about them yet. No deep link was tested.

On the observed screens the hierarchy labels were enough to find targets; that does not show
how complete long or clipped strings are. Take a screenshot when a field is missing or ambiguous.
