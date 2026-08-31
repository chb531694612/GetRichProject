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

    def test_ticket_list_uses_thumbnail_and_zoom_loads_full_image(self):
        """The list may only request thumbnails; the full photo loads on zoom."""
        source = (ROOT / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
        self.assertIn("plan.ticket_thumb_url || plan.ticket_image_url", source)
        self.assertIn("openTicketZoom", source)
        # The lightbox tells the user the big photo is still downloading.
        self.assertIn('class="zoom-loading"', source)
        # The list thumbnail must not point straight at the full-size route.
        self.assertNotIn('class="ticket-thumb" :src="plan.ticket_image_url"', source)

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

    def test_hit_rate_chart_opens_from_summary_and_compares_markets(self):
        source = (ROOT / "frontend" / "src" / "App.vue").read_text(encoding="utf-8")
        styles = (ROOT / "frontend" / "src" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('aria-label="查看命中率趋势图"', source)
        self.assertIn("/api/v1/analytics", source)
        self.assertIn("7日滚动", source)
        self.assertIn("各玩法命中率", source)
        self.assertIn('class="hit-rate-chart"', source)
        self.assertIn(".analytics-modal", styles)


if __name__ == "__main__":
    unittest.main()
