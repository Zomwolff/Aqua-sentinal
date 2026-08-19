# Aqua-Sentinel Spatial Rule

## The rule

```
All stored coordinates use:

    GEOMETRY(...,4326)

EPSG:4326 stores longitude/latitude as angular degrees, NOT meters.

For real-world distances:

    cast geometry to geography.

Example:

    ST_Distance(
        geom1::geography,
        geom2::geography
    )

returns meters.

For radius queries:

    ST_DWithin(
        geom1::geography,
        geom2::geography,
        radius_meters
    )

Topology operations such as:

    ST_Intersects
    ST_Contains
    ST_Within

should normally remain geometry-based unless there is a specific reason otherwise.

Always use:

    POINT(longitude latitude)

not:

    POINT(latitude longitude).
```

## Why

All spatial columns in this project are `GEOMETRY(...,4326)`. That is a deliberate,
verified decision — do NOT change columns to `geography` and do NOT change SRID.

Because 4326 geometry coordinates are degrees, these are WRONG if the value is
interpreted as meters:

```sql
ST_Distance(a.geom, b.geom)                 -- degrees, not meters
ST_DWithin(a.geom, b.geom, 1000)            -- 1000 degrees, not 1000 meters
```

Use the geography cast instead:

```sql
ST_Distance(a.geom::geography, b.geom::geography)                  -- meters
ST_DWithin(a.geom::geography, b.geom::geography, 1000)             -- 1000 meters
ST_Distance(a.geom::geography, b.geom::geography) / 1000.0         -- kilometers
```

## Distance vs. topology

| Operation                    | Type            | Keep as        |
|------------------------------|-----------------|----------------|
| `ST_Distance` (interpreted as meters) | distance | `::geography` |
| `ST_DWithin` (radius in meters) | distance | `::geography` |
| `ST_Intersects`              | topology        | geometry       |
| `ST_Contains`                | topology        | geometry       |
| `ST_Within`                  | topology        | geometry       |
| `ST_Buffer(..., <meters>)`   | distance-based  | `::geography` |

Example spatial-distance workflows:

- Vessel near spill: `ST_DWithin(vessel_positions.geom::geography, spill_incidents.centroid::geography, 10000)`
- Vessel-to-vessel (STS): `ST_DWithin(a.geom::geography, b.geom::geography, 500)`
- Dark-vessel match: `ST_DWithin(dark_vessel.geom::geography, vessel_positions.geom::geography, :radius_meters)`
- Spill-to-protected-area distance: `ST_Distance(spill_incidents.geom::geography, protected_areas.geom::geography)`
- Spill-overlaps-protected-area (topology): `ST_Intersects(spill_incidents.geom, protected_areas.geom)`
- Nearest environmental condition: `ST_Distance(environmental_conditions.geom::geography, spill_incidents.centroid::geography)`

## Coordinate order

PostGIS points are always `POINT(longitude latitude)`:

```sql
-- longitude = 73.92, latitude = 15.20
ST_SetSRID(ST_MakePoint(73.92, 15.20), 4326)
```

## Shared helpers

Use the shared spatial helpers so every service follows the same convention
(`shared/spatial`):

```python
from shared.spatial import distance_m_sql, distance_km_sql, dwithin_sql, make_point_sql, km_to_m

km_to_m(10)                       # 10000.0
make_point_sql(73.92, 15.20)      # ST_SetSRID(ST_MakePoint(73.92, 15.2), 4326)
distance_m_sql("v.geom", "s.centroid")   # ST_Distance(v.geom::geography, s.centroid::geography)
dwithin_sql("v.geom", "s.centroid", 10000)  # ST_DWithin(v.geom::geography, s.centroid::geography, 10000)
```

The helpers emit geography-cast SQL; topology operations (intersects/contains/within)
stay geometry-based. Prefer PostGIS filtering over Python-side distance math.

## Naming units explicitly

Prefer explicit variable names:

- `radius_meters` over `radius`
- `distance_meters` over `distance`
- `distance_km` only when the value is actually kilometers

Never store a value named `distance_km` unless the application really converted
meters to kilometers.