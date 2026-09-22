import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import rasterio
from rasterio.transform import from_origin
from fusion.metadata import acquisition, oil_spill_visualization, run_fusion

class FusionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.s1 = self.root / 'S1_product.tif'
        with rasterio.open(self.s1, 'w', driver='GTiff', width=4, height=4, count=1,
                           dtype='float32', crs='EPSG:32640', transform=from_origin(500000,2800000,10,10)) as dst:
            dst.write(np.ones((4,4),np.float32),1)
    def tearDown(self):
        self.temp.cleanup()
    def prediction(self):
        mask=np.zeros((4,4),np.uint8); mask[1:3,1:3]=1
        return {'mask':mask,'probability':np.full((4,4),.8,np.float32),
                'mode':'SAR_ONLY','threshold':.5,'weights':None,'oil_spill_detected':True}
    def test_tags_and_explicit_override(self):
        with rasterio.open(self.s1,'r+') as dst: dst.update_tags(TIFFTAG_DATETIME='2026:09:09 10:20:30')
        when,source=acquisition([self.s1],ais_time='2020-01-01T00:00:00Z')
        self.assertEqual(when.isoformat(),'2026-09-09T10:20:30+00:00')
        self.assertEqual(source,'geotiff:TIFFTAG_DATETIME')
        self.assertEqual(acquisition([self.s1],explicit='2025-01-01T00:00:00Z')[1],'explicit')
    def test_missing_time_is_not_processing_time(self):
        with self.assertRaises(ValueError): acquisition([self.s1])
    def test_measured_area_metadata_and_s1_georeference(self):
        with patch('fusion.metadata._detect_oil_result',return_value=self.prediction()):
            result=run_fusion(self.s1,output_dir=self.root/'out',ais_time='2026-09-09T10:00:00Z')
        self.assertEqual(result['scene_id'],'S1_product')
        self.assertEqual(result['acquisition_time_source'],'ais_fallback')
        self.assertEqual(result['georeference_source'],'sentinel1')
        self.assertAlmostEqual(result['area_m2'],400,delta=1)
        self.assertEqual(result['area_km2'],result['area_m2']/1e6)
        candidate = result['candidates'][0]
        self.assertNotIn('spill_id',candidate)
        self.assertEqual(candidate['model_version'], 'onnx-fusion-v1')
        self.assertEqual(candidate['shape']['area_px'], 4)
        self.assertIn('elongation', candidate['shape'])
        self.assertIn('contrast', candidate['texture'])
        self.assertIn('homogeneity', candidate['texture'])
        self.assertIn('gradient', candidate['edge'])
        with rasterio.open(self.root/'out/final_mask.tif') as dst, rasterio.open(self.s1) as src:
            self.assertEqual(dst.crs,src.crs); self.assertEqual(dst.transform,src.transform)
        self.assertEqual(json.loads((self.root/'out/fusion_metadata.json').read_text())['scene_id'],'S1_product')
        self.assertEqual(result['artifacts']['visualization'], 'oil_spill_visualization.png')
        self.assertTrue((self.root/'out/oil_spill_visualization.png').is_file())

    def test_visualization_keeps_ocean_blue_and_darkens_only_mask(self):
        values = np.arange(16, dtype=np.float32).reshape(4, 4)
        mask = np.zeros((4, 4), dtype=np.uint8)
        mask[1:3, 1:3] = 1
        path = self.root / 'visualization.png'
        oil_spill_visualization(values, mask, path)
        from PIL import Image
        pixels = np.asarray(Image.open(path))
        self.assertEqual(pixels.shape, (4, 4, 3))
        self.assertGreater(int(pixels[0, 3, 2]), int(pixels[0, 3, 0]))
        baseline_path = self.root / 'baseline.png'
        oil_spill_visualization(values, np.zeros_like(mask), baseline_path)
        baseline = np.asarray(Image.open(baseline_path))
        self.assertLess(int(pixels[2, 2].mean()), int(baseline[2, 2].mean()))
    def test_eo_only_does_not_export_georeferencing(self):
        with patch('fusion.metadata._detect_oil_result',return_value={**self.prediction(),'mode':'EO_ONLY'}), patch('fusion.metadata._prepare_eo',return_value=self.s1):
            result=run_fusion(sentinel2_path=self.s1,output_dir=self.root/'eo',acquisition_time='2026-09-09T10:00:00Z')
        self.assertIsNone(result['georeference_source'])
        self.assertIsNone(result['area_km2'])
        self.assertEqual(result['candidates'],[])
        self.assertFalse((self.root/'eo/final_mask.tif').exists())

if __name__=='__main__': unittest.main()
