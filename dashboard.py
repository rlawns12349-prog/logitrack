"""
dashboard.py — 최적화 결과 대시보드
"""
import logging
import pandas as pd
import streamlit as st
from solver import compute_truck_financials

logger = logging.getLogger("logitrack")

# ── 팔레트 ───────────────────────────────────────
_CARD_BG     = "#1e2535"   # 앱 배경(#0f1117)보다 확실히 밝음
_CARD_BORDER = "#2e3f5c"
_INNER_BG    = "#141a24"   # 카드 안 중첩 요소
_TEXT        = "#e2e8f0"
_MUTED       = "#64748b"
_DIM         = "#374151"

def _fw(v): return f"₩{int(v):,}"
def _ft(t): return f"{int(t//60)}h {int(t%60)}m"

def _card(content: str, pad: str = "20px 24px") -> str:
    return (
        f'<div style="background:{_CARD_BG};border:1px solid {_CARD_BORDER};'
        f'border-radius:14px;padding:{pad};margin-bottom:14px;">'
        f'{content}</div>'
    )

def _label(text: str) -> str:
    return (
        f'<div style="font-size:0.65rem;font-weight:700;color:{_MUTED};'
        f'letter-spacing:0.09em;text-transform:uppercase;margin-bottom:6px;">{text}</div>'
    )

def _value(text: str, color: str = _TEXT, size: str = "1.5rem") -> str:
    return (
        f'<div style="font-size:{size};font-weight:700;color:{color};'
        f'letter-spacing:-0.02em;line-height:1.1;">{text}</div>'
    )

def _sub(text: str, color: str = _MUTED) -> str:
    return f'<div style="font-size:0.72rem;color:{color};margin-top:5px;">{text}</div>'

def _pill(text: str, color: str) -> str:
    return (
        f'<span style="font-size:0.7rem;font-weight:700;color:{color};'
        f'background:{color}22;border:1px solid {color}55;'
        f'padding:3px 10px;border-radius:20px;">{text}</span>'
    )

def _bar_row(label, used, total, unit, pct, color):
    return (
        f'<div style="margin-bottom:14px;">'
        f'<div style="display:flex;justify-content:space-between;margin-bottom:6px;">'
        f'<span style="font-size:0.73rem;color:{_MUTED};">{label}</span>'
        f'<span style="font-size:0.73rem;font-weight:600;color:{color};">'
        f'{used} / {total} {unit} ({pct:.0f}%)</span></div>'
        f'<div style="height:8px;background:{_INNER_BG};border-radius:4px;overflow:hidden;">'
        f'<div style="height:100%;width:{min(pct,100):.1f}%;background:{color};'
        f'border-radius:4px;"></div></div></div>'
    )


# ════════════════════════════════════════════════
# 메인 렌더러
# ════════════════════════════════════════════════

