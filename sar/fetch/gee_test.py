import ee
import os

service_account = os.getenv("GEE_SERVICE_ACCOUNT")
private_key_path = os.getenv("GEE_PRIVATE_KEY_PATH")
credentials = ee.ServiceAccountCredentials(service_account, private_key_path)
ee.Initialize(credentials)

col = ee.ImageCollection("COPERNICUS/S1_GRD").limit(1)
info = col.getInfo()
if info and "features" in info and len(info["features"]) > 0:
    props = info["features"][0]["properties"]
    print(list(props.keys()))
