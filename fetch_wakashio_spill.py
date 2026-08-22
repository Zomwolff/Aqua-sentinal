import os
import urllib.request
import ee

key_path = "/run/secrets/gee-key.json"
service_account = "oil-spill-sar-sih@learn-langchain-3bf19.iam.gserviceaccount.com"

print("Authenticating with Earth Engine...")
credentials = ee.ServiceAccountCredentials(service_account, key_path)
ee.Initialize(credentials)

# Coordinates for the Wakashio oil spill (Mauritius, August 2020)
roi = ee.Geometry.Polygon([[
    [57.65, -20.45],
    [57.85, -20.45],
    [57.85, -20.30],
    [57.65, -20.30]
]])

print("Querying Earth Engine for Sentinel-1 GRD imagery of Wakashio spill (Aug 2020)...")
collection = (
    ee.ImageCollection("COPERNICUS/S1_GRD")
    .filterBounds(roi)
    .filterDate("2020-08-05", "2020-08-15")
    .filter(ee.Filter.eq("instrumentMode", "IW"))
    .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
    .sort('system:time_start')
)

image_list = collection.toList(1)
size = image_list.size().getInfo()
if size == 0:
    print("No images found for Wakashio spill.")
    exit(1)

print(f"Found {size} image(s). Processing the best one...")
image = ee.Image(image_list.get(0)).select('VV')
scene_id = image.get('system:id').getInfo().replace('/', '_')

print(f"Generating download URL for {scene_id}...")
url = image.getDownloadURL({
    'scale': 10,
    'region': roi,
    'format': 'GEO_TIFF',
    'crs': 'EPSG:4326'
})

output_path = f"/tmp/wakashio_raw.tif"
print(f"Downloading to {output_path}...")
urllib.request.urlretrieve(url, output_path)
print(f"Saved {output_path}")

print("Done. Please strip the CRS before using.")
