import os
import urllib.request
import ee

key_path = "/run/secrets/gee-key.json"
service_account = "oil-spill-sar-sih@learn-langchain-3bf19.iam.gserviceaccount.com"

credentials = ee.ServiceAccountCredentials(service_account, key_path)
ee.Initialize(credentials)

regions = [
    {"name": "English Channel", "coords": [[0.5, 50.5], [0.7, 50.5], [0.7, 50.7], [0.5, 50.7]]},
    {"name": "Singapore Strait", "coords": [[103.8, 1.1], [104.0, 1.1], [104.0, 1.3], [103.8, 1.3]]},
    {"name": "Gulf of Mexico", "coords": [[-90.0, 28.0], [-89.8, 28.0], [-89.8, 28.2], [-90.0, 28.2]]},
    {"name": "Strait of Hormuz", "coords": [[55.2, 26.3], [55.4, 26.3], [55.4, 26.5], [55.2, 26.5]]},
    {"name": "Mediterranean", "coords": [[33.0, 34.5], [33.2, 34.5], [33.2, 34.7], [33.0, 34.7]]},
    {"name": "Tokyo Bay", "coords": [[139.6, 35.2], [139.8, 35.2], [139.8, 35.4], [139.6, 35.4]]},
    {"name": "Cape of Good Hope", "coords": [[18.3, -34.5], [18.5, -34.5], [18.5, -34.3], [18.3, -34.3]]},
    {"name": "Panama Canal", "coords": [[-79.9, 9.4], [-79.7, 9.4], [-79.7, 9.6], [-79.9, 9.6]]},
    {"name": "Suez Canal", "coords": [[32.2, 31.2], [32.4, 31.2], [32.4, 31.4], [32.2, 31.4]]},
    {"name": "North Sea", "coords": [[3.0, 53.0], [3.2, 53.0], [3.2, 53.2], [3.0, 53.2]]}
]

print("Querying Earth Engine for 10 diverse regions...")
for i, region in enumerate(regions):
    print(f"[{i+1}/10] {region['name']}...")
    roi = ee.Geometry.Polygon([region['coords']])
    collection = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterBounds(roi)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .sort('system:time_start', False)
    )
    image = collection.first().select('VV')
    url = image.getDownloadURL({'scale': 10, 'region': roi, 'format': 'GEO_TIFF', 'crs': 'EPSG:4326'})
    tmp_path = f"/tmp/raw_sample_{i+1}.tif"
    urllib.request.urlretrieve(url, tmp_path)
print("Downloads complete.")
