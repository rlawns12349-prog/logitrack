"""
dashboard.py — 최적화 결과 대시보드

색상 원칙:
  배경 3단계(_BG/_SURFACE/"var(--surface)") + 시맨틱 6색
  파랑(정보) / 초록(성공) / 앰버(경고) / 빨강(위험) / 보라(비용) / 청록(ESG) / 주황(경로)
"""
from __future__ import annotations
import logging
import pandas as pd
import streamlit as st
from solver import compute_truck_financials

logger = logging.getLogger("logitrack")

# 배경 3단계
# CSS 변수 참조 (프로젝트.py :root 와 동기화)
_SURFACE = "var(--surface)"
_LINE    = "var(--line)"
_T1      = "var(--text)"
_T2      = "var(--text2)"
_T3      = "var(--text3)"
_BLUE    = "var(--blue)"
_GREEN   = "var(--green)"
_AMBER   = "var(--amber)"
_RED     = "var(--red)"
_PURPLE  = "var(--purple)"
_TEAL    = "var(--teal)"
_ORANGE  = "var(--orange)"


def _fw(v: float) -> str: return f"₩{int(v):,}"
def _ft(t: float) -> str: return f"{int(t//60)}h {int(t%60)}m"

def _card(content: str, accent: str = "", pad: str = "20px 24px") -> str:
    top = f"border-top:3px solid {accent};" if accent else ""
    return (
        f'<div style="background:{_SURFACE};border:1px solid {_LINE};{top}'
        f'border-radius:14px;padding:{pad};margin-bottom:14px;">'
        f'{content}</div>'
    )

def _label(text: str) -> str:
    return (
        f'<div style="font-size:0.68rem;font-weight:700;color:{_T3};'
        f'letter-spacing:0.09em;text-transform:uppercase;margin-bottom:6px;">{text}</div>'
    )

def _val(text: str, color: str = _T1, size: str = "1.45rem") -> str:
    return (
        f'<div style="font-size:{size};font-weight:800;color:{color};'
        f'letter-spacing:-0.02em;line-height:1.1;">{text}</div>'
    )

def _sub(text: str, color: str = _T2) -> str:
    return f'<div style="font-size:0.75rem;color:{color};margin-top:4px;">{text}</div>'

def _pill(text: str, color: str) -> str:
    return (
        f'<span style="font-size:0.72rem;font-weight:700;color:{color};'
        f'background:{color}1a;border:1px solid {color}44;'
        f'padding:3px 10px;border-radius:20px;">{text}</span>'
    )

def _bar(label: str, used: str, total: str, unit: str, pct: float, color: str) -> str:
    return (
        f'<div style="margin-bottom:14px;">'
        f'<div style="display:flex;justify-content:space-between;margin-bottom:6px;">'
        f'<span style="font-size:0.77rem;color:{_T2};">{label}</span>'
        f'<span style="font-size:0.77rem;font-weight:700;color:{color};">'
        f'{used}/{total}{unit} ({pct:.0f}%)</span></div>'
        f'<div style="height:8px;background:var(--surface);border-radius:4px;overflow:hidden;">'
        f'<div style="height:100%;width:{min(pct,100):.1f}%;background:{color};'
        f'border-radius:4px;"></div></div></div>'
    )

def _ibox(lbl: str, val: str, color: str = _T1) -> str:
    return (
        f'<div style="background:var(--surface);border-radius:10px;padding:13px 15px;">'
        + _label(lbl) + _val(val, color, "0.92rem") +
        '</div>'
    )


