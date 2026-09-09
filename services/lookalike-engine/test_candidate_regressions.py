"""Regression checks for candidate isolation and smoothed dark boundaries."""
import unittest
import numpy as np
from scipy.ndimage import gaussian_filter
from app.shape_filters import is_likely_calm_water, _edge_gradient
from shared.artifacts import candidate_pixel_mask, save_scene_artifact
import tempfile
from pathlib import Path


class CandidateRegressions(unittest.TestCase):
    def test_database_decimal_area_is_a_numeric_event_field(self):
        from decimal import Decimal
        from app.worker import _filtered_event
        event = _filtered_event({}, {
            'candidate_id': 'test', 'scene_id': 'scene', 'confidence': 0.7,
            'classification_label': 'possible_oil_spill', 'area_m2': Decimal('1234.56'),
        })
        self.assertIsInstance(event['area_m2'], float)
        self.assertEqual(event['area_m2'], 1234.56)

    def test_smoothed_dark_interior_is_not_rejected_by_boundary_alone(self):
        mask = np.zeros((80, 80), dtype=bool)
        mask[20:60, 20:60] = True
        image = gaussian_filter(np.where(mask, -26.0, -20.0), sigma=4)
        self.assertLess(_edge_gradient(image, mask), 3.0)
        self.assertFalse(is_likely_calm_water({}, image, dark_mask=mask))

    def test_low_contrast_water_still_rejected(self):
        mask = np.zeros((80, 80), dtype=bool)
        mask[20:60, 20:60] = True
        image = np.where(mask, -21.0, -20.0)
        self.assertTrue(is_likely_calm_water({}, image, dark_mask=mask))

    def test_no_background_does_not_claim_calm_water(self):
        mask = np.ones((20, 20), dtype=bool)
        self.assertFalse(is_likely_calm_water({}, np.full(mask.shape, -25.0), dark_mask=mask))

    def test_polygon_mask_preserves_holes_and_excludes_neighbours(self):
        geometry = {"type": "Polygon", "coordinates": [
            [[2, -2], [8, -2], [8, -8], [2, -8], [2, -2]],
            [[4, -4], [6, -4], [6, -6], [4, -6], [4, -4]],
        ]}
        mask = candidate_pixel_mask(geometry, [1, 0, 0, 0, -1, 0], (0, 10, 0, 10))
        self.assertEqual(int(mask.sum()), 32)
        self.assertFalse(mask[0, 0])
        self.assertFalse(mask[4, 4])
        self.assertTrue(mask[2, 2])

    def test_candidate_preview_is_separate_from_bright_targets(self):
        with tempfile.TemporaryDirectory() as root:
            candidate = np.eye(10, dtype=bool)
            path = save_scene_artifact(root, 'scene', filtered_image=np.zeros((10, 10)),
                cleaned_mask=candidate, bright_target_mask=np.zeros((10, 10), dtype=bool),
                candidate_mask=candidate, affine=[1, 0, 0, 0, -1, 0], shape=(10, 10))
            self.assertTrue((Path(path) / 'candidate_mask.png').exists())
            np.testing.assert_array_equal(np.load(Path(path) / 'candidate_mask.npy'), candidate)


if __name__ == '__main__':
    unittest.main()
