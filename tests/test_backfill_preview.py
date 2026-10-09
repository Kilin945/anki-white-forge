"""⌘S 補完的卡：滑鼠停在那列浮出的預覽（大圖＋例句＋整句翻譯＋單字翻譯）。純函式，不碰 Qt。"""
import addon

preview = addon._text._preview_html


def test_shows_all_three_texts_and_image():
    h = preview("The Logger class is concrete.", "Logger 類別是具體的。", "具體",
                "/media dir/concrete_img_1.jpg")
    assert "The Logger class is concrete." in h
    assert "Logger 類別是具體的。" in h
    assert "具體" in h
    assert "<img" in h


def test_image_path_with_space_is_a_file_url():
    # Anki 的媒體資料夾在「Application Support」底下，路徑有空白
    h = preview("s", "c", "t", "/a b/c.jpg")
    assert 'src="file:///a%20b/c.jpg"' in h


def test_no_image_no_img_tag():
    assert "<img" not in preview("s", "c", "t", "")


def test_empty_fields_show_dash():
    h = preview("", "", "", "")
    assert h.count("—") == 3


def test_html_in_fields_is_stripped_and_escaped():
    # 欄位存的是 HTML：標籤拿掉、實體還原後再跳脫，例句裡的 < & 不會弄壞提示框
    h = preview("<b>a &lt; b</b> &amp; c", "x", "y", "")
    assert ">a &lt; b &amp; c</p>" in h
    assert "<b>" not in h


def test_image_filename_moved_to_text_layer():
    f = addon._text._image_filename
    assert f('<img src="dull_img_1.jpg" alt="x">') == "dull_img_1.jpg"
    assert f("") is None


def test_image_fits_box_keeping_ratio():
    fit = addon._text._fit_box
    box = addon._text.PREVIEW_IMAGE_BOX
    assert fit(400, 600) == (round(400 * box / 600), box)      # 直式：高頂到框
    assert fit(900, 600) == (box, round(600 * box / 900))      # 橫式：寬頂到框
    assert fit(0, 0) == (box, box)                              # 讀不到尺寸


def test_image_left_text_right():
    h = preview("s", "c", "t", "/a.jpg", (400, 600))
    assert h.index("<img") < h.index(">s<")                     # 圖在前（左欄），文字在後（右欄）
    assert 'height="260"' in h
    assert "font-size:18px" in h
