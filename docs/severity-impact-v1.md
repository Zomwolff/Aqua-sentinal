# Severity-Impact V1

## 1. Overview
Severity-Impact V1 is a rule-based consequence assessment engine. It sits downstream in the Aqua Sentinel pipeline, consuming data from the Oil Spread V2 and Ecological Impact V1 services (specifically via the `spill.ecological` stream). Its primary problem to solve is to provide a single, authoritative severity level for an oil spill by evaluating both ecological and socioeconomic impacts across multiple forecast horizons and probability footprints, replacing previous arbitrary weighted formulas with deterministic rules.

## 2. Why This Approach
- **Consequence/Exposure-based Severity:** Base severity on the dominant consequence (maximum receptor impact) to ensure worst-case scenarios are flagged accurately.
- **Deterministic Rules vs. Arbitrary Formula:** A rule-based approach (e.g., maximum tier) is more explainable and robust than a weighted sum (the old 30/30/20/20 formula), preventing severe impacts in one domain from being diluted by zero impacts in another.
- **Forecast Horizons & Probabilities:** Uses horizons (current, 1h, 3h, 6h, 12h, 24h) and footprints (`best_estimate`, `probability_90`) to provide a complete temporal picture of the threat, acknowledging uncertainty in forecast models.
- **Separated Impacts:** Ecological and socioeconomic impacts are evaluated independently before escalation, preventing double-counting (e.g., MPAs) and allowing domain-specific rules.

## 3. How Severity Is Calculated
The complete calculation flow is as follows:

1. **Inputs:** Consumes forecast data (Oil Spread V2) and ecological impacts (Ecological Impact V1) from the `spill.ecological` event pipeline.
2. **Ecological Severity Aggregation:** Takes the maximum (dominant) receptor category among all affected ecological receptors (None < Low < Medium < High < Critical).
3. **Socioeconomic V1 Proxy:** Determines socioeconomic impact strictly via proximity rules: Port within 5km → High; Fishing zone within 10km → Medium; neither → Low.
4. **TTFE Urgency:** Calculates Time-To-First-Exposure (TTFE) in hours to gauge immediate threat (e.g., < 3 hours).
5. **Breadth Escalation:** If 2 or more distinct ecological receptors have High or Critical exposure, base severity is escalated by +1 tier.
6. **Cross-domain Escalation:** If both Ecological severity and Socioeconomic severity are High or greater, base severity is escalated by +1 tier.
7. **Total Escalation Cap:** Total escalation is capped at +1 tier overall, and cannot exceed "Critical".
8. **Final Severity Level:** The escalated tier becomes the authoritative severity level.
9. **Secondary Score (0-100):** A score is calculated based on fixed, non-overlapping bands (None: 0, Low: 1-24, Medium: 25-49, High: 50-74, Critical: 75-100). Within the band, the score is determined by 50% exposure intensity, 30% breadth ratio, and 20% TTFE urgency factor.
10. **Crucial Rule:** The secondary score does **NOT** override the authoritative severity level; it only provides within-category granularity.

## 4. Overall / Peak / Trajectory
- **Overall:** Represents the current status, calculated using the `current` horizon and `probability_90` footprint (or best available).
- **Peak:** The maximum severity recorded across *all* horizons and footprints, stored alongside the `peak_horizon_hours` and `peak_footprint_type`.
- **Trajectory:** A comparison between the current severity and the 24h `probability_90` severity. Returns `IMPROVING`, `STABLE`, or `WORSENING`.
- **Why Separate:** Overall defines the immediate response posture, Peak highlights the worst-case future scenario for resource staging, and Trajectory informs the trend of the incident.

## 5. What Data It Uses

| Data Source | Origin / Where it comes from | Why it is used |
| :--- | :--- | :--- |
| **Forecast data** | Oil Spread V2 (`forecasts` table) | To provide spatial extents for multi-horizon impact assessment. |
| **Mangroves** | Ecological Impact V1 (`ecological_impact` table) | High-value coastal ecological receptor. |
| **Coral reefs** | Ecological Impact V1 (`ecological_impact` table) | High-value marine ecological receptor. |
| **MPAs** | Ecological Impact V1 (`ecological_impact` table) | Marine Protected Areas (ecological receptor only). |
| **Sensitive coastline** | Ecological Impact V1 (`ecological_impact` table) | General ecological coastal receptor. |
| **Ports** | `protected_areas` table | Used as a socioeconomic proxy for economic/infrastructure exposure. |
| **Fishing zones** | `protected_areas` table | Used as a socioeconomic proxy for livelihood exposure. |
| **TTFE** | Forecast / `spill.ecological` event | Time-to-first-exposure drives the urgency factor and escalation rules. |
| **Physical/forecast metrics** | `spill.ecological` event | Metrics like `physical_area_m2` and `drift_distance_m` provide physical context for the score. |

## 6. Output / Database
Important fields in the `severity` table:
- `severity_level`: Authoritative categorical severity (LOW, MODERATE, HIGH, CRITICAL).
- `score`: Secondary 0-100 numeric score reflecting within-category intensity.
- `ecological_severity`: Independent severity tier for ecological domain.
- `socioeconomic_severity`: Independent severity tier for socioeconomic domain.
- `peak_*` fields: Metadata for the maximum severity across all forecasts (e.g., `peak_severity`, `peak_horizon_hours`).
- `trajectory`: Trend indicator (IMPROVING, STABLE, WORSENING).
- `primary_drivers`: JSON array of reasons/rules that triggered the current severity (e.g., port proximity, coral reef exposure).
- `escalation_*` fields: Flags and reasons if rule-based tier escalation was applied.
- `horizon_hours` & `footprint_type`: Forecast metadata defining which model slice the record represents.
- `methodology_version`: String identifying the algorithm version (e.g., `severity-impact-v1`).

## 7. API
- **Swagger/OpenAPI URL:** `/docs` on the Severity-Impact service or API Gateway.
- **Severity Endpoint:** `GET /severity/{spill_id}` (Severity-Impact internal service).
- **Gateway Endpoint:** `GET /spill/incidents/{spill_id}/severity` (API Gateway external route).
- **Query Parameters:** `horizon_hours` (float) and `footprint_type` (string).
- **HTTP Methods:** `GET`

**Representative JSON Example:**
```json
{
  "id": 105,
  "spill_id": "a1b2c3d4-e5f6-7890-1234-56789abcdef0",
  "severity_level": "CRITICAL",
  "score": 85.5,
  "horizon_hours": 24.0,
  "footprint_type": "probability_90",
  "methodology_version": "severity-impact-v1",
  "peak_severity": "CRITICAL",
  "peak_horizon_hours": 24.0,
  "peak_footprint_type": "probability_90",
  "trajectory": "WORSENING",
  "ecological_severity": "High",
  "socioeconomic_severity": "High",
  "escalation_applied": true,
  "escalation_reasons": ["cross_domain_escalation_both_high"],
  "primary_drivers": ["port_within_5km_count_2"],
  "ttfe_hours": 2.5
}
```

## 8. Known V1 Limitations
- Socioeconomic population and economic exposure is currently a proximity proxy based strictly on ports and fishing zones.
- There is no real population exposure model or coastal settlement data available yet.
- Score weights and fixed bands (e.g., 50/30/20 intensity components) are V1 project-defined defaults and may require future calibration.
- Historical and scientific forecast validation is still outstanding.

## 9. Methodology Version
- **Version String:** `severity-impact-v1`
- **Why Versioning is Stored:** Methodology versioning is stored in the database to track when algorithms change, ensure data provenance, and maintain reproducibility of historical severity outputs if rules are tweaked in the future.
