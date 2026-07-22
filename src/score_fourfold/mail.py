from __future__ import annotations

import hashlib
import html
import smtplib
from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from .config import Settings
from .database import Database, StoredPlan
from .domain import MarketType, PlanStatus, Recommendation, Settlement


STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;color:#1f2937;line-height:1.6}
.card{max-width:760px;margin:16px auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px}
h1{font-size:22px;margin:0 0 8px}.muted{color:#6b7280}.amount{font-size:24px;color:#b91c1c;font-weight:700}
table{width:100%;border-collapse:collapse;margin:16px 0}th,td{padding:10px 8px;border-bottom:1px solid #e5e7eb;text-align:left}
th{background:#f9fafb}.ok{color:#047857;font-weight:600}.bad{color:#b91c1c;font-weight:600}
.notice{background:#fffbeb;border-left:4px solid #f59e0b;padding:12px;margin:16px 0}.footer{font-size:12px;color:#6b7280;margin-top:20px}
"""


def _e(value) -> str:
    return html.escape(str(value), quote=True)


def _percent(value: Decimal) -> str:
    percent = value * 100
    return f"{percent:.2f}%" if percent >= 1 else f"{percent:.4f}%"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}元"


def _render_recommendation_v1(recommendation: Recommendation) -> tuple[str, str, str]:
    subject = f"[比分4串1] {recommendation.business_date} 2元基准推荐 {recommendation.plan_id}"
    rows_html: list[str] = []
    rows_text: list[str] = []
    for leg in recommendation.legs:
        match = leg.match
        snapshot_at = match.snapshot_fetched_at or match.odds_updated_at
        snapshot_label = snapshot_at.strftime("%m-%d %H:%M:%S") if snapshot_at else "未知"
        rows_html.append(
            "<tr>"
            f"<td>{_e(match.match_num)}</td><td>{_e(match.league)}</td>"
            f"<td>{_e(match.home)} vs {_e(match.away)}<br><span class='muted'>{_e(match.start_at.strftime('%m-%d %H:%M'))}</span></td>"
            f"<td><strong>{_e(leg.score.label)}</strong></td><td>{_e(leg.score.odds)}<br><span class='muted'>快照 {_e(snapshot_label)}</span></td>"
            f"<td>{_e(_percent(leg.score.probability))}</td>"
            "</tr>"
        )
        rows_text.append(
            f"{match.match_num} {match.league} {match.home} vs {match.away} "
            f"({match.start_at:%m-%d %H:%M}) → {leg.score.label}，SP {leg.score.odds}，"
            f"快照 {snapshot_label}，基线概率 {_percent(leg.score.probability)}"
        )
    notes_html = "".join(f"<li>{_e(note)}</li>" for note in recommendation.notes)
    expected_return = (recommendation.joint_probability * recommendation.net_prize).quantize(Decimal("0.01"))
    expected_profit = (expected_return - recommendation.stake).quantize(Decimal("0.01"))
    break_even_probability = recommendation.stake / recommendation.net_prize
    tax_line = (
        f"<p>预计单张税额：{_money(recommendation.tax)}；预计税后返还：<strong>{_money(recommendation.net_prize)}</strong></p>"
        if recommendation.tax > 0
        else "<p>按本张2元基准票估算，理论奖金未触发1万元税收门槛。</p>"
    )
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>竞彩足球比分4串1</h1>
<div class="muted">计划编号：{_e(recommendation.plan_id)} · 生成时间：{_e(recommendation.created_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
<table><thead><tr><th>编号</th><th>联赛</th><th>对阵</th><th>推荐比分</th><th>固定奖金</th><th>基线概率</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<p>投注方式：<strong>比分4串1，单式1注</strong>　基准投入：<strong>2.00元</strong></p>
<p>综合固定奖金：{_e(recommendation.combined_odds)}</p>
<p>四场全中联合基线概率：{_e(_percent(recommendation.joint_probability))}</p>
<p>四场全部命中时的理论税前返还：</p><div class="amount">{_money(recommendation.gross_prize)}</div>
{tax_line}
<p>按市场基线概率估算的返还：{_money(expected_return)}；基线期望收益：{_money(expected_profit)}。这不是盈利预测。</p>
<p>税后盈亏平衡命中率：{_e(_percent(break_even_probability))}；本张最大损失：2.00元。</p>
<div class="notice">按你的约定，本系统把每封计划记作已购买2元基准票。全部金额均为模拟账本值；实际出票固定奖金可能变化，最终兑奖以实体票为准。同一期其他彩票可能改变实际计税结果。</div>
<ul>{notes_html}</ul>
<div class="footer">仅限成年人理性购彩。本邮件是个人数据分析记录，不构成保证收益或线上彩票销售。</div>
</div></body></html>"""
    text_body = "\n".join(
        [
            "竞彩足球比分4串1（2元基准）",
            f"计划编号：{recommendation.plan_id}",
            f"生成时间：{recommendation.created_at:%Y-%m-%d %H:%M:%S %Z}",
            "",
            *rows_text,
            "",
            "投注方式：比分4串1，单式1注；基准投入：2.00元",
            f"综合固定奖金：{recommendation.combined_odds}",
            f"联合基线概率：{_percent(recommendation.joint_probability)}",
            f"四场全部命中时的理论税前返还：{_money(recommendation.gross_prize)}",
            f"预计单张税额：{_money(recommendation.tax)}",
            f"预计税后返还：{_money(recommendation.net_prize)}",
            f"市场基线估算返还：{_money(expected_return)}",
            f"市场基线期望收益：{_money(expected_profit)}（不是盈利预测）",
            f"税后盈亏平衡命中率：{_percent(break_even_probability)}；最大损失：2.00元",
            "",
            "按约定记作已购买2元；金额是模拟账本值，最终兑奖以实体票为准。",
            "仅限成年人理性购彩，不保证收益。",
        ]
    )
    return subject, text_body, html_body


def render_recommendation(recommendation: Recommendation) -> tuple[str, str, str]:
    pass_size = recommendation.pass_size
    pass_label = f"{pass_size}串1"
    market_label = recommendation.market.label_zh
    pick_header = "推荐比分" if recommendation.market is MarketType.CRS else "推荐结果"
    rows_html: list[str] = []
    rows_text: list[str] = []
    for leg in recommendation.legs:
        match = leg.match
        snapshot_at = match.snapshot_fetched_at or match.odds_updated_at
        snapshot_label = snapshot_at.strftime("%Y-%m-%d %H:%M:%S") if snapshot_at else "未知"
        start_label = match.start_at.strftime("%Y-%m-%d %H:%M")
        rows_html.append(
            "<tr>"
            f"<td>{_e(match.match_num)}</td><td>{_e(match.league)}</td>"
            f"<td>{_e(match.home)} vs {_e(match.away)}<br><span class='muted'>{_e(start_label)}</span></td>"
            f"<td><strong>{_e(leg.score.label)}</strong></td><td>{_e(leg.score.odds)}</td>"
            f"<td>{_e(_percent(leg.score.probability))}</td>"
            "</tr>"
        )
        rows_text.append(
            f"{match.match_num} {match.league} {match.home} vs {match.away} "
            f"({start_label}) → {leg.score.label}；SP {leg.score.odds}；"
            f"快照 {snapshot_label}；基线概率 {_percent(leg.score.probability)}"
        )
    expected_return = (recommendation.joint_probability * recommendation.net_prize).quantize(Decimal("0.01"))
    expected_profit = (expected_return - recommendation.stake).quantize(Decimal("0.01"))
    break_even_probability = recommendation.stake / recommendation.net_prize
    earliest_start = min(leg.match.start_at for leg in recommendation.legs)
    business_dates = {leg.match.business_date for leg in recommendation.legs}
    cross_day_notice = ""
    if len(business_dates) > 1:
        cross_day_notice = (
            "<div class='notice'>本票跨两个比赛编号日期。必须在最早一场停止销售前一次性完成购买；"
            "程序没有官方停售时间字段，请以实体终端实际可售状态和出票固定奖金为准。</div>"
        )
    notes_html = "".join(f"<li>{_e(note)}</li>" for note in recommendation.notes)
    subject = (
        f"[{market_label}{pass_label}] {recommendation.recommendation_date} "
        f"2元基准推荐 {recommendation.plan_id}"
    )
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>竞彩足球{_e(market_label)}{_e(pass_label)}</h1>
<div class="muted">计划编号：{_e(recommendation.plan_id)} · 推荐日：{_e(recommendation.recommendation_date)} · 最晚比赛编号日期：{_e(recommendation.issue_date)}</div>
<table><thead><tr><th>编号</th><th>联赛</th><th>对阵/开赛时间</th><th>{_e(pick_header)}</th><th>固定奖金</th><th>基线概率</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<p>投注方式：<strong>{_e(market_label)}{_e(pass_label)}，单式1注</strong>；最低投入：<strong>2.00元</strong></p>
<p>最早一场开赛时间：{_e(earliest_start.strftime('%Y-%m-%d %H:%M'))}；综合固定奖金：{_e(recommendation.combined_odds)}</p>
<p>{pass_size}场全中联合基线概率：{_e(_percent(recommendation.joint_probability))}</p>
<p>全部命中时理论税前返还：</p><div class="amount">{_money(recommendation.gross_prize)}</div>
<p>单张模拟税额：{_money(recommendation.tax)}；模拟税后返还：<strong>{_money(recommendation.net_prize)}</strong></p>
<p class="muted">税额这里只按本张2元票估算；同一期、同一游戏的其他中奖票可能改变实际计税结果。</p>
<p>按市场基线估算返还：{_money(expected_return)}；基线期望收益：{_money(expected_profit)}。这不是盈利预测。</p>
<p>税后盈亏平衡命中率：{_e(_percent(break_even_probability))}；本张最大损失：2.00元。</p>
{cross_day_notice}
<div class="notice">按你的约定，只有本邮件在截止前成功发送后，模拟账本才把本计划记作已购买2元。实际固定奖金及能否出票以实体终端为准。</div>
<ul>{notes_html}</ul>
<div class="footer">仅限成年人理性购彩，不保证收益；90分钟赛果和兑奖均以官方结果及实体票为准。</div>
</div></body></html>"""
    text_body = "\n".join(
        [
            f"竞彩足球{market_label}{pass_label}（2元基准）",
            f"计划编号：{recommendation.plan_id}",
            f"推荐日：{recommendation.recommendation_date}；最晚比赛编号日期：{recommendation.issue_date}",
            *rows_text,
            "",
            f"投注方式：{market_label}{pass_label}，单式1注；最低投入：2.00元",
            f"综合固定奖金：{recommendation.combined_odds}",
            f"{pass_size}场全中联合基线概率：{_percent(recommendation.joint_probability)}",
            f"全部命中理论税前返还：{_money(recommendation.gross_prize)}",
            f"单张模拟税额：{_money(recommendation.tax)}；模拟税后返还：{_money(recommendation.net_prize)}",
            "税额仅按本张2元票估算；同一期同一游戏的其他中奖票可能改变实际税额。",
            f"市场基线估算返还：{_money(expected_return)}；基线期望收益：{_money(expected_profit)}（不是盈利预测）",
            f"税后盈亏平衡命中率：{_percent(break_even_probability)}；最大损失：2.00元",
            "跨日计划需在最早一场停售前一次性购买，并以实体终端实际可售状态和出票固定奖金为准。"
            if len(business_dates) > 1
            else "请在最早一场停售前购买，并以实体终端出票固定奖金为准。",
            "仅限成年人理性购彩，不保证收益。",
        ]
    )
    return subject, text_body, html_body


def _render_no_recommendation_v1(day: date, reason: str, now: datetime) -> tuple[str, str, str]:
    subject = f"[比分4串1] {day.isoformat()} 本期无推荐"
    text_body = f"{day.isoformat()} 本期无推荐。\n原因：{reason}\n检查时间：{now:%Y-%m-%d %H:%M:%S %Z}\n"
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>本期无推荐</h1><p>{_e(day.isoformat())}</p>
<div class="notice">{_e(reason)}</div><p class="muted">检查时间：{_e(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>
<div class="footer">没有符合阈值的组合时不强行凑单。</div></div></body></html>"""
    return subject, text_body, html_body


def _render_error_v1(job_name: str, error: str, now: datetime) -> tuple[str, str, str]:
    subject = f"[比分4串1运行异常] {job_name} {now:%Y-%m-%d %H:%M}"
    text_body = f"任务：{job_name}\n时间：{now:%Y-%m-%d %H:%M:%S %Z}\n错误：{error}\n系统已停止本轮生成，不会使用缺失或伪造数据。\n"
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>运行异常</h1><p>任务：{_e(job_name)}</p>
<div class="notice">{_e(error)}</div><p class="muted">{_e(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>
<div class="footer">本轮已安全停止，不会使用缺失或伪造数据生成推荐。</div></div></body></html>"""
    return subject, text_body, html_body


def _render_mail_test_v1(now: datetime) -> tuple[str, str, str]:
    subject = f"[比分4串1] 邮件配置测试 {now:%Y-%m-%d %H:%M}"
    text_body = (
        f"邮件发送测试成功。\n时间：{now:%Y-%m-%d %H:%M:%S %Z}\n"
        "这不是推荐计划，不计入2元模拟账本。\n"
    )
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>邮件配置测试成功</h1>
<p>服务器已经可以发送比分4串1通知邮件。</p>
<p class="muted">{_e(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>
<div class="footer">这不是推荐计划，不计入2元模拟账本。</div></div></body></html>"""
    return subject, text_body, html_body


def _render_settlement_v1(plan: StoredPlan, settlement: Settlement, summary: dict[str, int | str]) -> tuple[str, str, str]:
    status_label = {
        PlanStatus.WON: "中奖",
        PlanStatus.LOST: "未中奖",
        PlanStatus.VOID: "全部无效/退款",
        PlanStatus.PENDING: "待结算",
    }[settlement.status]
    void_count = sum(result.status.value == "void" for result in settlement.leg_results)
    active_count = len(settlement.leg_results) - void_count
    if void_count and settlement.status is PlanStatus.WON:
        status_label = f"中奖（{void_count}场无效，按{active_count}串1重算）"
    subject = f"[比分4串1赛果] {status_label} {plan.plan_id}"
    result_map = {result.match_id: result for result in settlement.leg_results}
    rows_html: list[str] = []
    rows_text: list[str] = []
    for leg in plan.legs:
        result = result_map[leg.match_id]
        actual = result.score_label or ("无效场次" if result.status.value == "void" else "待定")
        hit = result.status.value == "void" or actual == leg.score_label
        verdict = "无效" if result.status.value == "void" else ("命中" if hit else "未中")
        css = "ok" if hit else "bad"
        rows_html.append(
            f"<tr><td>{_e(leg.match_num)}</td><td>{_e(leg.home)} vs {_e(leg.away)}</td>"
            f"<td>{_e(leg.score_label)}</td><td>{_e(actual)}</td><td class='{css}'>{_e(verdict)}</td></tr>"
        )
        rows_text.append(f"{leg.match_num} {leg.home} vs {leg.away}：推荐 {leg.score_label}，赛果 {actual}，{verdict}")
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>比分4串1赛果：{_e(status_label)}</h1>
<div class="muted">计划编号：{_e(plan.plan_id)} · 结算时间：{_e(settlement.settled_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
<table><thead><tr><th>编号</th><th>对阵</th><th>推荐</th><th>90分钟赛果</th><th>结果</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<p>2元基准税前返还：<strong>{_money(settlement.gross_prize)}</strong></p>
<p>预计单张税额：{_money(settlement.tax)}　税后返还：{_money(settlement.net_prize)}</p>
<div class="amount">本期2元基准模拟净收益：{_money(settlement.net_profit)}</div>
<p>累计计划：{_e(summary.get('plans_total', 0))}；累计模拟投入：{_e(summary.get('baseline_stake', '0.00'))}元；累计已结算模拟净收益：{_e(summary.get('baseline_profit', '0.00'))}元</p>
<div class="footer">税额按本张基准票单独估算，不含同期开奖的其他彩票；赛果按全场90分钟（含伤停补时）记录，实际兑奖以官方开奖结果和实体票为准。</div>
</div></body></html>"""
    text_body = "\n".join(
        [
            f"比分4串1赛果：{status_label}",
            f"计划编号：{plan.plan_id}",
            *rows_text,
            f"2元基准税前返还：{_money(settlement.gross_prize)}",
            f"预计单张税额：{_money(settlement.tax)}",
            f"税后返还：{_money(settlement.net_prize)}",
            f"本期2元基准模拟净收益：{_money(settlement.net_profit)}",
            f"累计计划：{summary.get('plans_total', 0)}",
            f"累计模拟投入：{summary.get('baseline_stake', '0.00')}元",
            f"累计已结算模拟净收益：{summary.get('baseline_profit', '0.00')}元",
            "税额仅按本张模拟票估算，不含同期开奖的其他彩票。",
        ]
    )
    return subject, text_body, html_body


def render_no_recommendation(day: date, reason: str, now: datetime) -> tuple[str, str, str]:
    subject = f"[比分串关] {day.isoformat()} 今日无有效购买推荐"
    text_body = (
        f"{day.isoformat()} 今日无有效购买推荐，请勿按本系统购票。\n"
        f"原因：{reason}\n确认时间：{now:%Y-%m-%d %H:%M:%S %Z}\n"
    )
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>今日无有效购买推荐</h1><p>{_e(day.isoformat())}</p>
<div class="notice">{_e(reason)}</div><p>今天请勿按本系统购票。</p>
<p class="muted">确认时间：{_e(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>
<div class="footer">没有合格组合或推荐未能在截止前送达时，不强行凑单。</div></div></body></html>"""
    return subject, text_body, html_body


def render_error(job_name: str, error: str, now: datetime) -> tuple[str, str, str]:
    subject = f"[比分串关运行异常] {job_name} {now:%Y-%m-%d %H:%M}"
    text_body = (
        f"任务：{job_name}\n时间：{now:%Y-%m-%d %H:%M:%S %Z}\n错误：{error}\n"
        "本轮已安全停止，不会使用缺失或伪造数据生成推荐。\n"
    )
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>运行异常</h1><p>任务：{_e(job_name)}</p>
<div class="notice">{_e(error)}</div><p class="muted">{_e(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>
<div class="footer">本轮已安全停止，不会使用缺失或伪造数据生成推荐。</div></div></body></html>"""
    return subject, text_body, html_body


def render_mail_test(now: datetime) -> tuple[str, str, str]:
    subject = f"[比分串关] 邮件配置测试 {now:%Y-%m-%d %H:%M}"
    text_body = f"邮件发送测试成功。\n时间：{now:%Y-%m-%d %H:%M:%S %Z}\n这不是购买推荐。\n"
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>邮件配置测试成功</h1><p>服务器可以发送比分串关通知邮件。</p>
<p class="muted">{_e(now.strftime('%Y-%m-%d %H:%M:%S %Z'))}</p>
<div class="footer">这不是购买推荐，不计入2元模拟账本。</div></div></body></html>"""
    return subject, text_body, html_body


def render_settlement(
    plan: StoredPlan,
    settlement: Settlement,
    summary: dict[str, int | str],
) -> tuple[str, str, str]:
    pass_label = f"{plan.pass_size}串1"
    market_label = plan.market.label_zh
    base_status = {
        PlanStatus.WON: "中奖",
        PlanStatus.LOST: "未中奖",
        PlanStatus.VOID: "全部无效/退款",
        PlanStatus.PENDING: "待结算",
    }[settlement.status]
    result_map = {result.match_id: result for result in settlement.leg_results}
    void_count = sum(result.status.value == "void" for result in settlement.leg_results)
    active_count = len(settlement.leg_results) - void_count
    status_label = base_status
    if void_count and settlement.status is PlanStatus.WON:
        status_label = f"中奖（原{pass_label}，{void_count}场无效，按{active_count}串1重算）"
    rows_html: list[str] = []
    rows_text: list[str] = []
    for leg in plan.legs:
        result = result_map[leg.match_id]
        actual = result.score_label or ("无效场次" if result.status.value == "void" else "待定")
        if result.status.value == "void":
            hit = True
        elif plan.market is MarketType.HAD:
            hit = result.had_label == leg.score_label
            if result.had_label:
                actual = f"{actual}（{result.had_label}）"
        else:
            hit = actual == leg.score_label
        verdict = "无效" if result.status.value == "void" else ("命中" if hit else "未中")
        css = "ok" if hit else "bad"
        rows_html.append(
            f"<tr><td>{_e(leg.match_num)}</td><td>{_e(leg.start_at.strftime('%Y-%m-%d %H:%M'))}</td>"
            f"<td>{_e(leg.home)} vs {_e(leg.away)}</td><td>{_e(leg.score_label)}</td>"
            f"<td>{_e(actual)}</td><td class='{css}'>{_e(verdict)}</td></tr>"
        )
        rows_text.append(
            f"{leg.match_num} ({leg.start_at:%Y-%m-%d %H:%M}) {leg.home} vs {leg.away}："
            f"推荐{leg.score_label}，赛果{actual}，{verdict}"
        )
    subject = f"[{market_label}{pass_label}赛果] {status_label} {plan.plan_id}"
    html_body = f"""<!doctype html><html><head><meta charset="utf-8"><style>{STYLE}</style></head>
<body><div class="card"><h1>{_e(market_label)}{_e(pass_label)}赛果：{_e(status_label)}</h1>
<div class="muted">计划编号：{_e(plan.plan_id)} · 推荐日：{_e(plan.recommendation_date)} · 结算时间：{_e(settlement.settled_at.strftime('%Y-%m-%d %H:%M:%S %Z'))}</div>
<table><thead><tr><th>编号</th><th>开赛时间</th><th>对阵</th><th>推荐</th><th>90分钟赛果</th><th>结果</th></tr></thead>
<tbody>{''.join(rows_html)}</tbody></table>
<p>2元基准税前返还：<strong>{_money(settlement.gross_prize)}</strong></p>
<p>单张模拟税额：{_money(settlement.tax)}；模拟税后返还：{_money(settlement.net_prize)}</p>
<p class="muted">税额仅按本张票估算；同一期、同一游戏的其他中奖票可能改变实际计税结果。</p>
<div class="amount">本期2元基准模拟净收益：{_money(settlement.net_profit)}</div>
<p>累计已送达计划：{_e(summary.get('plans_total', 0))}；累计模拟投入：{_e(summary.get('baseline_stake', '0.00'))}元；累计已结算模拟净收益：{_e(summary.get('baseline_profit', '0.00'))}元</p>
<div class="footer">无效场按原购票固定奖金降串重算；最终兑奖以官方结果和实体票为准。</div>
</div></body></html>"""
    text_body = "\n".join(
        [
            f"{market_label}{pass_label}赛果：{status_label}",
            f"计划编号：{plan.plan_id}；推荐日：{plan.recommendation_date}",
            *rows_text,
            f"2元基准税前返还：{_money(settlement.gross_prize)}",
            f"单张模拟税额：{_money(settlement.tax)}；模拟税后返还：{_money(settlement.net_prize)}",
            "税额仅按本张票估算；同一期同一游戏的其他中奖票可能改变实际税额。",
            f"本期2元基准模拟净收益：{_money(settlement.net_profit)}",
            f"累计已送达计划：{summary.get('plans_total', 0)}；累计模拟投入：{summary.get('baseline_stake', '0.00')}元；累计模拟净收益：{summary.get('baseline_profit', '0.00')}元",
        ]
    )
    return subject, text_body, html_body


class MailExpiredError(RuntimeError):
    pass


class Mailer:
    def __init__(self, settings: Settings, clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(settings.timezone))

    def now(self) -> datetime:
        return self._clock().astimezone(self.settings.timezone)

    def _ensure_not_expired(self, expires_at: datetime | None) -> None:
        if expires_at is not None and self.now() >= expires_at:
            raise MailExpiredError("recommendation mail cutoff has passed")

    def send(
        self,
        *,
        email_id: int,
        dedupe_key: str,
        subject: str,
        text_body: str,
        html_body: str,
        expires_at: datetime | None = None,
    ) -> str:
        self._ensure_not_expired(expires_at)
        message_digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:32]
        message_id = f"<score-fourfold-{message_digest}@localhost>"
        if self.settings.mail_dry_run:
            preview_dir = Path(self.settings.mail_preview_dir)
            preview_dir.mkdir(parents=True, exist_ok=True)
            safe_id = f"{email_id:06d}"
            (preview_dir / f"{safe_id}.txt").write_text(
                f"Message-ID: {message_id}\nSubject: {subject}\nTo: {self.settings.mail_to}\n\n{text_body}",
                encoding="utf-8",
            )
            (preview_dir / f"{safe_id}.html").write_text(html_body, encoding="utf-8")
            return "previewed"
        errors = self.settings.validate_mail()
        if errors:
            raise RuntimeError("; ".join(errors))
        message = EmailMessage()
        sender = self.settings.mail_from or self.settings.smtp_username
        message["From"] = sender
        message["To"] = self.settings.mail_to
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        with smtplib.SMTP_SSL(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.settings.http_timeout_seconds,
        ) as smtp:
            smtp.login(self.settings.smtp_username, self.settings.smtp_auth_code)
            # SMTP setup can take long enough to cross the cutoff.
            self._ensure_not_expired(expires_at)
            smtp.send_message(message)
        return "sent"


def flush_outbox(database: Database, mailer: Mailer, now: datetime) -> tuple[int, int]:
    sent = 0
    failed = 0
    for _ in range(20):
        rows = database.claim_due_emails(mailer.now(), limit=1)
        if not rows:
            break
        row = rows[0]
        claim_token = row["claim_token"]
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        try:
            delivery_result = mailer.send(
                email_id=int(row["id"]),
                dedupe_key=row["dedupe_key"],
                subject=row["subject"],
                text_body=row["text_body"],
                html_body=row["html_body"],
                expires_at=expires_at,
            )
        except MailExpiredError as exc:
            database.mark_email_expired(
                int(row["id"]),
                claim_token,
                mailer.now(),
                f"{type(exc).__name__}: {exc}",
            )
        except Exception as exc:  # SMTP failures must remain queued and observable.
            failure_now = mailer.now()
            database.mark_email_failed(
                int(row["id"]),
                claim_token,
                f"{type(exc).__name__}: {exc}",
                failure_now,
            )
            failed += 1
        else:
            if delivery_result == "previewed":
                database.mark_email_previewed(int(row["id"]), claim_token, mailer.now())
            else:
                database.mark_email_sent(int(row["id"]), claim_token, mailer.now())
            sent += 1
    return sent, failed
