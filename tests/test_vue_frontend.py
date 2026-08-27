from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VueFrontendSourceTests(unittest.TestCase):
    def test_ticket_upload_supports_mobile_selection_drag_and_paste(self):
        source = (ROOT / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertIn('accept="image/*"', source)
        self.assertIn('@drop.prevent="dropTicket($event, plan)"', source)
        self.assertIn("handlePaste", source)
        self.assertIn("手机可选相册或拍照", source)

    def test_recommendation_screenshot_copies_or_downloads_png(self):
        source = (ROOT / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertIn("async function screenshotPlan", source)
        self.assertIn("navigator.clipboard.write", source)
        self.assertIn("link.download", source)
        self.assertIn("一键截图推荐", source)
        self.assertNotIn("请与体彩店出票内容逐场核对", source)

    def test_mobile_breakpoints_are_present(self):
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:820px)", styles)
        self.assertIn("@media(max-width:520px)", styles)


if __name__ == "__main__":
    unittest.main()
