"""
ui/map_view.py — 운행 지시서 + 실시간 관제 맵 (st.fragment)
  render_report(res, hub_loc)
  render_map(res, hub_loc)
"""
from datetime import datetime

import folium
from folium import plugins
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium


def _is_delivery_row(row: dict, hub_name: str) -> bool:
    """허브 출발/복귀/휴게 행을 제외하고 실제 배송 행만 True
    M-2: hub_name이 None일 때 TypeError 방지
    """
    거점 = row.get('거점', '')
    메모  = row.get('메모', '')
    return (
        "휴게시간" not in 거점
        and "🚩" not in 거점
        and "🏁" not in 거점
        and 메모 not in ('허브 출발',)
        and (not hub_name or hub_name not in 거점)  # M-2: None 가드
    )


def _build_report_index(report: list) -> dict:
    """M-1: (트럭접두어, 거점명) → 트럭라벨 딕셔너리를 미리 구성해 O(1) 조회"""
    idx: dict[tuple, str] = {}
    for row in report:
        truck = row.get('트럭', '')
        spot  = row.get('거점', '')
        if truck and spot:
            # 트럭 접두어(예: "T1")와 거점명으로 키 구성
            prefix = truck.split('(')[0]  # "T1(1톤...)" → "T1"
            key = (prefix, spot)
            if key not in idx:
                idx[key] = truck
    return idx


@st.fragment
def render_report(res: dict, hub_loc: dict):
    st.subheader("📝 운행 지시서")
    if not hub_loc:
        return

    done      = st.session_state.get('delivery_done', {})
    hub_name  = hub_loc['name']
    valid_rows = [r for r in res['report'] if _is_delivery_row(r, hub_name)]
    total_s   = len(valid_rows)
    done_cnt  = sum(1 for v in done.values() if v)

    if total_s > 0:
        st.progress(
            min(1.0, done_cnt / total_s),
            text=f"배송 진행률: {done_cnt}/{total_s} 완료",
        )
    if total_s > 0 and done_cnt >= total_s and not st.session_state.get('_balloons_shown'):
        st.success("🎉 모든 배송 완료!")
        st.balloons()
        st.session_state['_balloons_shown'] = True
    if done_cnt < total_s:
        st.session_state['_balloons_shown'] = False

    df_report = pd.DataFrame([
        {**row, "완료": done.get(f"{row.get('트럭','')}-{row.get('거점','')}", False)}
        for row in res['report']
    ])

    edited_r = st.data_editor(
        df_report,
        column_config={
            "트럭":     st.column_config.TextColumn("트럭",     disabled=True),
            "거점":     st.column_config.TextColumn("거점",     disabled=True),
            "도착":     st.column_config.TextColumn("도착",     disabled=True),
            "약속시간": st.column_config.TextColumn("약속시간", disabled=True),
            "거리":     st.column_config.TextColumn("거리",     disabled=True),
            "잔여무게": st.column_config.TextColumn("잔여무게", disabled=True),
            "잔여부피": st.column_config.TextColumn("잔여부피", disabled=True),
            "메모":     st.column_config.TextColumn("📝 메모",  disabled=True),
            "완료":     st.column_config.CheckboxColumn("✅ 완료"),
        },
        hide_index=True,
        use_container_width=True,
        key="report_editor",
    )

    new_done = {
        f"{row.get('트럭','')}-{row.get('거점','')}": bool(row.get('완료', False))
        for _, row in edited_r.iterrows()
        if _is_delivery_row(row, hub_name)
    }
    if new_done != done:
        st.session_state.delivery_done = new_done
        try:
            st.rerun(scope="fragment")
        except Exception:
            st.rerun()

    now_s = datetime.now().strftime("%Y%m%d_%H%M")
    st.download_button(
        "⬇️ CSV 다운로드",
        data=df_report.to_csv(index=False, encoding='utf-8-sig'),
        file_name=f"운행지시서_V18_{now_s}.csv",
        mime="text/csv",
        use_container_width=True,
    )


