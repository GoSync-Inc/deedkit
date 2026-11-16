---
name: android-tokopedia-navigation
description: Observed facts for the Tokopedia Android app (com.tokopedia.tkpd) - launch, promo popup, search, filter sheet, product, seller and shipping details, and the Transaction list, including which screens need screenshots because cards are missing from the hierarchy. Use when a phone goal involves Tokopedia.
---

# Tokopedia on Android

Facts from one read-only audit on version 45.0.7 (static metadata). Recheck if the
screen differs.

## Observed routes

- Launch through the resolved launcher activity (observed:
  `com.tokopedia.tkpd/com.ss.android.ugc.aweme.splash.SplashActivity`). No deep link tested.
- Home has Home, Feed, PLUS, Transaction and Account tabs. A PLUS promotion can block home; its
  close X has no label, so locate it with a screenshot. Home nodes under it still report visible.
- Search: tap the home search, then the editable query field, select existing text, type, and
  press Search. In-app back goes results -> search entry -> home (two arrows).
- Filter icon on results opens a sheet (free shipping, location, Mall/Power shop, price range);
  its X closes it without applying. Sort, Store tab and chips are untested.
- Product page: the hierarchy carries the full title, price, previous price, discount, rating,
  sold and review counts, variant count with "Sold out" markers, seller name, location, rating
  and item count, and shipping price with estimated arrival. The sticky **Detail produk** tab
  jumps to specifications and description; "See more" was not opened.
- Variant thumbnails have no names in the hierarchy; their names, availability and prices are
  unverified.
- The product footer has an unlabeled chat icon beside **Beli langsung** and **+ Keranjang**.
  It is an affordance only; recipient and composer are unverified.
- Home -> **Transaction** opens the order list with status, product and date filters. Opening a
  card or tracking detail is untested.

## Text versus screen

Search result cards, filter chips and Transaction order cards were absent from the hierarchy
text, so read those screens with a screenshot. Product, seller and detail screens were
text-rich. Long text can extend past clipped bounds, and visibility flags do not prove a target
is unobscured.
