# Authority Command Dashboard — UI/Frontend Guide
### Every screen, every component, what it shows, and where its data comes from

## 1. Design Philosophy for a 3-Day Demo UI

This is a command-and-control interface for a maritime authority, not a consumer app — the visual language should read as "operations room," not "startup landing page": dark or muted-navy base theme, high-contrast status colors (red/amber/green risk tiers), dense information without clutter, map-first layout since almost everything in this system is inherently geospatial.

Every component below states the exact Gateway route it binds to (from the previous route guide) so frontend work has zero ambiguity about where data comes from.

**One screen to rule the demo:** everything lives on a single main dashboard view with a slide-in detail panel — do not build multi-page navigation for a 3-day hackathon. A single screen that a judge can watch evolve in real time (as the simulator plays events) is far more compelling than clicking between pages.

---

## 2. Overall Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  HEADER BAR: system name, live status indicator, incident count  │
├───────────────────────────────────────────┬─────────────────────┤
│                                             │                     │
│                                             │   ALERT / LIVE      │
│              LIVE MARITIME MAP             │   FEED (scrolling)  │
│         (vessels, spills, dark vessels,    │                     │
│          risk zones, forecast overlay)     ├─────────────────────┤
│                                             │                     │
│                                             │  INCIDENT SUMMARY   │
│                                             │  LIST (clickable)   │
├─────────────────────────────────────────────┴─────────────────────┤
│  FORECAST TIMELINE SLIDER (only visible when an incident selected) │
└─────────────────────────────────────────────────────────────────┘

  [Clicking an incident or vessel opens a slide-in DETAIL PANEL from the right, overlaying part of the map]
