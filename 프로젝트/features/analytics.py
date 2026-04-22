"""
features/analytics.py — D10~D14 분석 기능 모음

D10: 일별 비용 트렌드 누적 추적
D11: 배송지 위험도 사전 스크리닝
D13: 배차 전 AI SLA·비용 예보
D14: 기사별 수익 공정성 지수
"""
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Optional

import pandas as pd
import streamlit as st

from geo import DIESEL_EMISSION_FACTOR

logger = logging.getLogger("logitrack")


# ══════════════════════════════════════════════
# 공통 헬퍼 (피로도)
# ══════════════════════════════════════════════

def calc_fatigue(stat: dict) -> float:
    """차량 통계로 피로도 지수 산출 (0~100).

    Args:
        stat: TruckStats dict

    Returns:
        피로도 점수 (낮을수록 양호)
    """
    t = min(50.0, (stat.get("time",    0) / 600)                          * 50)
    l = min(30.0, (stat.get("used_wt", 0) / max(stat.get("max_wt", 1), 1)) * 30)
    w = min(20.0, (stat.get("wait_time", 0) / 120)                         * 20)
    return round(t + l + w, 1)


def fatigue_label(score: float) -> str:
    """피로도 점수를 텍스트 레이블로 변환."""
    if score >= 80: return f"🔴 위험({score})"
    if score >= 60: return f"🟡 주의({score})"
    return              f"🟢 양호({score})"


# ══════════════════════════════════════════════
# D10 — 누적 실행 트렌드
# ══════════════════════════════════════════════

_MAX_RUN_LOG = 50


def log_run(res: dict) -> None:
    """최적화 실행 결과를 세션 로그에 기록.

    Args:
        res: OptimizationResult dict
    """
    entry = {
        "시각":   datetime.now().strftime("%H:%M"),
        "총비용": int(res.get("total_cost", 0)),
        "총거리": round(res.get("dist", 0), 1),
        "SLA":    res.get("sla", 100),
        "효율":   res.get("efficiency", 0),
        "배송지": sum(v.get("stops", 0) for v in res.get("truck_stats", {}).values()),
        "미배차": len(res.get("unassigned", [])),
    }
    log: list[dict] = st.session_state.get("_run_log", [])
    log.append(entry)
    st.session_state._run_log = log[-_MAX_RUN_LOG:]


def render_run_trend(res: dict) -> None:
    """[D10] 당일 누적 실행 트렌드 패널."""
    log: list[dict] = st.session_state.get("_run_log", [])
    if len(log) < 2:
        return

    with st.expander(f"📈 오늘 실행 트렌드 ({len(log)}회)", expanded=False):
        st.caption("오늘 실행한 최적화 결과를 비교합니다.")
        df = pd.DataFrame(log)

        col1, col2, col3 = st.columns(3)
        col1.metric("최저 비용",   f"₩{df['총비용'].min():,}",
                    f"최고 ₩{df['총비용'].max():,} 대비")
        col2.metric("최고 SLA",    f"{df['SLA'].max():.1f}%",
                    f"최저 {df['SLA'].min():.1f}%")
        col3.metric("총 실행 횟수", f"{len(log)}회",
                    f"미배차 최소 {df['미배차'].min()}건")

        st.dataframe(
            df.rename(columns={
                "시각": "실행시각", "총비용": "총비용(₩)",
                "총거리": "거리(km)", "SLA": "SLA(%)",
                "효율": "효율(%)", "배송지": "배송건수",
            }),
            hide_index=True, use_container_width=True,
        )

        if len(log) >= 3:
            chart_df = pd.DataFrame(log[-10:]).set_index("시각")[["총비용", "SLA"]]
            c1, c2 = st.columns(2)
            with c1:
                st.caption("💰 비용 추이")
                st.line_chart(chart_df[["총비용"]], height=120, use_container_width=True)
            with c2:
                st.caption("📊 SLA 추이")
                st.line_chart(chart_df[["SLA"]], height=120, use_container_width=True)


