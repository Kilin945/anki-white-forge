"""⌘F Clear Flagged：清欄位前把圖片來源記成退圖（image_rejects.json）。假 aqt 來自 conftest。"""
import json
import addon


class TestRecordImageReject:
    def test_writes_lowercase_key(self, tmp_path):
        p = tmp_path / "r.json"
        assert addon._images._record_image_reject("Colonised", "pexels:12", path=str(p)) is True
        assert json.loads(p.read_text()) == {"colonised": ["pexels:12"]}

    def test_appends_and_dedupes(self, tmp_path):
        p = tmp_path / "r.json"
        addon._images._record_image_reject("w", "pexels:1", path=str(p))
        addon._images._record_image_reject("w", "wikimedia:2", path=str(p))
        addon._images._record_image_reject("w", "pexels:1", path=str(p))
        assert json.loads(p.read_text()) == {"w": ["pexels:1", "wikimedia:2"]}

    def test_empty_tag_does_not_write(self, tmp_path):
        p = tmp_path / "r.json"
        assert addon._images._record_image_reject("w", "", path=str(p)) is False
        assert not p.exists()

    def test_broken_file_is_rebuilt(self, tmp_path):
        p = tmp_path / "r.json"; p.write_text("{oops")
        assert addon._images._record_image_reject("w", "pexels:", path=str(p)) is True
        assert json.loads(p.read_text()) == {"w": ["pexels:"]}

    def test_non_list_value_is_replaced(self, tmp_path):
        p = tmp_path / "r.json"; p.write_text(json.dumps({"w": "oops"}))
        assert addon._images._record_image_reject("w", "pexels:1", path=str(p)) is True
        assert json.loads(p.read_text()) == {"w": ["pexels:1"]}

    def test_legacy_pexels_tag_recorded(self, tmp_path):
        p = tmp_path / "r.json"
        tag = addon._images._image_source('<img src="old.jpg"><div>Photo by X on Pexels</div>')
        addon._images._record_image_reject("w", tag, path=str(p))
        assert json.loads(p.read_text()) == {"w": ["pexels:"]}

    def test_path_constant_matches_core(self):
        import core.image as img
        assert addon._config.IMAGE_REJECTS_PATH == img.REJECTS_PATH

    def test_write_failure_returns_false(self, tmp_path):
        p = tmp_path / "missing" / "r.json"
        assert addon._images._record_image_reject("w", "pexels:1", path=str(p)) is False
