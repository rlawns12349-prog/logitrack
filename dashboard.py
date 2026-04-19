"""
dashboard.py — 최적화 결과 대시보드
"""
import json
import logging
from datetime import datetime

import pandas as pd
import streamlit as st

from solver import compute_truck_financials

logger = logging.getLogger("logitrack")


def render_dashboard(res: dict):
    # ── 뒤로가기 ──────────────────────────────────
    if st.button("← 대기열로 돌아가기"):
        st.session_state.opt_result = None
        st.rerun()

    st.divider()

    # ── 배차 불가 경고 ────────────────────────────
    for item in res.get("unassigned_diagnosed", []):
        st.error(f"🚨 **배차 불가: {item['name']}** — {item['reason']}")

    # ── KPI 메트릭 ────────────────────────────────
    st.subheader("📊 오늘의 배차 결과")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("총 예상 비용",  f"₩{int(res['total_cost']):,}")
    c2.metric("납기 준수율",   f"{res.get('sla', 100.0)}%",
              delta="목표달성" if res.get('sla', 100) >= 95 else f"{-res.get('late_count', 0)}건 지연",
              delta_color="normal" if res.get('sla', 100) >= 95 else "inverse")
    c3.metric("거리 효율",    f"{res.get('efficiency', 0):+.1f}%",
              help="최단 경로 대비 단축률. +가 클수록 효율적입니다.")
    c4.metric("탄소 배출량",  f"{res.get('co2_total', 0):.1f} kg")
    c5.metric("운영 차량",    f"{len(res['routes'])} 대")

    total_stops = sum(s["stops"] for s in res["truck_stats"].values())
    wait_total  = int(res.get("wait_time_total", 0))
    st.caption(f"배차 거점 {total_stops}개소  |  총 대기 {wait_total}분")

    st.divider()

    # ── 탭 ───────────────────────────────────────
    tstats = res.get("truck_stats", {})
    tab_sum, tab_util, tab_esg, tab_lifo, tab_cost = st.tabs([
        "🚛 운행 요약", "📦 적재율", "🌱 탄소 배출", "📋 상차 순서", "💰 비용 명세",
    ])

    with tab_sum:   _tab_summary(tstats)
    with tab_util:  _tab_utilization(tstats)
    with tab_esg:   _tab_esg(tstats)
    with tab_lifo:  _tab_lifo(tstats)
    with tab_cost:  _tab_cost(res, tstats)


# ── 운행 요약 ─────────────────────────────────────
def _tab_summary(tstats):
    for tn, s in tstats.items():
        rstr   = " → ".join(s["route_names"]) or "배송지 없음"
        th     = int(s["time"] // 60)
        tm     = int(s["time"] % 60)
        fin    = compute_truck_financials(s, st.session_state.cfg_fuel_price,
                                          st.session_state.cfg_labor)
        wt_pct = (s["used_wt"] / s["max_wt"] * 100) if s["max_wt"] > 0 else 0

        with st.container(border=True):
            col_l, col_r = st.columns([3, 1])
            with col_l:
                st.markdown(f"**{tn}**")
                st.caption(rstr)
            with col_r:
                st.caption(f"{s['stops']}개소 · {s['dist']:.1f}km · {th}h {tm}m")

            c1, c2, c3 = st.columns(3)
            c1.metric("변동비", f"₩{int(fin['total_variable']):,}")
            c2.metric("대기",   f"{int(s['wait_time'])}분")
            c3.metric("적재율", f"{wt_pct:.0f}%")


# ── 적재율 ────────────────────────────────────────
def _tab_utilization(tstats):
    for tn, s in tstats.items():
        with st.container(border=True):
            st.markdown(f"**{tn}**")
            wp = (s["used_wt"]  / s["max_wt"])  * 100 if s["max_wt"]  > 0 else 0
            vp = (s["used_vol"] / s["max_vol"]) * 100 if s["max_vol"] > 0 else 0
            st.progress(wp / 100, text=f"중량  {s['used_wt']}/{s['max_wt']}kg ({wp:.1f}%)")
            st.progress(vp / 100, text=f"부피  {s['used_vol']}/{s['max_vol']}CBM ({vp:.1f}%)")
            if wp > 90: st.warning("중량 90% 초과")
            if vp > 90: st.warning("부피 90% 초과")


# ── 탄소 배출 ─────────────────────────────────────
def _tab_esg(tstats):
    st.caption("GLEC 기준 Scope 3 탄소 배출량")
    rows = [
        {
            "트럭":          tn,
            "이동 거리":     f"{s['dist']:.1f}km",
            "연료 소비(L)":  f"{s['fuel_liter']:.1f}",
            "CO₂ 배출(kg)": f"{s['co2_kg']:.1f}",
            "상쇄 소나무":   f"{s['co2_kg'] / 6.6:.1f}그루",
        }
        for tn, s in tstats.items()
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


# ── 상차 순서 ─────────────────────────────────────
def _tab_lifo(tstats):
    st.caption("마지막 하차지 화물을 가장 안쪽에 적재합니다 (LIFO)")
    for tn, s in tstats.items():
        if not s["loads_detail"]:
            continue
        seq = " → ".join(d["name"] for d in reversed(s["loads_detail"]))
        st.success(f"**{tn}:** {seq}")


# ── 비용 명세 ─────────────────────────────────────
def _tab_cost(res, tstats):
    def fw(v): return f"₩{int(v):,}"
    def ft(t): return f"{int(t // 60)}h {int(t % 60)}m"

    rows = []
    for tn, s in tstats.items():
        fin = compute_truck_financials(s, st.session_state.cfg_fuel_price,
                                       st.session_state.cfg_labor)
        rows.append({
            "트럭":   tn,
            "거점수": s["stops"],
            "거리":   f"{s['dist']:.1f}km",
            "시간":   ft(s["time"]),
            "고정비": fw(s["cost"]),
            "통행료": fw(s["toll_cost"]),
            "연료비": fw(fin["fuel_cost"]),
            "인건비": fw(fin["labor_cost"]),
            "소계":   fw(fin["grand_total"]),
        })
    rows.append({
        "트럭":   "합계",
        "거점수": sum(s["stops"] for s in tstats.values()),
        "거리":   f"{res['dist']:.1f}km",
        "시간":   ft(sum(s["time"] for s in tstats.values())),
        "고정비": fw(res["fixed_cost"]),
        "통행료": fw(res["toll_cost"]),
        "연료비": fw(res["fuel_cost"]),
        "인건비": fw(res["labor"]),
        "소계":   fw(res["total_cost"]),
    })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
