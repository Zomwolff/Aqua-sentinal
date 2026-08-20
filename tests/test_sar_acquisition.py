import os
import sys
import json
import pytest
from unittest.mock import Mock, patch, MagicMock, PropertyMock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services/data-ingestion"))

# Mock ee module before importing sar_acquisition
import ee
ee.Initialize = Mock()
ee.ErrorMargin = Mock(return_value=Mock())
ee.Image = Mock()
ee.ImageCollection = Mock()
ee.Filter = Mock()
ee.batch = Mock()
ee.batch.Export = Mock()
ee.batch.Export.image = Mock()
ee.batch.Export.image.toDrive = Mock()
ee.Geometry = Mock()
ee.ServiceAccountCredentials = Mock()


class TestSARAcquisitionDownload:
    """Tests for SAR acquisition download logic using mocks."""

    def setup_method(self):
        """Set up test environment."""
        os.environ.setdefault("GEE_SERVICE_ACCOUNT", "test@test.iam.gserviceaccount.com")
        os.environ.setdefault("GEE_PRIVATE_KEY_PATH", "/fake/key.json")

    @patch("app.sar_acquisition.ee")
    def test_init_gee_success(self, mock_ee):
        """Test Earth Engine initialization."""
        from app.sar_acquisition import init_gee
        init_gee()
        mock_ee.ServiceAccountCredentials.assert_called_once()
        mock_ee.Initialize.assert_called_once()

    @patch("app.sar_acquisition.ee")
    def test_init_gee_missing_env(self, mock_ee):
        """Test Earth Engine initialization fails without env vars."""
        from app.sar_acquisition import init_gee
        os.environ.pop("GEE_SERVICE_ACCOUNT", None)
        os.environ.pop("GEE_PRIVATE_KEY_PATH", None)
        with pytest.raises(RuntimeError, match="must be set for SAR acquisition"):
            init_gee()

    @patch("app.sar_acquisition.ee")
    def test_coverage_fraction(self, mock_ee):
        """Test coverage fraction calculation."""
        from app.sar_acquisition import _coverage_fraction
        mock_scene_geom = Mock()
        mock_aoi_geom = Mock()
        mock_intersect = Mock()
        mock_scene_geom.intersection.return_value = mock_intersect
        mock_intersect.area.return_value = Mock()
        mock_intersect.area.return_value.divide.return_value.getInfo.return_value = 0.75
        mock_aoi_geom.area.return_value = Mock()

        result = _coverage_fraction(mock_scene_geom, mock_aoi_geom)
        assert result == 0.75
        mock_ee.ErrorMargin.assert_called_with(1)

    @patch("app.sar_acquisition.ee")
    def test_get_sentinel1_scenes(self, mock_ee):
        """Test Sentinel-1 scene query."""
        from app.sar_acquisition import get_sentinel1_scenes

        mock_collection = Mock()
        mock_ee.ImageCollection.return_value.filterBounds.return_value.filterDate.return_value.filter.return_value.filter.return_value = mock_collection
        mock_collection.toList.return_value.getInfo.return_value = [
            {"id": "SCENE_1", "properties": {"acquisition_time": 1234567890}},
            {"id": "SCENE_2", "properties": {"acquisition_time": 1234567891}},
        ]
        mock_ee.Image.return_value.rename.return_value.bandNames.return_value.getInfo.return_value = ["VV"]
        mock_ee.Image.return_value.geometry.return_value = Mock()

        scenes = get_sentinel1_scenes("2024-01-01", "2024-01-31")
        assert len(scenes) == 2
        assert scenes[0]["id"] == "SCENE_1"

    @patch("app.sar_acquisition.ee")
    def test_select_best_scene(self, mock_ee):
        """Test best scene selection."""
        from app.sar_acquisition import select_best_scene

        mock_aoi = Mock()
        mock_ee.Image.return_value.geometry.return_value = mock_aoi

        scenes = [
            {
                "id": "SCENE_1",
                "properties": {"system:time_start": 1000, "acquisition_time": 1000},
                "geometry": Mock(),
                "image": Mock(),
            },
            {
                "id": "SCENE_2",
                "properties": {"system:time_start": 2000, "acquisition_time": 2000},
                "geometry": Mock(),
                "image": Mock(),
            },
        ]
        scenes[0]["geometry"] = Mock()
        scenes[1]["geometry"] = Mock()
        scenes[0]["image"].bandNames.return_value.getInfo.return_value = ["VV"]
        scenes[1]["image"].bandNames.return_value.getInfo.return_value = ["VV"]

        with patch("app.sar_acquisition._coverage_fraction", side_effect=[0.5, 0.8]):
            with patch("app.sar_acquisition.mumbai_aoi_geometry", return_value=mock_aoi):
                best = select_best_scene(scenes)
                assert best["id"] == "SCENE_2"
                assert best["coverage"] == 0.8

    @patch("app.sar_acquisition.ee")
    @patch("app.sar_acquisition.mumbai_aoi_geometry")
    @patch("app.sar_acquisition._coverage_fraction")
    def test_select_best_scene_no_valid(self, mock_coverage, mock_aoi, mock_ee):
        """Test best scene selection raises when no valid scene."""
        from app.sar_acquisition import select_best_scene

        mock_aoi.return_value = Mock()
        mock_coverage.return_value = 0.5

        scenes = [
            {
                "id": "SCENE_1",
                "properties": {"system:time_start": 1000},
                "geometry": Mock(),
                "image": Mock(),
            },
        ]
        scenes[0]["image"].bandNames.return_value.getInfo.return_value = ["VH"]

        with pytest.raises(RuntimeError, match="No suitable Sentinel"):
            select_best_scene(scenes)

    @patch("app.sar_acquisition._build_drive_service")
    @patch("app.sar_acquisition._validate_geotiff")
    @patch("app.sar_acquisition.time.sleep", return_value=None)
    def test_download_exported_file_success(self, mock_sleep, mock_validate, mock_build_drive):
        """Test successful download from Google Drive."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.side_effect = [
            {"state": "RUNNING"},
            {"state": "COMPLETED", "destination_uris": ["https://drive.google.com/file/d/FILE_ID_123/view"]},
        ]

        mock_drive_service = Mock()
        mock_build_drive.return_value = mock_drive_service

        mock_request = Mock()
        mock_drive_service.files.return_value.get_media.return_value = mock_request

        mock_downloader = Mock()
        mock_downloader.next_chunk.side_effect = [
            (Mock(progress=lambda: 0.5), False),
            (Mock(progress=lambda: 1.0), True),
        ]

        with patch("app.sar_acquisition.MediaIoBaseDownload", return_value=mock_downloader):
            with patch("app.sar_acquisition.io.FileIO", return_value=Mock()) as mock_fileio:
                with patch("app.sar_acquisition.os.makedirs"):
                    with patch("app.sar_acquisition.os.path.join", return_value="/data/sar/test.tif"):
                        result = _download_exported_file(mock_task, "test_folder", "test")

        assert result == "/data/sar/test.tif"
        mock_validate.assert_called_once_with("/data/sar/test.tif")
        mock_build_drive.assert_called_once_with("/fake/key.json")

    @patch("app.sar_acquisition.time.sleep", return_value=None)
    def test_download_exported_file_failed(self, mock_sleep):
        """Test download raises on failed EE task."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.return_value = {"state": "FAILED"}

        with pytest.raises(RuntimeError, match="EE export task failed with state FAILED"):
            _download_exported_file(mock_task, "folder", "prefix")

    @patch("app.sar_acquisition.time.sleep", return_value=None)
    def test_download_exported_file_timeout(self, mock_sleep):
        """Test download raises on timeout."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.return_value = {"state": "RUNNING"}

        with pytest.raises(TimeoutError, match="did not complete within"):
            _download_exported_file(mock_task, "folder", "prefix")

    @patch("app.sar_acquisition._build_drive_service")
    @patch("app.sar_acquisition._validate_geotiff")
    @patch("app.sar_acquisition.time.sleep", return_value=None)
    def test_download_exported_file_fallback_search(self, mock_sleep, mock_validate, mock_build_drive):
        """Test fallback file search when destination_uris missing."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.side_effect = [
            {"state": "RUNNING"},
            {"state": "COMPLETED", "destination_uris": []},
        ]

        mock_drive_service = Mock()
        mock_build_drive.return_value = mock_drive_service

        mock_folder_result = Mock()
        mock_folder_result.execute.return_value = {"files": [{"id": "FOLDER_ID"}]}
        mock_drive_service.files.return_value.list.return_value = mock_folder_result

        mock_file_result = Mock()
        mock_file_result.execute.return_value = {"files": [{"id": "FILE_ID_FALLBACK"}]}
        mock_drive_service.files.return_value.list.return_value = mock_file_result

        mock_request = Mock()
        mock_drive_service.files.return_value.get_media.return_value = mock_request

        mock_downloader = Mock()
        mock_downloader.next_chunk.side_effect = [
            (Mock(progress=lambda: 1.0), True),
        ]

        with patch("app.sar_acquisition.MediaIoBaseDownload", return_value=mock_downloader):
            with patch("app.sar_acquisition.io.FileIO", return_value=Mock()):
                with patch("app.sar_acquisition.os.makedirs"):
                    with patch("app.sar_acquisition.os.path.join", return_value="/data/sar/test.tif"):
                        result = _download_exported_file(mock_task, "test_folder", "test")

        assert result == "/data/sar/test.tif"

    @patch("app.sar_acquisition._build_drive_service")
    def test_download_exported_file_fallback_folder_not_found(self, mock_build_drive):
        """Test fallback search raises when folder not found."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.return_value = {"state": "COMPLETED", "destination_uris": []}

        mock_drive_service = Mock()
        mock_build_drive.return_value = mock_drive_service

        mock_folder_result = Mock()
        mock_folder_result.execute.return_value = {"files": []}
        mock_drive_service.files.return_value.list.return_value = mock_folder_result

        with pytest.raises(RuntimeError, match="Drive folder.*not found"):
            _download_exported_file(mock_task, "missing_folder", "test")

    @patch("app.sar_acquisition._build_drive_service")
    def test_download_exported_file_fallback_file_not_found(self, mock_build_drive):
        """Test fallback search raises when file not found in folder."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.return_value = {"state": "COMPLETED", "destination_uris": []}

        mock_drive_service = Mock()
        mock_build_drive.return_value = mock_drive_service

        # First call: folder search
        mock_folder_result = Mock()
        mock_folder_result.execute.return_value = {"files": [{"id": "FOLDER_ID"}]}
        # Second call: file search
        mock_file_result = Mock()
        mock_file_result.execute.return_value = {"files": []}
        mock_drive_service.files.return_value.list.side_effect = [mock_folder_result, mock_file_result]

        with pytest.raises(RuntimeError, match="File.*not found in Drive folder"):
            _download_exported_file(mock_task, "test_folder", "test")

    def test_validate_geotiff_success(self, tmp_path):
        """Test GeoTIFF validation with valid file."""
        from app.sar_acquisition import _validate_geotiff
        import rasterio
        import numpy as np

        test_file = tmp_path / "test.tif"
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": 100,
            "height": 100,
            "count": 1,
            "crs": "EPSG:4326",
            "transform": rasterio.transform.from_bounds(72, 18, 73, 19, 100, 100),
        }
        with rasterio.open(test_file, "w", **profile) as dst:
            dst.write(np.ones((100, 100), dtype=np.float32), 1)

        _validate_geotiff(str(test_file))

    @patch("app.sar_acquisition.rasterio.open")
    def test_validate_geotiff_zero_dimensions(self, mock_rasterio_open):
        """Test GeoTIFF validation fails on zero dimensions."""
        from app.sar_acquisition import _validate_geotiff

        mock_ds = Mock()
        mock_ds.width = 0
        mock_ds.height = 100
        mock_ds.count = 1
        mock_rasterio_open.return_value.__enter__.return_value = mock_ds

        with pytest.raises(ValueError, match="zero dimensions"):
            _validate_geotiff("/fake/path.tif")

    @patch("app.sar_acquisition.rasterio.open")
    def test_validate_geotiff_no_bands(self, mock_rasterio_open):
        """Test GeoTIFF validation fails on zero bands."""
        from app.sar_acquisition import _validate_geotiff

        mock_ds = Mock()
        mock_ds.width = 100
        mock_ds.height = 100
        mock_ds.count = 0
        mock_rasterio_open.return_value.__enter__.return_value = mock_ds

        with pytest.raises(ValueError, match="no bands"):
            _validate_geotiff("/fake/path.tif")

    def test_validate_geotiff_unreadable(self, tmp_path):
        """Test GeoTIFF validation fails on unreadable file."""
        from app.sar_acquisition import _validate_geotiff

        test_file = tmp_path / "test.tif"
        test_file.write_bytes(b"not a geotiff")

        with pytest.raises(Exception):
            _validate_geotiff(str(test_file))

    @patch("app.sar_acquisition.service_account.Credentials.from_service_account_file")
    @patch("app.sar_acquisition.build")
    def test_build_drive_service(self, mock_build, mock_creds):
        """Test Drive service building."""
        from app.sar_acquisition import _build_drive_service

        mock_creds.return_value = Mock()
        mock_build.return_value = Mock()

        result = _build_drive_service("/fake/key.json")

        mock_creds.assert_called_once_with("/fake/key.json", scopes=["https://www.googleapis.com/auth/drive.readonly"])
        mock_build.assert_called_once_with("drive", "v3", credentials=mock_creds.return_value, cache_discovery=False)
        assert result == mock_build.return_value

    @patch("app.sar_acquisition._build_drive_service")
    @patch("app.sar_acquisition._download_from_drive")
    @patch("app.sar_acquisition._validate_geotiff")
    @patch("app.sar_acquisition.time.sleep", return_value=None)
    def test_download_exported_file_download_failure(self, mock_sleep, mock_validate, mock_download, mock_build_drive):
        """Test download raises on Drive API failure."""
        from app.sar_acquisition import _download_exported_file

        mock_task = Mock()
        mock_task.status.side_effect = [
            {"state": "RUNNING"},
            {"state": "COMPLETED", "destination_uris": ["https://drive.google.com/file/d/FILE_ID_123/view"]},
        ]

        mock_download.side_effect = Exception("Download failed")

        with pytest.raises(Exception, match="Download failed"):
            _download_exported_file(mock_task, "folder", "prefix")

    def test_download_exported_file_zero_byte_rejected(self, tmp_path):
        """Test that zero-byte file is rejected."""
        from app.sar_acquisition import _validate_geotiff

        test_file = tmp_path / "empty.tif"
        test_file.write_bytes(b"")

        with pytest.raises(Exception):
            _validate_geotiff(str(test_file))


class TestSARAcquisitionIntegration:
    """Integration-style tests for the SAR acquisition flow."""

    @patch("app.sar_acquisition.init_gee")
    @patch("app.sar_acquisition.get_sentinel1_scenes")
    @patch("app.sar_acquisition.select_best_scene")
    @patch("app.sar_acquisition.export_scene_metadata")
    @patch("app.sar_acquisition.publish_sar_scene")
    def test_run_sar_acquisition_flow(self, mock_publish, mock_export, mock_select, mock_get_scenes, mock_init):
        """Test the full acquisition pipeline is called correctly."""
        from app.sar_acquisition import run_sar_acquisition

        mock_get_scenes.return_value = [{"id": "SCENE_1"}]
        mock_select.return_value = {"id": "SCENE_1"}
        mock_export.return_value = ("/data/sar/test.tif", {"scene_id": "SCENE_1"})

        run_sar_acquisition("2024-01-01", "2024-01-31", inject_synthetic=False)

        mock_init.assert_called_once()
        mock_get_scenes.assert_called_once_with("2024-01-01", "2024-01-31")
        mock_select.assert_called_once()
        mock_export.assert_called_once()
        mock_publish.assert_called_once_with("/data/sar/test.tif", {"scene_id": "SCENE_1"})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])