@st.fragment
def render_map(res: dict, hub_loc: dict):
    st.subheader("🗺️ 실시간 관제 맵")
    if not hub_loc:
        return

    late_spots = {
        (row['트럭'], row['거점'])
        for row in res['report']
        if '⚠️지연' in row.get('도착', '')
    }
    all_lats = [loc['lat'] for p in res['routes'] for loc in p]
    all_lons = [loc['lon'] for p in res['routes'] for loc in p]

    zoom = 11
    if all_lats:
        ms   = max(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons))
        zoom = (14 if ms < 0.05 else 13 if ms < 0.15 else 12 if ms < 0.5 else
                11 if ms < 1.0 else 10 if ms < 2.0 else 9 if ms < 4.0 else
                8  if ms < 6.0 else 7)

    m = folium.Map(
        location=[hub_loc['lat'], hub_loc['lon']],
        zoom_start=zoom,
        tiles="cartodbpositron",
    )
    if all_lats:
        m.fit_bounds([[min(all_lats), min(all_lons)], [max(all_lats), max(all_lons)]])

    # 경로 폴리라인
    for pi in res['paths']:
        if not pi['path']:
            continue
        fb = pi.get('is_fallback', False)
        folium.PolyLine(
            pi['path'],
            color='#888888' if fb else pi['color'],
            weight=4 if fb else 5,
            opacity=0.4 if fb else 0.8,
            dash_array='8 6' if fb else None,
            tooltip='⚠️ 맨해튼 추정' if fb else None,
        ).add_to(m)
        if not fb:
            plugins.AntPath(
                locations=pi['path'],
                color="white",
                pulse_color=pi['color'],
                delay=800,
                weight=4,
            ).add_to(m)

    colors     = ['#1E3A8A', '#DC2626', '#059669', '#D97706', '#7C3AED', '#0891B2', '#BE185D']
    hub_drawn  = False
    done       = st.session_state.get('delivery_done', {})

    # M-1: 리포트를 미리 인덱싱해 마커 매칭을 O(n*m) → O(1)로 개선
    report_idx = _build_report_index(res['report'])

    for vi, p in enumerate(res['routes']):
        tc     = colors[vi % len(colors)]
        prefix = f"T{vi+1}"
        sn     = 0
        for loc in p:
            if loc['name'] == hub_loc['name']:
                if not hub_drawn:
                    folium.Marker(
                        [loc['lat'], loc['lon']],
                        tooltip=f"🚩 허브: {loc['name']}",
                        icon=folium.DivIcon(html=(
                            '<div style="background:black;border:2px solid white;'
                            'border-radius:50%;color:white;font-weight:bold;'
                            'width:30px;height:30px;line-height:26px;'
                            'text-align:center;font-size:11px;">H</div>'
                        )),
                    ).add_to(m)
                    hub_drawn = True
            else:
                sn += 1
                # M-1: 딕셔너리 O(1) 조회 (기존 O(n) 순차 탐색 제거)
                matched_truck = report_idx.get((prefix, loc['name']), prefix)

                ck       = f"{matched_truck}-{loc['name']}"
                is_done  = done.get(ck, False)
                is_late  = (matched_truck, loc['name']) in late_spots

                if is_done:   mc, bd, si = "#16a34a", "3px solid #bbf7d0", "✓"
                elif is_late: mc, bd, si = "#ea580c", "3px solid #fed7aa", "!"
                else:         mc, bd, si = tc,        "2px solid white",   ""

                lbl = f"{si} T{vi+1}-{sn}".strip()
                tt  = (f"{'✅' if is_done else ('⚠️' if is_late else '⏳')} | "
                       f"{matched_truck} · {sn}번째 | {loc['name']}")
                mm  = loc.get('memo', '')
                if mm:
                    tt += f" | 📝 {mm}"

                folium.Marker(
                    [loc['lat'], loc['lon']],
                    tooltip=tt,
                    icon=folium.DivIcon(html=(
                        f'<div style="background:{mc};border:{bd};border-radius:50%;'
                        f'color:white;font-weight:bold;width:36px;height:36px;'
                        f'line-height:32px;text-align:center;font-size:8px;">{lbl}</div>'
                    )),
                ).add_to(m)

    st_folium(m, width="100%", height=520, key="final_map")
