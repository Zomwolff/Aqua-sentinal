from pathlib import Path
import argparse, csv, json
import numpy as np
import rasterio
import torch
import torch.nn as nn

FEATURE_NAMES=['B2','B3','B4','B5','B6','B7','B8','B8A','B11','B12']
ALIASES={'B2':['B2','B02'],'B3':['B3','B03'],'B4':['B4','B04'],'B5':['B5','B05'],'B6':['B6','B06'],'B7':['B7','B07'],'B8':['B8','B08'],'B8A':['B8A','B08A'],'B11':['B11'],'B12':['B12']}
FIXED_SIZE=256; OVERLAP=32
DEVICE=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
DEFAULT_CHECKPOINT=Path(__file__).resolve().parent/'sentinel2_unet_binary_best.pth'

class DoubleConv(nn.Module):
    def __init__(self,in_ch,out_ch):
        super().__init__(); self.block=nn.Sequential(nn.Conv2d(in_ch,out_ch,3,padding=1),nn.BatchNorm2d(out_ch),nn.ReLU(inplace=True),nn.Conv2d(out_ch,out_ch,3,padding=1),nn.BatchNorm2d(out_ch),nn.ReLU(inplace=True))
    def forward(self,x): return self.block(x)
class BinaryUNet(nn.Module):
    def __init__(self,in_channels=10,base=32):
        super().__init__(); self.enc1=DoubleConv(in_channels,base); self.enc2=DoubleConv(base,base*2); self.enc3=DoubleConv(base*2,base*4); self.enc4=DoubleConv(base*4,base*8); self.pool=nn.MaxPool2d(2); self.bottleneck=DoubleConv(base*8,base*16); self.up4=nn.ConvTranspose2d(base*16,base*8,2,2); self.dec4=DoubleConv(base*16,base*8); self.up3=nn.ConvTranspose2d(base*8,base*4,2,2); self.dec3=DoubleConv(base*8,base*4); self.up2=nn.ConvTranspose2d(base*4,base*2,2,2); self.dec2=DoubleConv(base*4,base*2); self.up1=nn.ConvTranspose2d(base*2,base,2,2); self.dec1=DoubleConv(base*2,base); self.classifier=nn.Conv2d(base,1,1)
    def forward(self,x):
        e1=self.enc1(x); e2=self.enc2(self.pool(e1)); e3=self.enc3(self.pool(e2)); e4=self.enc4(self.pool(e3)); b=self.bottleneck(self.pool(e4)); d4=self.dec4(torch.cat([self.up4(b),e4],1)); d3=self.dec3(torch.cat([self.up3(d4),e3],1)); d2=self.dec2(torch.cat([self.up2(d3),e2],1)); d1=self.dec1(torch.cat([self.up1(d2),e1],1)); return self.classifier(d1).squeeze(1)

def cname(s):
    n=str(s).upper().replace('-','').replace('_',''); return {'B2':'B2','B02':'B2','B3':'B3','B03':'B3','B4':'B4','B04':'B4','B5':'B5','B05':'B5','B6':'B6','B06':'B6','B7':'B7','B07':'B7','B8':'B8','B08':'B8','B8A':'B8A','B08A':'B8A','B11':'B11','B12':'B12'}.get(n)
def load_model(p):
    c=torch.load(p,map_location=DEVICE,weights_only=False); m=BinaryUNet(10,int(c.get('base_channels',32))).to(DEVICE); m.load_state_dict(c['model_state_dict'],strict=True); m.eval(); mean=np.asarray(c['normalization_mean'],np.float32); std=np.asarray(c['normalization_std'],np.float32); th=float(c.get('best_threshold',.5)); return m,mean,std,th,c
def positions(n,t,o):
    if n<=t:return [0]
    step=t-o; a=list(range(0,n-t+1,step));
    if a[-1]!=n-t:a.append(n-t)
    return a
