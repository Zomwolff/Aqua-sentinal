import os
import requests
import ee

key_path = "/run/secrets/gee-key.json"
service_account = "oil-spill-sar-sih@learn-langchain-3bf19.iam.gserviceaccount.com"

credentials = ee.ServiceAccountCredentials(service_account, key_path)
ee.Initialize(credentials)

roi = ee.Geometry.Polygon([[
    [72.7, 18.8],
    [72.9, 18.8],
    [72.9, 19.0],
    [72.7, 19.0]
]])

print("Querying Earth Engine for Sentinel-1 GRD imagery...")
collection = (
    ee.ImageCollection("COPERNICUS/S1_GRD")
    .filterBounds(roi)
    .filter(ee.Filter.eq("instrumentMode", "IW"))
    .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    .sort('system:time_start', False)
)

image = collection.first().select('VV')
scene_id = image.get('system:id').getInfo().replace('/', '_')

print(f"Generating download URL for {scene_id} in EPSG:4326...")
url = image.getDownloadURL({
    'scale': 10,
    'region': roi,
    'format': 'GEO_TIFF',
    'crs': 'EPSG:4326'  # Explicitly request EPSG:4326 so the backend accepts it
})
print("URL generated. Downloading...")

res = requests.get(url)
res.raise_for_status()

output_path = f"/tmp/{scene_id}.tif"
with open(output_path, "wb") as f:
    f.write(res.content)

print(f"Successfully saved to {output_path}")