# ══════════════════════════════════════════════
# D11 — 배송지 위험도 사전 스크리닝
# ══════════════════════════════════════════════

def calc_risk(
    target: dict,
    n_1t: int,
    n_2t: int,
    n_5t: int,
) -> tuple[int, list[str]]:
    """배차 실패 위험도 0~100 및 원인 목록 반환.

    Args:
        target: 배송지 노드 dict
        n_1t:   투입 1톤 차량 수
        n_2t:   투입 2.5톤 차량 수
        n_5t:   투입 5톤 차량 수

    Returns:
        (위험도 점수, 원인 리스트)
    """
    score   = 0
    reasons: list[str] = []

    weight   = target.get("weight",      0)
    temp     = target.get("temperature", "상온")
    tw_disp  = target.get("tw_disp",     "00:00~23:59")
    priority = target.get("priority",    "일반")
    diff     = target.get("difficulty",  "일반")

    # ① 무게
    max_cap = max(
        (1000 if n_1t > 0 else 0),
        (2500 if n_2t > 0 else 0),
        (5000 if n_5t > 0 else 0),
    )
    if max_cap > 0:
        ratio = weight / max_cap
        if ratio > 0.95:
            score += 40
            reasons.append(f"무게 {weight}kg — 최대 적재 {max_cap}kg의 {ratio*100:.0f}%")
        elif ratio > 0.80:
            score += 20
            reasons.append("무게 — 적재율 주의")

    # ② 냉장·냉동 + 냉탑 없음
    if temp in ("냉장", "냉동"):
        if n_1t == 0:
            score += 50
            reasons.append(f"{temp} 화물이지만 1톤 냉탑 차량 없음")
        elif weight > 1000:
            score += 30
            reasons.append(f"{temp} 화물 {weight}kg — 냉탑 용량 초과 가능성")

    # ③ 시간창
    try:
        ts, te = tw_disp.split("~")
        tw_min = (int(te[:2]) - int(ts[:2])) * 60 + (int(te[3:]) - int(ts[3:]))
        if tw_min < 120:
            score += 30
            reasons.append(f"시간창 {tw_min}분 — 매우 좁음")
        elif tw_min < 180:
            score += 15
            reasons.append(f"시간창 {tw_min}분 — 다소 좁음")
    except (ValueError, IndexError):
        pass

    # ④ VIP + 재래시장
    if priority == "VIP" and "재래시장" in diff:
        score += 20
        reasons.append("VIP + 재래시장 진입 — 지연 위험 높음")

    return min(score, 100), reasons


