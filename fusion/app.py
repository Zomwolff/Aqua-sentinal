"""Manual upload/ONNX mask service; distinct from automated evidence-fusion."""

from pathlib import Path
import shutil, uuid
import numpy as np
import rasterio
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image
from fusion.config import SAR_ONNX, EO_ONNX, SAR_THRESHOLD, EO_THRESHOLD
from fusion.onnx_runtime import _sar_probability, _eo_probability, _prepare_eo
from fusion.alignment import align_sar_probability
from fusion.pipeline import detect_oil_result

app=FastAPI(); ROOT=Path('/data/artifacts/fusion')
def png(a,p,mask=False):
 a=np.asarray(a,float); v=(a>0).astype('uint8')*255 if mask else np.clip((a-np.nanpercentile(a,2))/max(np.nanpercentile(a,98)-np.nanpercentile(a,2),1e-8)*255,0,255).astype('uint8'); Image.fromarray(v).save(p)
async def save(f,p):
 b=await f.read()
 if not b: raise HTTPException(422,'Empty upload')
 p.write_bytes(b)
@app.get('/health')
def health(): return {'status':'ok','sar_threshold':SAR_THRESHOLD,'eo_threshold':EO_THRESHOLD}
@app.post('/upload')
async def upload(sentinel1:UploadFile=File(...),sentinel2:UploadFile|None=File(None),sentinel2_bands:list[UploadFile]|None=File(None)):
 if bool(sentinel2)==bool(sentinel2_bands): raise HTTPException(422,'Provide one S2 multiband TIFF or individual S2 bands')
 run=uuid.uuid4().hex; out=ROOT/run; inp=out/'inputs'; inp.mkdir(parents=True)
 try:
  s1=inp/'s1.tif'; await save(sentinel1,s1)
  if sentinel2: s2=inp/'s2.tif'; await save(sentinel2,s2)
  else:
   s2=inp/'s2_bands'; s2.mkdir()
   for f in sentinel2_bands or []: await save(f,s2/Path(f.filename or 'band.tif').name)
   print("S2_BANDS_SAVED:", [(p.name, p.stat().st_size) for p in s2.iterdir()], flush=True)
  sar=_sar_probability(s1,SAR_ONNX); sp=out/'sar.tif'
  with rasterio.open(s1) as x:
   prof=x.profile.copy(); prof.update(count=1,dtype='float32',nodata=0)
   with rasterio.open(sp,'w',**prof) as d:d.write(sar,1)
   png(x.read(1),out/'s1.png')
  eo,eo_valid=_eo_probability(s2,EO_ONNX); prep=out/'prep'; prep.mkdir(); ref=_prepare_eo(s2,prep); ep=out/'eo.tif'
  with rasterio.open(ref) as x:
   prof=x.profile.copy(); prof.update(count=1,dtype='float32',nodata=0)
   with rasterio.open(ep,'w',**prof) as d:d.write(eo,1)
   png(x.read(1),out/'s2.png')
  aligned,valid=align_sar_probability(sp,ep); png((aligned>=SAR_THRESHOLD)&valid,out/'sar_mask.png',True)
  result=detect_oil_result(s1,s2); png(result['mask'],out/'final_mask.png',True)
  return {'mode':result['mode'],'oil_spill_detected':result['oil_spill_detected'],'weights':result['weights'],'artifacts':{k:f'/artifacts/fusion/{run}/{v}' for k,v in {'s1':'s1.png','sar':'sar_mask.png','s2':'s2.png','final':'final_mask.png'}.items()}}
 except Exception as e: shutil.rmtree(out,ignore_errors=True); raise HTTPException(422,str(e))