def render_dashboard(res: dict):
    # ── 상단바 ───────────────────────────────────
    col_back, col_title = st.columns([1, 6])
    with col_back:
        if st.button("← 돌아가기", use_container_width=True):
            st.session_state.opt_result = None
            st.rerun()
    with col_title:
        st.markdown(
            f'<div style="padding:5px 0;font-size:1.25rem;font-weight:800;color:{_TEXT};">'
            f'📊 배차 최적화 결과</div>',
            unsafe_allow_html=True,
        )

    # ── 배차 불가 알림 ───────────────────────────
    for item in res.get("unassigned_diagnosed", []):
        st.markdown(
            f'<div style="background:#2a0a0a;border:1px solid #6b1c1c;'
            f'border-left:4px solid #ef4444;border-radius:10px;'
            f'padding:12px 18px;margin-bottom:8px;display:flex;gap:10px;align-items:center;">'
            f'<span style="font-size:1rem;">🚨</span>'
            f'<div><span style="font-size:0.82rem;font-weight:700;color:#fca5a5;">'
            f'배차 불가: {item["name"]}</span>'
            f'<span style="font-size:0.78rem;color:#7f2020;margin-left:8px;">— {item["reason"]}</span>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    # ── KPI 5칸 ──────────────────────────────────
    sla         = res.get("sla", 100.0)
    eff         = res.get("efficiency", 0)
    late        = res.get("late_count", 0)
    total_stops = sum(s["stops"] for s in res["truck_stats"].values())
    sla_c = "#4ade80" if sla >= 95 else "#fb923c" if sla >= 80 else "#f87171"
    eff_c = "#4ade80" if eff >= 0 else "#fb923c"

    kpis = [
        ("총 예상 비용",  f"₩{int(res['total_cost']):,}",    f"고정비 {_fw(res['fixed_cost'])}",  _TEXT),
        ("납기 준수율",   f"{sla}%",                          "목표달성 ✓" if sla>=95 else f"{late}건 지연", sla_c),
        ("거리 효율",     f"{eff:+.1f}%",                     "NN 대비 단축률",                     eff_c),
        ("탄소 배출",     f"{res.get('co2_total',0):.1f} kg", f"통행료 {_fw(int(res['toll_cost']))}", _TEXT),
        ("운영 차량",     f"{len(res['routes'])} 대",         f"총 {total_stops}개소 배송",          _TEXT),
    ]
    cols = st.columns(5)
    for col, (lbl, val, sub_txt, col_val) in zip(cols, kpis):
        col.markdown(
            f'<div style="background:{_CARD_BG};border:1px solid {_CARD_BORDER};'
            f'border-radius:14px;padding:18px 20px;min-height:100px;">'
            + _label(lbl)
            + _value(val, col_val)
            + _sub(sub_txt, sla_c if lbl=="납기 준수율" and sla<95 else _MUTED)
            + f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    # ── 탭 ───────────────────────────────────────
    tstats = res.get("truck_stats", {})
    tab_sum, tab_util, tab_esg, tab_lifo, tab_cost = st.tabs([
        "🚛 운행 요약", "📦 적재율", "🌱 탄소 배출", "📋 상차 순서", "💰 비용 명세",
    ])
    with tab_sum:  _tab_summary(tstats)
    with tab_util: _tab_utilization(tstats)
    with tab_esg:  _tab_esg(tstats)
    with tab_lifo: _tab_lifo(tstats)
    with tab_cost: _tab_cost(res, tstats)


# ════════════════════════════════════════════════
# 운행 요약 탭
# ════════════════════════════════════════════════

def _tab_summary(tstats):
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    for tn, s in tstats.items():
        fin    = compute_truck_financials(s, st.session_state.cfg_fuel_price,
                                          st.session_state.cfg_labor)
        wt_pct = (s["used_wt"] / s["max_wt"] * 100) if s["max_wt"] > 0 else 0
        wc     = "#f87171" if wt_pct>90 else "#fb923c" if wt_pct>70 else "#4ade80"
        th, tm = int(s["time"]//60), int(s["time"]%60)
        route  = " → ".join(s["route_names"]) or "배송지 없음"

        # 헤더
        header = (
            f'<div style="display:flex;justify-content:space-between;'
            f'align-items:center;margin-bottom:14px;">'
            f'<div>'
            f'<span style="font-size:1.05rem;font-weight:800;color:{_TEXT};">{tn}</span>'
            f'<span style="font-size:0.73rem;color:{_DIM};margin-left:12px;">'
            f'{s["stops"]}개소 · {s["dist"]:.1f} km · {th}h {tm}m</span>'
            f'</div>'
            + _pill(f"적재 {wt_pct:.0f}%", wc) +
            f'</div>'
        )
        # 경로
        route_box = (
            f'<div style="font-size:0.75rem;color:{_MUTED};padding:9px 14px;'
            f'background:{_INNER_BG};border-radius:8px;border-left:3px solid #2e4a7a;'
            f'margin-bottom:16px;word-break:break-all;line-height:1.6;">{route}</div>'
        )
        # 지표 3칸
        def _metric_box(lbl, val, color=_TEXT):
            return (
                f'<div style="background:{_INNER_BG};border-radius:10px;padding:14px 16px;">'
                + _label(lbl) + _value(val, color, "0.95rem") +
                f'</div>'
            )
        metrics = (
            f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;">'
            + _metric_box("변동비",   _fw(fin["total_variable"]))
            + _metric_box("대기 시간", f'{int(s["wait_time"])}분',
                          "#fb923c" if s["wait_time"]>30 else "#94a3b8")
            + _metric_box("연료 소비", f'{s["fuel_liter"]:.1f} L', "#94a3b8")
            + f'</div>'
        )
        st.markdown(_card(header + route_box + metrics), unsafe_allow_html=True)


# ════════════════════════════════════════════════
# 적재율 탭
# ════════════════════════════════════════════════

def _tab_utilization(tstats):
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    for tn, s in tstats.items():
        wp = (s["used_wt"]  / s["max_wt"])  * 100 if s["max_wt"]  > 0 else 0
        vp = (s["used_vol"] / s["max_vol"]) * 100 if s["max_vol"] > 0 else 0
        wc = "#f87171" if wp>90 else "#fb923c" if wp>70 else "#4ade80"
        vc = "#f87171" if vp>90 else "#fb923c" if vp>70 else "#4ade80"
        warn = (
            f'<div style="margin-top:10px;font-size:0.75rem;color:#fca5a5;'
            f'padding:7px 12px;background:rgba(239,68,68,0.07);border-radius:7px;">'
            f'⚠️ {"중량 " if wp>90 else ""}{"부피 " if vp>90 else ""}90% 초과</div>'
        ) if wp>90 or vp>90 else ""

        body = (
            f'<div style="font-size:0.92rem;font-weight:700;color:{_TEXT};margin-bottom:16px;">{tn}</div>'
            + _bar_row("중량", f"{s['used_wt']:.0f}", f"{s['max_wt']:.0f}", "kg",  wp, wc)
            + _bar_row("부피", f"{s['used_vol']:.2f}", f"{s['max_vol']:.2f}", "CBM", vp, vc)
            + warn
        )
        st.markdown(_card(body), unsafe_allow_html=True)


# ════════════════════════════════════════════════
# 탄소 배출 탭
# ════════════════════════════════════════════════

def _tab_esg(tstats):
    st.markdown(
        f'<div style="font-size:0.75rem;color:{_MUTED};margin-bottom:14px;">'
        f'GLEC Framework 기준 Scope 3 배출량</div>',
        unsafe_allow_html=True,
    )
    total_co2 = sum(s["co2_kg"] for s in tstats.values())
    for tn, s in tstats.items():
        pct = s["co2_kg"] / total_co2 * 100 if total_co2 > 0 else 0
        body = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">'
            f'<span style="font-size:0.9rem;font-weight:700;color:{_TEXT};">{tn}</span>'
            f'<span style="font-size:0.72rem;color:{_DIM};">{s["dist"]:.1f} km · {s["fuel_liter"]:.1f} L</span>'
            f'</div>'
            f'<div style="display:flex;align-items:center;gap:14px;">'
            f'<div style="flex:1;height:8px;background:{_INNER_BG};border-radius:4px;">'
            f'<div style="width:{pct:.1f}%;height:100%;background:#22c55e;border-radius:4px;"></div></div>'
            f'<span style="font-size:1rem;font-weight:800;color:#4ade80;min-width:72px;text-align:right;">'
            f'{s["co2_kg"]:.1f} kg</span>'
            f'</div>'
            f'<div style="font-size:0.68rem;color:{_DIM};margin-top:6px;">'
            f'소나무 {s["co2_kg"]/6.6:.1f}그루 흡수량 상당</div>'
        )
        st.markdown(_card(body, "16px 22px"), unsafe_allow_html=True)

    st.markdown(
        f'<div style="text-align:right;font-size:0.8rem;color:{_MUTED};">'
        f'총 배출 <strong style="color:#4ade80;">{total_co2:.1f} kg CO₂</strong></div>',
        unsafe_allow_html=True,
    )


# ════════════════════════════════════════════════
# 상차 순서 탭
# ════════════════════════════════════════════════

def _tab_lifo(tstats):
    st.markdown(
        f'<div style="font-size:0.75rem;color:{_MUTED};margin-bottom:14px;">'
        f'마지막 하차지 화물을 가장 안쪽에 적재 (LIFO 원칙)</div>',
        unsafe_allow_html=True,
    )
    for tn, s in tstats.items():
        if not s["loads_detail"]:
            continue
        stops     = list(reversed(s["loads_detail"]))
        rows_html = ""
        for i, d in enumerate(stops):
            is_last  = (i == len(stops) - 1)
            is_first = (i == 0)
            nb  = "#1d4ed8" if is_first else "#1e293b"
            nc  = "#93c5fd" if is_first else "#475569"
            tc  = _TEXT    if is_first else "#94a3b8"
            sep = "" if is_last else f'border-bottom:1px solid {_INNER_BG};'
            badge = (
                f'<span style="font-size:0.65rem;color:#60a5fa;background:rgba(59,130,246,0.1);'
                f'padding:2px 7px;border-radius:4px;margin-left:8px;">먼저 상차</span>'
            ) if is_first else ""
            rows_html += (
                f'<div style="display:flex;align-items:center;gap:10px;'
                f'padding:9px 0;{sep}">'
                f'<div style="width:26px;height:26px;border-radius:50%;background:{nb};'
                f'color:{nc};font-size:0.68rem;font-weight:700;display:flex;'
                f'align-items:center;justify-content:center;flex-shrink:0;">{i+1}</div>'
                f'<span style="font-size:0.83rem;color:{tc};">{d["name"]}</span>'
                f'{badge}</div>'
            )
        body = (
            f'<div style="font-size:0.9rem;font-weight:700;color:{_TEXT};margin-bottom:6px;">{tn}</div>'
            + rows_html
        )
        st.markdown(_card(body), unsafe_allow_html=True)


# ════════════════════════════════════════════════
# 비용 명세 탭
# ════════════════════════════════════════════════

def _tab_cost(res, tstats):
    rows = []
    for tn, s in tstats.items():
        fin = compute_truck_financials(s, st.session_state.cfg_fuel_price,
                                       st.session_state.cfg_labor)
        rows.append({
            "트럭":   tn,
            "거점": s["stops"],
            "거리":   f"{s['dist']:.1f}km",
            "시간":   _ft(s["time"]),
            "고정비": _fw(s["cost"]),
            "통행료": _fw(s["toll_cost"]),
            "연료비": _fw(fin["fuel_cost"]),
            "인건비": _fw(fin["labor_cost"]),
            "소계":   _fw(fin["grand_total"]),
        })
    rows.append({
        "트럭":   "합계",
        "거점": sum(s["stops"] for s in tstats.values()),
        "거리":   f"{res['dist']:.1f}km",
        "시간":   _ft(sum(s["time"] for s in tstats.values())),
        "고정비": _fw(res["fixed_cost"]),
        "통행료": _fw(res["toll_cost"]),
        "연료비": _fw(res["fuel_cost"]),
        "인건비": _fw(res["labor"]),
        "소계":   _fw(res["total_cost"]),
    })

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        use_container_width=True,
        column_config={
            "트럭":   st.column_config.TextColumn("트럭",  width=130),
            "거점":   st.column_config.NumberColumn("거점", width=55),
            "소계":   st.column_config.TextColumn("소계",  width=120),
        },
    )

    # 비용 구성 비율 바
    total = res["total_cost"]
    if total > 0:
        items = [
            ("고정비", res["fixed_cost"], "#3b82f6"),
            ("연료비", res["fuel_cost"],  "#f59e0b"),
            ("인건비", res["labor"],      "#8b5cf6"),
            ("통행료", res["toll_cost"],  "#06b6d4"),
        ]
        seg    = "".join(
            f'<div style="flex:{v/total*100:.2f};background:{c};min-width:2px;"></div>'
            for _, v, c in items if v > 0
        )
        legend = "".join(
            f'<div style="display:flex;align-items:center;gap:5px;">'
            f'<div style="width:9px;height:9px;border-radius:50%;background:{c};"></div>'
            f'<span style="font-size:0.73rem;color:{_MUTED};">{l} {v/total*100:.1f}%</span></div>'
            for l, v, c in items
        )
        st.markdown(
            f'<div style="background:{_CARD_BG};border:1px solid {_CARD_BORDER};'
            f'border-radius:12px;padding:18px 22px;margin-top:6px;">'
            + _label("비용 구성") +
            f'<div style="display:flex;height:12px;border-radius:6px;overflow:hidden;'
            f'margin:10px 0 12px;">{seg}</div>'
            f'<div style="display:flex;gap:18px;flex-wrap:wrap;">{legend}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
