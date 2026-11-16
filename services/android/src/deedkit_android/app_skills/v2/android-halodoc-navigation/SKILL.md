---
name: android-halodoc-navigation
description: Observed navigation facts for the Halodoc Android app (com.linkdokter.halodoc.android) - home popup, Chat With Doctor directory and doctor lists, Health Store search and product pages, history entries, and where hierarchy text is partial. Navigation only, no medical guidance. Use when a phone goal involves Halodoc.
---

# Halodoc on Android

Facts from one read-only audit on version 31.000 (static metadata), signed in. Recheck if the
screen differs. These are navigation facts only: choosing a doctor, medicine or treatment is
the caller's decision, and consultations, chats and purchases follow the caller's goal.

## Observed routes

- Launch through the resolved launcher activity (observed:
  `com.linkdokter.halodoc.android/.splash.presentation.ui.SplashActivity`). No deep link tested.
- A promotional popup can cover home. Its only label is `backgroung-pop-up`, so a screenshot is
  needed to see it; the visible **Close** dismisses it.
- Home **Chat With Doctor** opens the "Chat with Doctor" directory: a field "Search by doctor,
  speciality or symptom" and specialty categories. A specialty opens a doctor list.
- The first doctor-list visit shows two tips: **Next**, then **Finish**. Cards then show name,
  specialty, experience, rating and reviews, price and **Chat**. Sort and filter controls exist
  (untested).
- Home **Health Store** opens the catalogue. Its search preview needs a second tap to focus
  the real field `act_search`; type the query and use the keyboard's **Search** action.
- Result cards show name, unit, price, stock and **Add / Select Options**. Tapping the product
  title, away from Add, opens the product page with stock state, price and description.
- Toolbar back arrows went doctor list -> directory -> home, and product -> results with the
  query and first results kept (one round trip observed). Some arrows have no label; the directory one is
  `toolbar_back`.
- **History** (`tab_history`) on home and a history icon (`containerForHistory`) in the doctor
  directory are entries only; their contents are unverified.

## Text versus screen

Coverage is partial. Long texts (home address, product description) were complete in the
hierarchy while visually truncated or obscured. A doctor price node sat under the HILDA
assistant overlay. Many card labels report `clickable=false`, so tap the clickable container.
Use a screenshot for unlabeled controls, popups and anything overlays may hide.

Unobserved: login or payment gates beyond these screens, order details, pharmacy or courier
participants.
