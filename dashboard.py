"""
dashboard.py — 최적화 결과 대시보드
"""
import json
import logging
from datetime import datetime

import pandas as pd
import requests
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
    tab_sum, tab_util, tab_esg, tab_lifo, tab_cost, tab_ai = st.tabs([
        "🚛 운행 요약", "📦 적재율", "🌱 탄소 배출", "📋 상차 순서", "💰 비용 명세", "🤖 AI 브리핑",
    ])

    with tab_sum:   _tab_summary(tstats)
    with tab_util:  _tab_utilization(tstats)
    with tab_esg:   _tab_esg(tstats)
    with tab_lifo:  _tab_lifo(tstats)
    with tab_cost:  _tab_cost(res, tstats)
    with tab_ai:    _tab_ai(res, tstats)


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


# ── AI 브리핑 ─────────────────────────────────────
def _tab_ai(res, tstats):
    """Claude API로 배차 결과 자동 브리핑 (버튼 클릭 한 번)"""

    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        import os
        api_key = os.getenv("ANTHROPIC_API_KEY", "")

    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "hub":  res["hub_name"],
        "summary": {
            "vehicles":       len(res["routes"]),
            "total_cost":     int(res["total_cost"]),
            "sla_pct":        res.get("sla", 100),
            "co2_kg":         round(res.get("co2_total", 0), 1),
            "dist_km":        round(res["dist"], 1),
            "efficiency_pct": res.get("efficiency", 0),
            "wait_min":       int(res.get("wait_time_total", 0)),
        },
        "unassigned": [
            {"name": n["name"], "reason": n["reason"]}
            for n in res.get("unassigned_diagnosed", [])
        ],
        "vehicles": {
            k: {
                "stops":        v["stops"],
                "dist_km":      round(v["dist"], 1),
                "co2_kg":       round(v["co2_kg"], 1),
                "wt_util_pct":  round(v["used_wt"]  / v["max_wt"]  * 100, 1) if v["max_wt"]  > 0 else 0,
                "vol_util_pct": round(v["used_vol"] / v["max_vol"] * 100, 1) if v["max_vol"] > 0 else 0,
            }
            for k, v in tstats.items()
        },
    }

    # 세션에 캐시된 브리핑이 있으면 바로 표시
    cached = st.session_state.get("_ai_briefing")
    if cached:
        st.success("✅ AI 브리핑이 생성되었습니다.")
        st.markdown(cached)
        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "⬇️ 브리핑 다운로드",
                data=cached.encode("utf-8"),
                file_name=f"배차브리핑_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                mime="text/plain",
                use_container_width=True,
            )
        with col2:
            if st.button("🔄 다시 생성", use_container_width=True):
                del st.session_state["_ai_briefing"]
                st.rerun()
        return

    st.caption("오늘 배차 결과를 Claude가 경영진 보고용으로 자동 요약합니다.")

    if not api_key:
        st.warning("ANTHROPIC_API_KEY가 설정되지 않았습니다. Streamlit Secrets를 확인해주세요.")
        return

    if st.button("🤖 AI 브리핑 생성", type="primary", use_container_width=True):
        prompt = (
            f"다음 물류 배차 데이터를 보고 한국어로 경영진 보고용 일일 배차 브리핑을 작성해주세요.\n\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
            f"아래 5가지 항목으로 간결하게 작성하세요:\n"
            f"① 전체 현황 요약\n"
            f"② 성과 및 위험 요소\n"
            f"③ 배차 불가 원인과 대안 (없으면 생략)\n"
            f"④ ESG·탄소 배출 현황\n"
            f"⑤ 내일 운영 권고사항"
        )
        with st.spinner("Claude가 브리핑을 작성 중입니다..."):
            try:
                r = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    timeout=30,
                )
                if r.status_code == 200:
                    result = r.json()["content"][0]["text"]
                    st.session_state["_ai_briefing"] = result
                    st.rerun()
                else:
                    logger.warning("Claude API %d: %s", r.status_code, r.text[:200])
                    st.error(f"API 오류 ({r.status_code}). 잠시 후 다시 시도해주세요.")
            except requests.RequestException as e:
                logger.warning("Claude API network error: %s", e)
                st.error("네트워크 오류가 발생했습니다.")
