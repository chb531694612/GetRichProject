from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from score_fourfold.provider import (
    EXPECTED_CRS_CODES,
    OkoooProvider,
    SportteryProvider,
    parse_normalized_matches,
    parse_results,
    parse_sporttery_matches,
)

# Okooo uses server-rendered HTML whose attribute order is not stable.
from score_fourfold.provider import (
    _OKOOO_CRS_LABEL_MAP,
    _okooo_crs_options_from_html,
    _okooo_parse_matches_from_html,
)

from .helpers import make_settings


class ProviderParsingTests(unittest.TestCase):
    def test_okooo_html_accepts_reordered_attributes_and_single_quotes(self):
        page = """
        <div data-hname='主队' id='match_123' data-aname='客队'
             class='extra touzhu_1' data-ordercn='周一001'>
          <a class="saiming">测试联赛</a><i title="比赛时间:2026-07-20 20:00:00"></i>
        </div>
        """
        parsed = _okooo_parse_matches_from_html(page, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(parsed["123"]["home"], "主队")
        self.assertEqual(parsed["123"]["match_num"], "周一001")

        odds = "<li data-sp='2.50' class='active ping'><span class='peilv'>1-0</span></li>"
        options = _okooo_crs_options_from_html(odds)
        self.assertEqual([(item.code, item.label) for item in options], [("s01s00", "1:0")])

    def test_okooo_current_table_row_parses_match_and_inline_had(self):
        page = """
        <tr class="alltrObj" id="tr7201" isover="1">
          <td><span class="xh"><i>201</i></span><a class="ls">K联赛</a></td>
          <td title="比赛时间：2026-07-21 20:00:00"></td>
          <td><a class="duinameh" href="/soccer/match/1320358/trends/">主队</a>
              <a class="duinameh" href="/soccer/match/1320358/trends/">客队</a></td>
          <td><div class="frqBetObj"><a class="betObj">2.10</a>
              <a class="betObj">3.20</a><a class="betObj">3.40</a></div></td>
        </tr>
        """
        parsed = _okooo_parse_matches_from_html(page, ZoneInfo("Asia/Shanghai"))["1320358"]
        self.assertEqual((parsed["match_num"], parsed["league"]), ("201", "K联赛"))
        self.assertEqual(parsed["match_order"], "7201")
        self.assertEqual([item.code for item in parsed["had_options"]], ["h", "d", "a"])
        self.assertTrue(parsed["selling"])
        self.assertIsNone(parsed["result_score"])

    def test_okooo_expanded_score_panel_parses_all_31_options(self):
        panel = "".join(
            f"<a class='mixselect ping' data-sp='{index + 2}.50'>"
            f"<span class='peilv'>{label}</span></a>"
            for index, label in enumerate(_OKOOO_CRS_LABEL_MAP)
        )
        options = _okooo_crs_options_from_html(panel)

        self.assertEqual(len(options), 31)
        self.assertEqual({option.code for option in options}, EXPECTED_CRS_CODES)

    def test_okooo_current_finished_row_parses_result(self):
        page = """
        <tr class="alltrObj" id="tr7201" isover="0">
          <td><span class="xh"><i>201</i></span><a class="ls">K联赛</a></td>
          <td title="比赛时间：2026-07-19 18:30:00"></td>
          <td><a class="duinameh" href="/soccer/match/1320358/trends/">主队</a>
              <b class="bftext font_red">1-3</b>
              <a class="duinameh" href="/soccer/match/1320358/trends/">客队</a></td>
        </tr>
        """
        parsed = _okooo_parse_matches_from_html(page, ZoneInfo("Asia/Shanghai"))["1320358"]
        self.assertFalse(parsed["selling"])
        self.assertEqual(parsed["result_score"], (1, 3))

    def test_okooo_provider_uses_inline_had_and_current_result_markup(self):
        page = """
        <tr class="alltrObj" id="tr1201" isover="1"><td><span class="xh"><i>201</i></span>
        <a class="ls">芬超</a></td><td title="比赛时间：2026-07-21 20:00:00"></td>
        <td><a class="duinameh" href="/soccer/match/1001/trends/">主队</a>
        <span class="bftext">VS</span><a class="duinameh" href="/soccer/match/1001/trends/">客队</a></td>
        <td><div class="frqBetObj"><a class="betObj">2.10</a><a class="betObj">3.20</a>
        <a class="betObj">3.40</a></div></td></tr>
        <tr class="alltrObj" isover="0"><td><span class="xh"><i>101</i></span>
        <a class="ls">芬超</a></td><td title="比赛时间：2026-07-20 10:00:00"></td>
        <td><a class="duinameh" href="/soccer/match/1000/trends/">甲队</a>
        <b class="bftext">2-1</b><a class="duinameh" href="/soccer/match/1000/trends/">乙队</a></td></tr>
        """
        provider = OkoooProvider(make_settings(Path("data"), data_provider="okooo"))
        requested_urls = []

        def response(url, *, accept_html=False):
            requested_urls.append(url)
            if "action=more" in url:
                return ""
            if "/ajax/" in url:
                return "{}"
            return page

        with patch.object(provider, "_get", side_effect=response):
            matches = provider.get_matches()
            results = provider.get_results(datetime(2026, 7, 20).date(), datetime(2026, 7, 20).date())
        self.assertEqual([match.match_id for match in matches], ["1001"])
        self.assertEqual([option.code for option in matches[0].had_options], ["h", "d", "a"])
        self.assertEqual(results["1000"].score_label, "2:1")
        expand_urls = [url for url in requested_urls if "action=more" in url]
        self.assertEqual(len(expand_urls), 1)
        self.assertIn("LotteryNo=2026-07-21", expand_urls[0])
        self.assertIn("MatchOrder=1201", expand_urls[0])

    def _complete_official_payload(self):
        crs = {code: "10.00" for code in EXPECTED_CRS_CODES}
        crs["updateTime"] = "2026-07-14 12:00:00"
        return {
            "success": True,
            "errorCode": "0",
            "value": {
                "allUpList": {"CRS": [{"poolCode": "CRS", "formula": "4x1"}]},
                "matchInfoList": [
                    {
                        "businessDate": "2026-07-14",
                        "subMatchList": [
                            {
                                "matchId": 123,
                                "matchNumStr": "周二001",
                                "matchDate": "2026-07-14",
                                "matchTime": "20:00:00",
                                "leagueAbbName": "测试联赛",
                                "homeTeamAbbName": "主队",
                                "awayTeamAbbName": "客队",
                                "matchStatus": "Selling",
                                "sellStatus": 1,
                                "poolList": [
                                    {
                                        "poolCode": "CRS",
                                        "poolStatus": "Selling",
                                        "bettingAllup": 1,
                                    }
                                ],
                                "crs": crs,
                            }
                        ],
                    }
                ],
            },
        }

    def test_parses_official_odds_shape_and_score_codes(self):
        payload = {
            "value": {
                "lastUpdateTime": "2026-07-14 12:00:00",
                "matchInfoList": [
                    {
                        "businessDate": "2026-07-14",
                        "matchNumDate": "260714",
                        "subMatchList": [
                            {
                                "matchId": 123,
                                "matchNumStr": "周二001",
                                "matchDate": "2026-07-14",
                                "matchTime": "20:00:00",
                                "leagueAbbName": "测试联赛",
                                "homeTeamAbbName": "主队",
                                "awayTeamAbbName": "客队",
                                "matchStatus": "Selling",
                                "sellStatus": 1,
                                "poolList": [
                                    {
                                        "poolCode": "CRS",
                                        "poolStatus": "Selling",
                                        "bettingAllup": 1,
                                    }
                                ],
                                "crs": {
                                    "s01s00": "6.50",
                                    "s01s01": "7.00",
                                    "s00s01": "8.20",
                                    "s1sh": "25.00",
                                    "s1sd": "60.00",
                                    "s1sa": "30.00",
                                    "updateTime": "2026-07-14 12:00:00",
                                },
                            }
                        ],
                    }
                ],
            }
        }
        matches = parse_sporttery_matches(payload, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].match_id, "123")
        labels = {option.label for option in matches[0].score_options}
        self.assertEqual(labels, {"1:0", "1:1", "0:1", "胜其它", "平其它", "负其它"})
        probability_sum = sum(option.probability for option in matches[0].score_options)
        self.assertAlmostEqual(float(probability_sum), 1.0, places=10)
        self.assertEqual(matches[0].odds_updated_at, datetime(2026, 7, 14, 12, tzinfo=ZoneInfo("Asia/Shanghai")))

    def test_parses_supported_three_and_four_by_one_formulas(self):
        payload = self._complete_official_payload()
        payload["value"]["allUpList"]["CRS"] = [
            {"poolCode": "CRS", "formula": "3×1"},
            {"poolCode": "CRS", "formula": "4x1"},
        ]
        matches = parse_sporttery_matches(payload, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(matches[0].supported_pass_sizes, frozenset({3, 4}))

    def test_parses_multiple_result_shapes(self):
        payload = {
            "value": {
                "matchResultList": [
                    {
                        "matchId": 1,
                        "sectionsNo999": "2:1",
                        "matchResultStatus": "2",
                        "poolStatus": "Payout",
                    },
                    {"matchId": 2, "homeScore": 0, "awayScore": 0, "status": "finished"},
                    {"matchId": 3, "status": "取消"},
                    {"matchId": 4, "status": "scheduled"},
                ]
            }
        }
        results = parse_results(payload)
        self.assertEqual(results["1"].score_label, "2:1")
        self.assertEqual(results["2"].score_label, "0:0")
        self.assertEqual(results["3"].status.value, "void")
        self.assertEqual(results["4"].status.value, "pending")

    def test_does_not_settle_uniform_live_score_before_payout(self):
        payload = {
            "value": {
                "matchResult": [
                    {
                        "matchId": 10,
                        "sectionsNo999": "1:0",
                        "matchResultStatus": "1",
                        "poolStatus": "Selling",
                    },
                    {
                        "matchId": 11,
                        "sectionsNo999": "无效场次",
                        "matchResultStatus": "2",
                        "poolStatus": "Payout",
                    },
                    {
                        "matchId": 12,
                        "sectionsNo999": "取消",
                        "matchResultStatus": "1",
                        "poolStatus": "Suspended",
                    },
                ]
            }
        }
        results = parse_results(payload)
        self.assertEqual(results["10"].status.value, "pending")
        self.assertEqual(results["11"].status.value, "void")
        self.assertEqual(results["12"].status.value, "pending")

    def test_normalizes_score_label_from_code(self):
        payload = {
            "matches": [
                {
                    "match_id": "normal-1",
                    "match_num": "周二001",
                    "business_date": "2026-07-14",
                    "league": "测试联赛",
                    "home": "主队",
                    "away": "客队",
                    "start_at": "2026-07-14T20:00:00+08:00",
                    "markets": {
                        "crs": {
                            "updateTime": "2026-07-14 12:00:00",
                            "outcomes": [
                                {"code": "s01s00", "label": "1-0", "odds": 6.5, "probability": 0.1}
                            ],
                        }
                    },
                }
            ]
        }
        matches = parse_normalized_matches(payload, ZoneInfo("Asia/Shanghai"))
        self.assertEqual(matches[0].score_options[0].label, "1:0")

    def test_official_provider_requires_complete_selling_market(self):
        provider = SportteryProvider(make_settings(Path("data"), data_provider="sporttery"))
        payload = self._complete_official_payload()
        with patch.object(provider, "_get_json", return_value=payload):
            self.assertEqual(len(provider.get_matches()), 1)

        payload["value"]["matchInfoList"][0]["subMatchList"][0]["sellStatus"] = 2
        with patch.object(provider, "_get_json", return_value=payload):
            self.assertEqual(provider.get_matches(), [])


if __name__ == "__main__":
    unittest.main()