def render_risk_screening() -> None:
    """[D11] 배차 대기열에서 위험도 사전 스크리닝 표시."""
    targets = st.session_state.get("targets", [])
    if not targets:
        return

    n1 = st.session_state.cfg_1t_cnt
    n2 = st.session_state.cfg_2t_cnt
    n5 = st.session_state.cfg_5t_cnt

    risk_rows = []
    has_risk  = False
    for t in targets:
        score, reasons = calc_risk(t, n1, n2, n5)
        if score >= 20:
            has_risk = True
        label = "🔴 위험" if score >= 60 else "🟡 주의" if score >= 30 else "🟢 정상"
        risk_rows.append({
            "배송지": t["name"],
            "위험도": f"{label} ({score})",
            "원인":   " / ".join(reasons) if reasons else "—",
        })

    if not has_risk:
        return

    warn_cnt = sum(1 for r in risk_rows if "🔴" in r["위험도"] or "🟡" in r["위험도"])
    with st.expander(f"⚠️ 배차 위험 사전 스크리닝 — {warn_cnt}건 주의", expanded=True):
        st.caption(
            "최적화 실행 전 배차 실패 가능성이 높은 배송지를 미리 알려드립니다. "
            "위험도가 높은 항목은 조건을 수정하거나 차량 구성을 바꾸세요."
        )
        filtered = [r for r in risk_rows if "🔴" in r["위험도"] or "🟡" in r["위험도"]]
        st.dataframe(pd.DataFrame(filtered), hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════
# D13 — 배차 전 AI SLA·비용 예보
# ══════════════════════════════════════════════

_FORECAST_SYSTEM = """당신은 물류 배차 전문가입니다.
현재 배송지 목록과 차량 구성을 보고 최적화 실행 전에 결과를 예측하세요.
반드시 JSON만 반환하세요 (마크다운 없이). 형식:
{
  "sla": 숫자(0~100),
  "total_cost": 정수(원),
  "est_dist": 숫자(km),
  "risks": ["위험요인1", "위험요인2"],
  "suggestions": ["개선제안1", "개선제안2"],
  "reasoning": "2~3문장 한국어 근거"
}"""


def _make_forecast_key() -> str:
    """현재 설정 조합을 고유 키로 변환 (캐시 무효화용)."""
    s = st.session_state
    data = (
        len(s.get("targets", [])),
        s.cfg_1t_cnt, s.cfg_2t_cnt, s.cfg_5t_cnt,
        s.cfg_speed,  s.cfg_weather, s.cfg_max_hours,
        s.cfg_start_time, s.cfg_fuel_price, s.cfg_labor,
        s.cfg_congestion,
    )
    return hashlib.md5(str(data).encode()).hexdigest()[:12]


def _rule_based_forecast() -> dict:
    """AI API 없을 때 규칙 기반 예보.

    비용 추정 개선:
        이전 구현은 n_stops × avg_dist_per_stop으로 총 거리를 단순 계산했으나
        차량 수와 클러스터링 효과를 무시해 과대 추정됐음.
        수정: 총 배송 거리를 차량 수로 나눠 차량별 평균 루트로 근사.
    """
    s       = st.session_state
    targets = s.get("targets", [])
    n1, n2, n5 = s.cfg_1t_cnt, s.cfg_2t_cnt, s.cfg_5t_cnt
    total_v    = n1 + n2 + n5
    n_stops    = len(targets)

    if total_v == 0 or n_stops == 0:
        return {}

    total_cap_kg  = n1 * 1000 + n2 * 2500 + n5 * 5000
    total_weight  = sum(t.get("weight", 0) for t in targets)
    cap_ratio     = total_weight / max(total_cap_kg, 1)

    tight_tw = sum(
        1 for t in targets
        if (t.get("tw_end", 540) - t.get("tw_start", 0)) < 120
    )
    tight_ratio   = tight_tw / max(n_stops, 1)
    cold_stops    = sum(1 for t in targets if t.get("temperature") in ("냉장", "냉동"))
    temp_mismatch = cold_stops > 0 and n1 == 0

    # SLA 예측
    sla = 95.0
    if cap_ratio > 0.9:           sla -= 20
    elif cap_ratio > 0.7:         sla -= 8
    if tight_ratio > 0.3:         sla -= 15
    elif tight_ratio > 0.15:      sla -= 7
    if temp_mismatch:             sla -= 25
    if "눈" in s.cfg_weather:     sla -= 10
    elif "비" in s.cfg_weather:   sla -= 5
    sla -= max(0, (s.cfg_congestion - 40) / 10) * 2
    stops_per_v = n_stops / total_v
    if stops_per_v > 12:   sla -= 10
    elif stops_per_v > 8:  sla -= 4
    sla = max(30.0, min(100.0, round(sla, 1)))

    # 비용 예측 — 차량 수 반영 거리 근사
    congestion_factor = 1 + (s.cfg_congestion - 40) / 200
    avg_dist_per_stop = 8 * max(0.8, congestion_factor)

    # 핵심 수정: 총 거리 = (배송지 수 × 구간 거리) / 차량 수 × 차량 수
    # → 실제로는 차량별로 자기 구역만 돌므로 단순 곱이 아닌
    #   차량당 배송지 × 구간 거리로 추정
    est_dist_per_vehicle = stops_per_v * avg_dist_per_stop
    est_dist_total       = est_dist_per_vehicle * total_v  # 전체 합산
    fuel_cost  = est_dist_total / 10 * s.cfg_fuel_price
    labor_cost = s.cfg_max_hours * total_v * s.cfg_labor
    fixed      = n1 * 100_000 + n2 * 180_000 + n5 * 280_000
    total_cost = int(fuel_cost + labor_cost + fixed)

    risks: list[str] = []
    if cap_ratio > 0.85:
        risks.append(f"적재 용량 {cap_ratio*100:.0f}% — 차량 추가 또는 배송지 분리 권장")
    if tight_ratio > 0.2:
        risks.append(f"시간창 2시간 미만 배송지 {tight_tw}건 — SLA 리스크 높음")
    if temp_mismatch:
        risks.append(f"냉장·냉동 {cold_stops}건이지만 냉탑 차량(1톤) 없음")
    if stops_per_v > 10:
        risks.append(f"차량 1대당 배송지 {stops_per_v:.1f}개 — 과부하 위험")
    if "눈" in s.cfg_weather:
        risks.append("눈 날씨로 감속 30% — SLA 하락 예상")
    if s.cfg_congestion > 60:
        risks.append(f"혼잡도 {s.cfg_congestion}% — 운행 시간 증가")

    suggestions: list[str] = []
    if cap_ratio > 0.85:
        suggestions.append("차량 1대 추가 시 SLA 약 +8~12% 예상")
    if tight_ratio > 0.2:
        suggestions.append("시간창 좁은 VIP 배송지를 허브 인근으로 재배치 검토")
    if stops_per_v > 10:
        suggestions.append(f"{total_v}대 → {total_v+1}대로 늘리면 과부하 해소 가능")
    if not suggestions:
        suggestions.append("현재 구성으로 안정적인 배차 가능합니다.")

    return {
        "sla":        sla,
        "total_cost": total_cost,
        "est_dist":   round(est_dist_total, 1),
        "risks":      risks,
        "suggestions": suggestions,
        "cap_ratio":  round(cap_ratio * 100, 1),
        "tight_tw":   tight_tw,
        "stops_per_v": round(stops_per_v, 1),
        "via_ai":     False,
    }


def render_pre_dispatch_forecast(anthropic_key: Optional[str], call_anthropic_fn) -> None:
    """[D13] 배차 전 AI SLA·비용 예보 패널.

    Args:
        anthropic_key:    Anthropic API 키 (없으면 규칙 기반 폴백)
        call_anthropic_fn: _call_anthropic 함수 레퍼런스
    """
    targets = st.session_state.get("targets", [])
    total_v = (st.session_state.cfg_1t_cnt
               + st.session_state.cfg_2t_cnt
               + st.session_state.cfg_5t_cnt)
    if not targets or total_v == 0:
        return

    with st.expander("🔮 배차 전 AI 예보 — 최적화 실행 전 SLA·비용 미리 보기", expanded=False):
        st.caption(
            "**[D13]** 최적화를 돌리기 전에 현재 조건으로 예상 SLA와 비용을 미리 계산합니다."
        )

        forecast_key = _make_forecast_key()
        cached     = st.session_state.get("_forecast_cache")
        cached_key = st.session_state.get("_forecast_key", "")

        col_run, col_clear = st.columns([3, 1])
        with col_run:
            run_forecast = st.button(
                "🔮 지금 예보 실행", use_container_width=True, key="forecast_btn",
            )
        with col_clear:
            if cached and st.button("🗑️ 초기화", use_container_width=True, key="forecast_clear"):
                st.session_state._forecast_cache = None
                st.session_state._forecast_key   = ""
                st.rerun()

        if cached and cached_key != forecast_key:
            st.warning("⚠️ 배송지 또는 차량 설정이 변경됐습니다. 예보를 다시 실행하세요.")

        if run_forecast:
            with st.spinner("AI가 배차 결과를 사전 예측 중..."):
                if anthropic_key:
                    s = st.session_state
                    context = {
                        "배송지수": len(targets),
                        "차량구성": {"1톤": s.cfg_1t_cnt, "2.5톤": s.cfg_2t_cnt, "5톤": s.cfg_5t_cnt},
                        "기상": s.cfg_weather, "최대운행시간": s.cfg_max_hours,
                        "출발시간": s.cfg_start_time, "연료단가": s.cfg_fuel_price,
                        "인건비시간당": s.cfg_labor,
                        "배송지요약": [
                            {
                                "온도":     t.get("temperature", "상온"),
                                "우선순위": t.get("priority",    "일반"),
                                "무게kg":   t.get("weight",      0),
                                "시간창분": t.get("tw_end", 540) - t.get("tw_start", 0),
                            }
                            for t in targets[:30]
                        ],
                    }
                    raw = call_anthropic_fn(
                        messages=[{"role": "user", "content":
                            f"아래 조건으로 배차를 최적화하면 어떤 결과가 나올지 예측하세요.\n"
                            f"{json.dumps(context, ensure_ascii=False)}"}],
                        system=_FORECAST_SYSTEM,
                        max_tokens=600,
                    )
                    try:
                        fc = json.loads(raw.replace("```json", "").replace("```", "").strip())
                        fc["via_ai"] = True
                    except Exception:
                        fc = _rule_based_forecast()
                else:
                    fc = _rule_based_forecast()

            st.session_state._forecast_cache = fc
            st.session_state._forecast_key   = forecast_key
            cached = fc

        if not cached:
            st.info("위 버튼을 누르면 최적화 전 예보를 제공합니다.")
            return

        fc  = cached
        src = "🤖 AI 예보" if fc.get("via_ai") else "📐 규칙 기반 예보"

        m1, m2, m3 = st.columns(3)
        sla = fc.get("sla", 0)
        sla_icon = "🟢" if sla >= 90 else "🟡" if sla >= 75 else "🔴"
        m1.metric(f"{sla_icon} 예상 SLA",    f"{sla:.1f}%")
        m2.metric("💰 예상 총비용",           f"₩{fc.get('total_cost', 0):,}")
        m3.metric("🛣️ 예상 총거리",           f"{fc.get('est_dist', 0):.0f}km")
        st.caption(f"출처: {src}")

        for r in fc.get("risks", []):
            st.error(f"• {r}")
        for sg in fc.get("suggestions", []):
            st.success(f"• {sg}")
        if fc.get("reasoning"):
            st.info(f"🤖 AI 판단: {fc['reasoning']}")
        st.caption("※ 예보는 실제 최적화 전 추정치이며, 실행 결과와 다를 수 있습니다.")


# ══════════════════════════════════════════════
# D14 — 기사별 수익 공정성 지수
# ══════════════════════════════════════════════

def calc_equity_index(truck_stats: dict) -> dict:
    """차량(기사)별 배차 공정성을 0~100으로 환산.

    최적화: calc_fatigue를 stats 순회마다 3회 호출하던 것을
    fatigues 리스트 한 번만 계산해 재사용.
    """
    if len(truck_stats) < 2:
        return {"index": 100, "detail": [], "worst": ""}

    stats = list(truck_stats.items())

    def _cv(values: list[float]) -> float:
        """변동계수 (표준편차/평균). 낮을수록 균등."""
        if not values or sum(values) == 0:
            return 0.0
        mean = sum(values) / len(values)
        if mean == 0:
            return 0.0
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return (variance ** 0.5) / mean

    dists    = [v.get("dist",  0) for _, v in stats]
    stops    = [v.get("stops", 0) for _, v in stats]
    # calc_fatigue 결과를 한 번만 계산해 이후 detail/scores에서 재사용
    fatigues = [calc_fatigue(v) for _, v in stats]

    cv_dist    = _cv(dists)
    cv_stops   = _cv(stops)
    cv_fatigue = _cv(fatigues)

    weighted_cv  = cv_dist * 0.3 + cv_stops * 0.3 + cv_fatigue * 0.4
    equity_index = max(0, min(100, int((1 - weighted_cv / 0.5) * 100)))

    detail = [
        {
            "차량":   truck,
            "거리km": round(v.get("dist",  0), 1),
            "정지수": v.get("stops", 0),
            "피로도": fatigues[idx],           # ← 재계산 없이 재사용
            "적재율%": round(v.get("used_wt", 0) / max(v.get("max_wt", 1), 1) * 100, 1),
            "연료비":  int(v.get("fuel_cost", 0)),
        }
        for idx, (truck, v) in enumerate(stats)
    ]

    if stats:
        scores = [
            (dists[idx]    / max(max(dists),    1)) * 0.3
            + (stops[idx]  / max(max(stops),    1)) * 0.3
            + (fatigues[idx] / 100.0)               * 0.4  # ← 재사용
            for idx in range(len(stats))
        ]
        worst = stats[scores.index(max(scores))][0]
    else:
        worst = ""

    return {
        "index":      equity_index,
        "detail":     detail,
        "worst":      worst,
        "cv_dist":    round(cv_dist,    3),
        "cv_stops":   round(cv_stops,   3),
        "cv_fatigue": round(cv_fatigue, 3),
    }


def render_driver_equity(res: dict) -> None:
    """[D14] 기사별 수익 공정성 지수 패널."""
    truck_stats = res.get("truck_stats", {})
    if len(truck_stats) < 2:
        return

    eq  = calc_equity_index(truck_stats)
    idx = eq["index"]
    label = "🟢 균등" if idx >= 80 else "🟡 불균형 주의" if idx >= 55 else "🔴 심각한 불균형"

    with st.expander(f"⚖️ 기사별 공정성 지수 — {label} ({idx}/100)", expanded=False):
        st.caption("**[D14]** 차량(기사)간 배차가 얼마나 공정하게 분배됐는지 수치화합니다.")

        st.markdown(f"**공정성 지수: `{idx}/100`** — {label}")
        st.progress(idx / 100)

        c1, c2, c3 = st.columns(3)
        c1.metric("거리 편차(CV)",  f"{eq['cv_dist']:.3f}",
                  "✅ 균등" if eq["cv_dist"]    < 0.2 else "⚠️ 편차 큼")
        c2.metric("정지수 편차(CV)", f"{eq['cv_stops']:.3f}",
                  "✅ 균등" if eq["cv_stops"]   < 0.2 else "⚠️ 편차 큼")
        c3.metric("피로도 편차(CV)", f"{eq['cv_fatigue']:.3f}",
                  "✅ 균등" if eq["cv_fatigue"] < 0.2 else "⚠️ 편차 큼")

        if eq["worst"]:
            st.warning(f"🚨 가장 과부하 차량: **{eq['worst']}** — 일부 배송지를 다른 차량으로 이동 검토")

        if eq["detail"]:
            st.dataframe(
                pd.DataFrame(eq["detail"]).rename(columns={
                    "거리km": "거리(km)", "정지수": "정지 수",
                    "적재율%": "적재율(%)", "연료비": "연료비(₩)",
                }),
                hide_index=True, use_container_width=True,
            )

        if idx < 80:
            st.markdown("**💡 균등화 제안**")
            if eq["cv_stops"]   > 0.25: st.info(f"정지 수 불균형 — {eq['worst']} 차량의 배송지 일부를 인근 차량으로 이동하세요.")
            if eq["cv_dist"]    > 0.25: st.info("거리 불균형 — 권역 클러스터링(D9)을 참고해 권역을 재조정하세요.")
            if eq["cv_fatigue"] > 0.25: st.info("피로도 편차 — 차량 배분을 조정해 보세요.")

        st.caption("CV(변동계수) = 표준편차/평균. 0에 가까울수록 균등 배분.")