```

Left ~70% width: the map, always visible, always the anchor. Right ~30%: a stacked live feed above a static incident list. Detail panel slides in on top when something is selected, map stays visible underneath/beside it so spatial context is never lost.

---

## 3. Header Bar

**What it shows:** system name/logo, a live connection status indicator (small pulsing dot: green = WebSocket connected, red = disconnected/reconnecting), a running incident counter badge (e.g., "3 Active Incidents"), current simulated/real time.

**Data source:** connection status from the `/live` WebSocket's own connection state (not a data payload — just whether the socket is open); incident counter from the length of the `GET /incidents` response, kept live-updated as WebSocket events arrive.

**Why it matters:** this is the first thing a judge's eye lands on. The live pulsing indicator alone communicates "this is a real-time system" before they've read a single number.

**Build priority:** trivial, build last — pure polish.

---

## 4. Live Maritime Map

This is the centerpiece. Built on Leaflet (or Mapbox GL if your team already knows it) with several toggleable layers.

### 4a. Vessel Layer
**What it shows:** a marker per vessel at its current position, colored by risk tier (green = LOW, amber = MEDIUM, red = HIGH), with a small icon rotated to match the vessel's current heading so movement direction is visible at a glance even for a static snapshot.

**Data source:** `GET /vessels` (Gateway) on load, updated live via `/live` WebSocket `risk_escalated` events (refetch that one vessel's data rather than the whole layer).

**Interaction:** clicking a marker opens the Vessel Detail Popup (section 6). Hovering shows a lightweight tooltip: vessel name, mmsi, current risk tier.

**Why it matters:** this is your "situational awareness at a glance" story — a judge should be able to look at the map for two seconds and see which vessels are concerning without clicking anything, purely from marker color.

### 4b. Dark Vessel Layer
**What it shows:** a visually distinct marker (different icon — e.g., a ghost/warning icon, not a boat icon) at each dark vessel detection location, since these have no corresponding AIS-broadcasting vessel to render as a normal marker.

**Data source:** `GET /dark-vessels`, live-updated via WebSocket.

**Why it matters:** this is one of your two strongest visual differentiators (alongside spill attribution). Make it unmistakably distinct from a normal vessel marker — this single marker type is what tells the "AIS can't be trusted" story without a word of narration.

### 4c. Spill Layer
**What it shows:** the classified spill polygon (from B2/C1), filled with a color/opacity keyed to severity tier (D2) — e.g., yellow outline for LOW severity, deepening through orange to a solid red fill for CRITICAL. Clicking a polygon opens the Incident Detail Panel.

**Data source:** `GET /incidents` (which should already be enriched with severity tier per the Gateway route design).

**Why it matters:** severity should be readable from color alone, same principle as the vessel layer — a judge scanning the map should immediately know which spill is the urgent one.

### 4d. Risk Zone / Protected Area Layer
**What it shows:** a subtle, semi-transparent overlay of ecologically sensitive zones (mangroves, fishing zones, protected areas) from your static reference layers — always visible at low opacity so a viewer immediately understands *why* a spill near one of these zones is flagged HIGH ecological exposure.

**Data source:** `GET /reference-layers` (A1), loaded once, cached client-side (this data is static, no need to refetch).

**Why it matters:** without this layer, "ecological exposure: HIGH" is just text. With it, a viewer visually sees the spill polygon sitting right next to a highlighted mangrove zone — the causality is self-evident.

### 4e. Forecast Overlay
**What it shows:** when an incident is selected and the Forecast Timeline Slider (section 8) is engaged, this replaces or overlays the current spill polygon with the predicted polygon for the selected horizon (6h/12h/24h/48h/72h), typically rendered with a dashed border and lower opacity than the "current, confirmed" polygon to visually distinguish observed-fact from predicted-future.

**Data source:** the `forecasts` array already included in the incident's `GET /incidents/{id}/full` response (from D1) — no separate call needed, just filter client-side by the slider's selected horizon.

**Why it matters:** this is your "predictive" story made visible — watching the polygon visibly grow and drift as you slide through 6h → 72h is one of the most compelling few seconds of your entire demo.

### 4f. Backward Attribution Trajectory (shown only inside a selected incident)
**What it shows:** a dashed line tracing the estimated backward drift path from the spill polygon to the estimated origin point, with the top-attributed vessel's actual historical track drawn alongside it in a contrasting color/style — the visual payoff is the two lines visibly converging near the same point in space and time.

**Data source:** `GET /incidents/{id}/backward-trajectory` (C2).

**Why it matters:** this is arguably your single best demo visual. It makes "we calculated who caused this" tangible and inspectable rather than a black-box percentage — protect time to build this well.

**Build priority:** vessel layer and spill layer first (Day 1–2, core map functionality) → dark vessel layer (Day 2) → risk zone layer (Day 2, mostly static-data plumbing) → forecast overlay (Day 3, once D1 has real data) → backward trajectory (Day 3, your final polish item, do this after everything else on the map works).

---

## 5. Incident Detail Panel

Slides in from the right when a spill polygon (map) or an incident row (list, section 7) is clicked. This is the single most information-dense component in the UI — it's where a judge spends the most time once they click something.

**Data source:** one call, `GET /incidents/{id}/full` (Gateway), which already merges C1+C2+D1+D2+E1 output — the panel is purely a rendering layer over one response object.

**Sub-sections, top to bottom:**

- **Header row:** Incident ID, a large severity badge (color-coded per D2's tier, same palette as the map's spill layer), a timestamp ("detected 14 minutes ago").
- **Confidence & Evidence breakdown:** the `overall_confidence` from C1 as a prominent percentage, with a small expandable breakdown showing each evidence component (`sar_evidence`, `ais_evidence`, `spatial_match`, `temporal_match`, `wind_current_match`, `ais_reliability`) as individual mini progress bars — this is what makes the system explainable rather than a black box, and is worth building even if simple, since it's a direct answer to "how do you know this is real" before a judge even asks.
- **Source Attribution breakdown:** the headline visual — a horizontal bar list, one bar per candidate vessel, labeled with vessel name/mmsi and its `final_attribution_probability` (e.g., "Vessel A — 87%"), sorted descending, with an explicit "Unknown — 2%" bar at the bottom so the uncertainty is visible rather than hidden. Clicking a vessel bar highlights that vessel's marker on the map and (if this incident is currently selected) shows its trajectory per section 4f.
- **Severity & Impact stats:** a small stat grid — estimated area (km²), estimated volume (rendered explicitly as a **range**, e.g., "140–210 tonnes," never a single number — this honesty is worth calling out verbally in your demo as a deliberate design choice), growth rate (%/hour), distance to coastline (km), ecological exposure badge (LOW/MEDIUM/HIGH, colored).
- **Recommended Actions:** the ordered list from E1, rendered as a numbered checklist (visually, checkboxes an operator could imagine ticking off, even if not functionally interactive for the demo) — "1. Deploy containment, 2. Notify coastal authority, 3. Monitor fishing zone, 4. Track probable source vessel."
- **Close/dismiss control** to slide the panel back out.

**States to handle:** loading skeleton while `/full` resolves; if `attribution.unknown_probability` is high (no confident source found) show that honestly rather than forcing a misleading top candidate to look confident.

**Build priority:** build this against mock JSON on Day 1 (you already have the exact shape from the route guide) so it's visually complete before real data exists — wire to the real Gateway call only once C1/C2/D1/D2/E1 are producing real output, likely Day 3.

---

## 6. Vessel Detail Popup

A smaller popup (not a full slide-in panel — a lightweight overlay anchored near the clicked marker) when a vessel marker is clicked.

**What it shows:** vessel name, mmsi, vessel type, current risk tier + score, the `contributing_factors` list from A7 rendered as short tags (e.g., "AIS trust: 0.31 ⚠", "Dark vessel match", "Anomaly: erratic course"), a small trust-score sparkline (section 4a note — the trust score history from A5 is genuinely worth a tiny inline chart here).

**Data source:** `GET /vessels/{mmsi}/full` (Gateway).

**Why it matters:** this is where the AIS-spoofing story gets its own moment, separate from the incident panel — a judge should be able to click any suspicious-looking vessel and immediately see *why* it's suspicious, in plain tags, not a raw JSON dump.

**Build priority:** Day 2–3, lower priority than the incident panel since the incident panel carries more of your core narrative — build this if time allows, and if it's tight, a simpler tooltip-only version (name + risk tier, no sparkline) is an acceptable fallback.

---

## 7. Incident Summary List

A simple, always-visible list in the right sidebar below the alert feed — one compact row per active incident.

**What it shows per row:** small severity color chip, incident ID, one-line location description (or lat/lon), time since detection, top attributed vessel name if attribution has run. Clicking a row opens the same Incident Detail Panel as clicking the map polygon, and also pans/zooms the map to that incident's location.

**Data source:** `GET /incidents` (Gateway), live-updated via WebSocket `new_incident` events (prepend new rows with a brief highlight animation so new incidents are visually obvious as they arrive during the demo).

**Why it matters:** the map alone requires a viewer to spot a small polygon; the list gives a guaranteed, always-visible entry point to every incident regardless of current map zoom/pan state — important since your demo will likely zoom into specific areas at times.

**Build priority:** Day 2, straightforward once `/incidents` is real.

---

## 8. Alert / Live Feed

A scrolling, auto-updating list above the incident summary list — this is the component that makes the WebSocket connection visible and the whole system feel "live" rather than a static loaded page.

**What it shows:** a chronological feed of events as they occur — "New incident detected near [location]," "Vessel [name] risk escalated to HIGH," "Dark vessel sighted at [location]," "Vessel [name] AIS trust dropped below threshold" — each with a small icon matching its type and a relative timestamp ("just now," "2 min ago"), newest at top, with a brief highlight/flash animation on arrival.

**Data source:** entirely driven by the `/live` WebSocket — each event type from the Gateway (`new_incident`, `risk_escalated`, new dark vessel, etc.) maps directly to one feed entry; no polling needed, this component is purely reactive to socket messages.

**Why it matters:** this is the component that turns your demo from "here's a dashboard with some data in it" into "watch this happen in real time" — during your scripted demo scenario, narrate directly off this feed as events appear ("and there — the system just flagged that vessel's AIS trust score dropping").

**Build priority:** Day 2–3. Build the WebSocket connection and a bare unstyled list first to prove the plumbing works, style it last — the functional live-update behavior matters far more than its visual polish.

---

## 9. Forecast Timeline Slider

A horizontal slider/tab control that appears only when an incident is selected, positioned along the bottom of the map.

**What it shows:** five labeled stops — "Now," "+6h," "+12h," "+24h," "+48h," "+72h" — dragging or clicking a stop updates the Forecast Overlay (section 4e) on the map to show that horizon's predicted polygon, and updates a small inline label showing that horizon's confidence value (from D1, since confidence decays with horizon — displaying this number alongside the slider reinforces that further-out predictions are explicitly less certain, another honesty signal worth narrating).

**Data source:** no separate call — uses the `forecasts` array already present in the currently-selected incident's `/full` response, filtered client-side.

**Why it matters:** this is your Innovation 7 made interactive rather than a static chart — letting a judge drag the slider themselves during a demo (rather than just watching you narrate it) is a strong moment of hands-on engagement if your demo format allows audience interaction.

**Build priority:** Day 3, once D1 is producing real forecast polygons — build the slider UI itself earlier against mock horizon data so it's ready to wire in immediately.

---

## 10. Component Build Order (Frontend-Specific, Cross-Referencing the Day Plan)

| Day | Build | Data source status |
|---|---|---|
| Day 1 | Map skeleton (base tiles, pan/zoom working), Header bar, Incident Detail Panel layout, Vessel Popup layout | all against mock/hardcoded JSON matching the exact route response shapes |
| Day 2 | Vessel layer, Spill layer, Incident Summary List, WebSocket connection + bare Alert Feed | wired to real Gateway endpoints as each backend module comes online |
| Day 3 | Dark vessel layer, Risk zone layer, Forecast Overlay + Slider, Backward Attribution Trajectory, visual polish pass, animations on live feed entries | wired to real data throughout; trajectory and forecast are the last two components since they depend on the last services (C2, D1) to finish |

**Critical rule for frontend/backend parallelism:** because every component above lists its exact Gateway route and response shape, the frontend team should never be blocked waiting for backend completion — build every component against a hand-written mock JSON file matching the documented shape from Day 1, and swap the mock fetch for the real Gateway call as a one-line change once that route is live. This is the single most important process decision for hitting a 3-day deadline with a small team.

---

## 11. Visual/Style Quick Reference

- **Color coding, keep consistent everywhere (map markers, badges, bars, feed icons):** LOW = green, MEDIUM = amber/yellow, HIGH = orange, CRITICAL = red. Use the exact same four colors for vessel risk tiers and spill severity tiers even though they're conceptually different scales — visual consistency across the whole UI reduces cognitive load for a viewer seeing this for the first time (a judge), which matters more in a 3-minute demo than scale-purity.
- **Typography:** one clean sans-serif (e.g., Inter), monospace only for IDs/coordinates/mmsi numbers — this small touch reads as "operational tooling" rather than "generic web app."
- **Never show a raw unstyled number where a badge/chip communicates the same thing faster** — severity, risk tier, and ecological exposure should always be colored badges, not plain text, throughout every component.
- **Loading states matter more than usual here** because your demo involves live data arriving progressively — every list/panel needs a real skeleton/spinner state, not a blank flash, or the demo will look broken during natural network/processing delays.