def render_dashboard(res: dict) -> None:
    col_back, col_title = st.columns([1, 6])
    with col_back:
        if st.button("← 돌아가기", use_container_width=True):
            st.session_state.opt_result = None
            st.rerun()
    with col_title:
        st.markdown(
            f'<div style="padding:5px 0;font-size:1.2rem;font-weight:800;color:{_T1};">'
            f'📊 배차 최적화 결과</div>',
            unsafe_allow_html=True,
        )

    for item in res.get("unassigned_diagnosed", []):
        st.markdown(
            f'<div style="background:var(--red-bg);border:1px solid var(--red);'
            f'border-left:4px solid {_RED};border-radius:10px;'
            f'padding:12px 18px;margin-bottom:8px;display:flex;gap:10px;align-items:center;">'
            f'<span>🚨</span>'
            f'<div><b style="color:{_RED};font-size:0.82rem;">배차 불가: {item["name"]}</b>'
            f'<span style="font-size:0.78rem;color:{_T2};margin-left:8px;">— {item["reason"]}</span>'
            f'</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    sla         = res.get("sla", 100.0)
    eff         = res.get("efficiency", 0)
    late        = res.get("late_count", 0)
    tstats      = res.get("truck_stats", {})
    total_stops = sum(s["stops"] for s in tstats.values())

    sla_c = _GREEN if sla >= 95 else _AMBER if sla >= 80 else _RED
    eff_c = _GREEN if eff >= 5  else _TEAL  if eff >= 0  else _ORANGE

    # KPI 5칸 — 각각 다른 accent 색
    kpis = [
        ("💰 총 예상 비용", f"₩{int(res['total_cost']):,}", f"고정비 {_fw(res['fixed_cost'])}", _PURPLE),
        ("🎯 납기 준수율",  f"{sla:.0f}%", "목표달성 ✓" if sla >= 95 else f"{late}건 지연", sla_c),
        ("📍 거리 효율",    f"{eff:+.1f}%", "NN 대비 단축률", eff_c),
        ("🌱 탄소 배출",    f"{res.get('co2_total',0):.1f}kg", f"통행료 {_fw(int(res['toll_cost']))}", _TEAL),
        ("🚛 운영 차량",    f"{len(res['routes'])}대", f"총 {total_stops}개소", _BLUE),
    ]
    for col, (lbl, val, sub_txt, accent) in zip(st.columns(5), kpis):
        col.markdown(
            f'<div style="background:{_SURFACE};border:1px solid {_LINE};'
            f'border-top:3px solid {accent};border-radius:14px;'
            f'padding:18px 20px;min-height:110px;">'
            + _label(lbl) + _val(val, accent)
            + _sub(sub_txt, sla_c if "납기" in lbl and sla < 95 else _T2)
            + '</div>',
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

    fuel_price = st.session_state.cfg_fuel_price
    labor      = st.session_state.cfg_labor
    fins = {tn: compute_truck_financials(s, fuel_price, labor) for tn, s in tstats.items()}

    tab_sum, tab_util, tab_esg, tab_lifo, tab_cost = st.tabs([
        "🚛 운행 요약", "📦 적재율", "🌱 탄소 배출", "📋 상차 순서", "💰 비용 명세",
    ])
    with tab_sum:  _tab_summary(tstats, fins)
    with tab_util: _tab_utilization(tstats)
    with tab_esg:  _tab_esg(tstats)
    with tab_lifo: _tab_lifo(tstats)
    with tab_cost: _tab_cost(res, tstats, fins)


def _tab_summary(tstats: dict, fins: dict) -> None:
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    for tn, s in tstats.items():
        fin    = fins[tn]
        wt_pct = (s["used_wt"] / s["max_wt"] * 100) if s["max_wt"] > 0 else 0
        wc     = _RED if wt_pct > 90 else _AMBER if wt_pct > 70 else _GREEN
        th, tm = int(s["time"] // 60), int(s["time"] % 60)
        route  = " → ".join(s["route_names"]) or "배송지 없음"

        header = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">'
            f'<div>'
            f'<span style="font-size:1rem;font-weight:800;color:{_T1};">{tn}</span>'
            f'<span style="font-size:0.72rem;color:{_T2};margin-left:10px;">'
            f'{s["stops"]}개소 · {s["dist"]:.1f}km · {th}h {tm}m</span>'
            f'</div>'
            + _pill(f"적재 {wt_pct:.0f}%", wc) + '</div>'
        )
        route_box = (
            f'<div style="font-size:0.74rem;color:{_T2};padding:8px 13px;'
            f'background:var(--surface);border-radius:7px;border-left:3px solid {_ORANGE};'
            f'margin-bottom:14px;line-height:1.65;word-break:break-all;">{route}</div>'
        )
        metrics = (
            f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;">'
            + _ibox("변동비",    _fw(fin["total_variable"]), _PURPLE)
            + _ibox("대기 시간", f'{int(s["wait_time"])}분',
                    _AMBER if s["wait_time"] > 30 else _T2)
            + _ibox("연료 소비", f'{s["fuel_liter"]:.1f}L', _TEAL)
            + '</div>'
        )
        st.markdown(_card(header + route_box + metrics, accent=wc), unsafe_allow_html=True)


def _tab_utilization(tstats: dict) -> None:
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    for tn, s in tstats.items():
        wp = (s["used_wt"]  / s["max_wt"])  * 100 if s["max_wt"]  > 0 else 0
        vp = (s["used_vol"] / s["max_vol"]) * 100 if s["max_vol"] > 0 else 0
        wc = _RED if wp > 90 else _AMBER if wp > 70 else _GREEN
        vc = _RED if vp > 90 else _AMBER if vp > 70 else _GREEN
        warn = (
            f'<div style="margin-top:10px;font-size:0.75rem;color:{_RED};'
            f'padding:7px 12px;background:#1f0a0a;border-radius:7px;">'
            f'⚠️ {"중량 " if wp>90 else ""}{"부피 " if vp>90 else ""}90% 초과</div>'
        ) if wp > 90 or vp > 90 else ""
        body = (
            f'<div style="font-size:0.9rem;font-weight:700;color:{_T1};margin-bottom:14px;">{tn}</div>'
            + _bar("중량", f"{s['used_wt']:.0f}", f"{s['max_wt']:.0f}", "kg",  wp, wc)
            + _bar("부피", f"{s['used_vol']:.2f}", f"{s['max_vol']:.2f}", "CBM", vp, vc)
            + warn
        )
        st.markdown(_card(body), unsafe_allow_html=True)


def _tab_esg(tstats: dict) -> None:
    st.markdown(
        f'<div style="font-size:0.75rem;color:{_T2};margin-bottom:14px;">'
        f'GLEC Framework 기준 Scope 3 배출량</div>',
        unsafe_allow_html=True,
    )
    total_co2 = sum(s["co2_kg"] for s in tstats.values())
    for tn, s in tstats.items():
        pct = s["co2_kg"] / total_co2 * 100 if total_co2 > 0 else 0
        body = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
            f'<span style="font-size:0.9rem;font-weight:700;color:{_T1};">{tn}</span>'
            f'<span style="font-size:0.72rem;color:{_T3};">{s["dist"]:.1f}km · {s["fuel_liter"]:.1f}L</span>'
            f'</div>'
            f'<div style="display:flex;align-items:center;gap:12px;">'
            f'<div style="flex:1;height:8px;background:var(--surface);border-radius:4px;">'
            f'<div style="width:{pct:.1f}%;height:100%;background:{_TEAL};border-radius:4px;"></div></div>'
            f'<span style="font-size:1rem;font-weight:800;color:{_TEAL};min-width:70px;text-align:right;">'
            f'{s["co2_kg"]:.1f} kg</span>'
            f'</div>'
            f'<div style="font-size:0.68rem;color:{_T3};margin-top:5px;">'
            f'소나무 {s["co2_kg"]/6.6:.1f}그루 흡수량 상당</div>'
        )
        st.markdown(_card(body, accent=_TEAL, pad="16px 20px"), unsafe_allow_html=True)

    st.markdown(
        f'<div style="text-align:right;font-size:0.8rem;color:{_T2};">'
        f'총 배출 <strong style="color:{_TEAL};">{total_co2:.1f} kg CO₂</strong></div>',
        unsafe_allow_html=True,
    )


def _tab_lifo(tstats: dict) -> None:
    st.markdown(
        f'<div style="font-size:0.75rem;color:{_T2};margin-bottom:14px;">'
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
            nb   = _BLUE if is_first else (_T3 if is_last else "var(--surface)")
            tc   = _T1   if is_first else _T2
            sep  = "" if is_last else f'border-bottom:1px solid var(--surface);'
            badge = (
                f'<span style="font-size:0.65rem;color:{_BLUE};'
                f'background:{_BLUE}15;padding:2px 7px;border-radius:4px;margin-left:8px;">먼저 상차</span>'
            ) if is_first else ""
            rows_html += (
                f'<div style="display:flex;align-items:center;gap:10px;padding:9px 0;{sep}">'
                f'<div style="width:26px;height:26px;border-radius:50%;background:{nb};'
                f'color:#fff;font-size:0.68rem;font-weight:700;display:flex;'
                f'align-items:center;justify-content:center;flex-shrink:0;">{i+1}</div>'
                f'<span style="font-size:0.83rem;color:{tc};">{d["name"]}</span>'
                f'{badge}</div>'
            )
        body = (
            f'<div style="font-size:0.9rem;font-weight:700;color:{_T1};margin-bottom:6px;">{tn}</div>'
            + rows_html
        )
        st.markdown(_card(body), unsafe_allow_html=True)


def _tab_cost(res: dict, tstats: dict, fins: dict) -> None:
    rows = []
    for tn, s in tstats.items():
        fin = fins[tn]
        rows.append({
            "트럭":   tn,
            "거점":   s["stops"],
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
        "거점":   sum(s["stops"] for s in tstats.values()),
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
        pd.DataFrame(rows), hide_index=True, use_container_width=True,
        column_config={
            "트럭": st.column_config.TextColumn("트럭",  width=130),
            "거점": st.column_config.NumberColumn("거점", width=55),
            "소계": st.column_config.TextColumn("소계",  width=120),
        },
    )

    total = res["total_cost"]
    if total > 0:
        cost_items = [
            ("고정비", res["fixed_cost"], _PURPLE),
            ("연료비", res["fuel_cost"],  _AMBER),
            ("인건비", res["labor"],      _BLUE),
            ("통행료", res["toll_cost"],  _ORANGE),
        ]
        seg = "".join(
            f'<div style="flex:{v/total*100:.2f};background:{c};min-width:2px;"></div>'
            for _, v, c in cost_items if v > 0
        )
        legend = "".join(
            f'<div style="display:flex;align-items:center;gap:5px;">'
            f'<div style="width:10px;height:10px;border-radius:3px;background:{c};"></div>'
            f'<span style="font-size:0.73rem;color:{_T2};">{l} {v/total*100:.1f}%</span></div>'
            for l, v, c in cost_items
        )
        st.markdown(
            f'<div style="background:{_SURFACE};border:1px solid {_LINE};'
            f'border-radius:12px;padding:18px 22px;margin-top:6px;">'
            + _label("비용 구성 비율")
            + f'<div style="display:flex;height:14px;border-radius:7px;overflow:hidden;'
            f'margin:10px 0 14px;">{seg}</div>'
            f'<div style="display:flex;gap:18px;flex-wrap:wrap;">{legend}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