def weight(h,w):
    y,x=np.mgrid[0:h,0:w]; cy=(h-1)/2; cx=(w-1)/2; sy=max(h/3,1); sx=max(w/3,1); return np.maximum(np.exp(-.5*(((y-cy)/sy)**2+((x-cx)/sx)**2)).astype(np.float32),1e-3)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('input',type=Path); ap.add_argument('--checkpoint',type=Path,default=DEFAULT_CHECKPOINT); ap.add_argument('--output-dir',type=Path,default=None); ap.add_argument('--threshold',type=float,default=None); a=ap.parse_args(); out=a.output_dir or a.input.parent/(a.input.stem+'_fusion_features'); out.mkdir(parents=True,exist_ok=True)
    model,mean,std,cth,ck=load_model(a.checkpoint); th=cth if a.threshold is None else a.threshold
    with rasterio.open(a.input) as src:
        mp={};
        for i,d in enumerate(src.descriptions,1):
            c=cname(d)
            if c and c not in mp: mp[c]=i
        miss=[b for b in FEATURE_NAMES if b not in mp]
        if miss: raise ValueError(f'Missing required bands: {miss}')
        H,W=src.height,src.width; raw=np.stack([src.read(mp[b]).astype(np.float32) for b in FEATURE_NAMES]); valid=np.all(np.isfinite(raw),0); norm=(raw-mean[:,None,None])/std[:,None,None]; norm[:,~valid]=0
        ps=np.zeros((H,W),np.float32); ws=np.zeros((H,W),np.float32); ys=positions(H,FIXED_SIZE,OVERLAP); xs=positions(W,FIXED_SIZE,OVERLAP)
        with torch.no_grad():
            for y in ys:
                for x in xs:
                    tile=norm[:,y:y+FIXED_SIZE,x:x+FIXED_SIZE]; hh,ww=tile.shape[1:]; ph=FIXED_SIZE-hh; pw=FIXED_SIZE-ww
                    if ph or pw: tile=np.pad(tile,((0,0),(0,ph),(0,pw)))
                    p=torch.sigmoid(model(torch.from_numpy(tile[None]).to(DEVICE)))[0].cpu().numpy()[:hh,:ww]; z=weight(hh,ww); ps[y:y+hh,x:x+ww]+=p*z; ws[y:y+hh,x:x+ww]+=z
        prob=ps/np.maximum(ws,1e-8); prob[~valid]=0; mask=((prob>=th)&valid).astype(np.uint8)
        base=src.profile.copy(); base.update(count=1,dtype='float32',compress='deflate',predictor=2)
        with rasterio.open(out/'oil_probability.tif','w',**base) as d:d.write(prob,1)
        bm=base.copy(); bm.update(dtype='uint8',nodata=0)
        with rasterio.open(out/'oil_mask.tif','w',**bm) as d:d.write(mask,1)
        st=src.profile.copy(); st.update(count=10,dtype='float32',compress='deflate',predictor=2)
        with rasterio.open(out/'spectral_raw_10band.tif','w',**st) as d:
            d.write(raw)
            [d.set_band_description(i,b) for i,b in enumerate(FEATURE_NAMES,1)]
        with rasterio.open(out/'spectral_normalized_10band.tif','w',**st) as d:
            d.write(norm); [d.set_band_description(i,b+'_normalized') for i,b in enumerate(FEATURE_NAMES,1)]
        np.savez_compressed(out/'fusion_features.npz',raw_reflectance=raw,normalized_bands=norm,oil_probability=prob,oil_mask=mask,valid_mask=valid.astype(np.uint8),band_names=np.array(FEATURE_NAMES),normalization_mean=mean,normalization_std=std,threshold=np.float32(th))
        with open(out/'band_statistics.csv','w',newline='') as f:
            w=csv.writer(f); w.writerow(['band','tiff_band','min','max','mean','std','normalization_mean','normalization_std'])
            for i,b in enumerate(FEATURE_NAMES):
                v=raw[i][valid]; w.writerow([b,mp[b],float(v.min()),float(v.max()),float(v.mean()),float(v.std()),float(mean[i]),float(std[i])])
        meta={'input':str(a.input),'checkpoint':str(a.checkpoint),'architecture':'BinaryUNet','base_channels':int(ck.get('base_channels',32)),'feature_names':FEATURE_NAMES,'tiff_band_mapping':mp,'width':W,'height':H,'crs':str(src.crs) if src.crs else None,'threshold':float(th),'checkpoint_threshold':cth,'normalization_mean':mean.tolist(),'normalization_std':std.tolist(),'tile_size':FIXED_SIZE,'tile_overlap':OVERLAP,'blending':'center-weighted overlapping tile average','valid_pixels':int(valid.sum()),'predicted_oil_pixels':int(mask.sum()),'predicted_oil_fraction':float(mask.sum()/max(valid.sum(),1)),'outputs':['oil_probability.tif','oil_mask.tif','spectral_raw_10band.tif','spectral_normalized_10band.tif','fusion_features.npz','band_statistics.csv','metadata.json']}
        with open(out/'metadata.json','w') as f:json.dump(meta,f,indent=2)
    print('DONE'); print(out)
if __name__=='__main__':main()
