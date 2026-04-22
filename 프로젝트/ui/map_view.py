"""
ui/map_view.py — 기사 현장 뷰 + 관제 맵

개선 사항:
  - 모든 함수에 타입 힌팅·docstring 추가
  - _ISSUE_TYPES 상수 분리
  - _render_delay_alert: 경보 임계 분을 상수로 관리
  - render_map: 색상 배열 app.py _TRUCK_COLORS와 동일하게 통일
  - _parse_eta: 반환 타입 Optional[datetime] 명시
"""
from datetime import datetime, timedelta
from typing import Optional

import folium
from folium import plugins
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

# ── 상수 ─────────────────────────────────────────
_WARN_MARGIN_MIN  = 15   # 마감까지 15분 미만이면 경고
_DEADHEAD_MIN_KM  = 30   # 공차 복귀 30km 이상이면 낭비 경고
_MAP_COLORS       = [
    "#1E3A8A", "#DC2626", "#059669", "#D97706",
    "#7C3AED", "#0891B2", "#BE185D", "#92400E",
]
_ISSUE_TYPES = [
    "선택", "수취인 부재", "주소 불일치", "접근 불가",
    "화물 파손 의심", "기타",
]


# ── 헬퍼 ─────────────────────────────────────────

def _is_delivery_row(row: dict, hub_name: str) -> bool:
    """배송지 행 여부 판단 (허브·휴게시간 제외).

    Args:
        row:      운행지시서 행 dict
        hub_name: 허브 거점명

    Returns:
        True이면 실제 배송지 행
    """
    거점 = row.get("거점", "")
    메모 = row.get("메모", "")
    return (
        "휴게시간" not in 거점
        and "🚩" not in 거점
        and "🏁" not in 거점
        and 메모 not in ("허브 출발",)
        and (not hub_name or hub_name not in 거점)
    )


def _build_report_index(report: list[dict]) -> dict[tuple[str, str], str]:
    """(트럭 prefix, 거점명) → 트럭명 인덱스 생성 (지도 마커용).

    Args:
        report: 운행지시서 행 리스트

    Returns:
        {(prefix, 거점명): 트럭명} dict
    """
    idx: dict[tuple[str, str], str] = {}
    for row in report:
        truck = row.get("트럭", "")
        spot  = row.get("거점", "")
        if truck and spot:
            prefix = truck.split("(")[0]
            key    = (prefix, spot)
            if key not in idx:
                idx[key] = truck
    return idx


def _get_truck_list(report: list[dict]) -> list[str]:
    """운행지시서에서 차량 이름 목록 추출 (순서 유지, 중복 제거).

    Args:
        report: 운행지시서 행 리스트

    Returns:
        차량 이름 리스트
    """
    trucks: list[str] = []
    for row in report:
        t = row.get("트럭", "")
        if t and t not in trucks:
            trucks.append(t)
    return trucks


def _parse_eta(eta_str: str) -> Optional[datetime]:
    """'HH:MM' 또는 'HH:MM ⚠️지연' → 오늘 날짜 기준 datetime.

    Args:
        eta_str: 예상 도착 시각 문자열

    Returns:
        datetime 또는 None (파싱 실패)
    """
    try:
        clean = eta_str.replace(" ⚠️지연", "").strip()
        now   = datetime.now()
        return datetime.strptime(clean, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
    except Exception:
        return None


def _minutes_until(eta_dt: datetime) -> int:
    """현재 시각으로부터 eta_dt까지 분 수 반환 (음수 가능).

    Args:
        eta_dt: 목표 datetime

    Returns:
        남은 분 수
    """
    return int((eta_dt - datetime.now()).total_seconds() / 60)


# ── 기능 1: 실시간 지연 경보 ─────────────────────

def _render_delay_alert(my_rows: list[dict]) -> None:
    """현재 시각 기준 지연·임박 경보 렌더링.

    Args:
        my_rows: 현재 기사의 배송 행 리스트
    """
    now    = datetime.now()
    alerts = []

    for row in my_rows:
        eta_dt = _parse_eta(row.get("도착", ""))
        tw     = row.get("약속시간", "")
        if not eta_dt or not tw or tw in ("-", "종일"):
            continue
        try:
            te_str   = tw.split("~")[1].strip()
            deadline = datetime.strptime(te_str, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )
        except Exception:
            continue

        margin       = int((deadline - eta_dt).total_seconds() / 60)
        mins_to_eta  = _minutes_until(eta_dt)

        if "⚠️지연" in row.get("도착", ""):
            alerts.append(("danger", row.get("거점", ""), margin, mins_to_eta, tw))
        elif margin < _WARN_MARGIN_MIN:
            alerts.append(("warning", row.get("거점", ""), margin, mins_to_eta, tw))

    if not alerts:
        return

    st.markdown("### 🚨 지금 확인이 필요합니다")
    for level, spot, margin, mins_to_eta, tw in alerts:
        if level == "danger":
            st.error(
                f"**{spot}** — 약속 시간({tw}) 초과 예정입니다. "
                f"예상 초과: **{abs(margin)}분**"
            )
        else:
            st.warning(
                f"**{spot}** — 약속 시간({tw})까지 여유가 **{margin}분** 밖에 없습니다. "
                f"서두르세요."
            )
    st.markdown("---")


# ── 기능 2: 순서 변경 미리보기 ───────────────────

def _render_reorder_preview(my_rows: list[dict], cfg_start_time: str) -> None:
    """순서 변경 시 시간창 충족 여부 미리보기.

    Args:
        my_rows:        현재 배송 행 리스트
        cfg_start_time: 출발 시각 "HH:MM"
    """
    with st.expander("🔀 순서 바꾸면 어떻게 될까? (현장 판단 도구)"):
        st.caption("순서를 바꿔서 저장하면 각 거점의 예상 도착 시각이 어떻게 달라지는지 미리 확인합니다.")

        names = [r.get("거점", "") for r in my_rows]
        if len(names) < 2:
            st.info("배송지가 2개 이상이어야 순서 변경을 시뮬레이션할 수 있습니다.")
            return

        try:
            sh, sm = map(int, cfg_start_time.split(":"))
        except Exception:
            sh, sm = 9, 0
        base = datetime.now().replace(hour=sh, minute=sm, second=0, microsecond=0)
        cur  = base

        st.markdown("**현재 순서** (번호를 바꿔 새 순서를 입력하세요)")
        cols = st.columns(min(len(names), 5))
        new_order_input: list[tuple[int, int]] = []
        for i, name in enumerate(names):
            with cols[i % 5]:
                val = st.number_input(
                    f"#{i+1} 순서", min_value=1, max_value=len(names),
                    value=i + 1, step=1, key=f"reorder_{i}",
                    label_visibility="collapsed",
                )
                st.caption(name[:8] + ("..." if len(name) > 8 else ""))
                new_order_input.append((val, i))

        new_order_input.sort(key=lambda x: x[0])
        new_order = [idx for _, idx in new_order_input]

        sim_rows = []
        ok_all   = True
        for rank, orig_idx in enumerate(new_order):
            row      = my_rows[orig_idx]
            tw       = row.get("약속시간", "종일")
            orig_eta = _parse_eta(row.get("도착", ""))

            if orig_eta and rank > 0:
                prev_orig = _parse_eta(my_rows[new_order[rank - 1]].get("도착", ""))
                if prev_orig and orig_eta > prev_orig:
                    gap = (orig_eta - prev_orig).total_seconds() / 60
                    cur = cur + timedelta(minutes=gap)

            ok = True
            if tw and tw not in ("-", "종일"):
                try:
                    te_str   = tw.split("~")[1].strip()
                    deadline = datetime.strptime(te_str, "%H:%M").replace(
                        year=cur.year, month=cur.month, day=cur.day
                    )
                    if cur > deadline:
                        ok     = False
                        ok_all = False
                except Exception:
                    pass

            sim_rows.append({
                "순서":       rank + 1,
                "거점":       row.get("거점", ""),
                "예상 도착":  cur.strftime("%H:%M"),
                "약속 시간":  tw,
                "상태":       "✅ 정상" if ok else "⚠️ 지연",
            })

        st.dataframe(pd.DataFrame(sim_rows), hide_index=True, use_container_width=True)
        if ok_all:
            st.success("이 순서로 가면 모든 약속 시간을 지킬 수 있습니다.")
        else:
            st.error("이 순서로 가면 일부 약속 시간을 지키지 못합니다. 순서를 다시 조정해보세요.")


# ── 기능 3: 출발 전 적재 순서 안내 ──────────────

def _render_loading_guide(truck_name: str, res: dict) -> None:
    """LIFO 기반 출발 전 적재 순서 안내.

    Args:
        truck_name: 차량명
        res:        OptimizationResult dict
    """
    tstats = res.get("truck_stats", {})
    stat   = tstats.get(truck_name)
    if not stat or not stat.get("loads_detail"):
        return

    with st.expander("📦 출발 전 적재 순서 안내", expanded=True):
        st.caption(
            "마지막에 내릴 짐을 가장 안쪽(깊숙이)에 싣고, "
            "첫 번째로 내릴 짐을 가장 바깥쪽에 실으세요."
        )
        loads = stat["loads_detail"]
        total = len(loads)

        for order, item in enumerate(reversed(loads)):
            is_first = order == total - 1
            is_last  = order == 0

            if is_first:
                border, bg, tag = "2px solid #60a5fa", "#1a3a6e", "🔵 가장 바깥쪽 — 첫 번째 하차"
            elif is_last:
                border, bg, tag = "2px solid #475569", "#1e2d45", "⬛ 가장 안쪽 — 마지막 하차"
            else:
                position = total - order
                border, bg, tag = "1px solid #2d4060", "#1a2235", f"안에서 {position}번째"

            st.markdown(f"""
<div style="border:{border};border-radius:9px;padding:12px 16px;margin-bottom:8px;background:{bg};">
  <div style="font-weight:700;font-size:0.95rem;color:#f1f5f9;">{order+1}번째로 싣기 — {item['name']}</div>
  <div style="color:#94a3b8;font-size:0.84rem;margin-top:5px;">
    {tag} &nbsp;|&nbsp; {item['weight']}kg &nbsp;|&nbsp; 난이도: {item['diff']}
  </div>
</div>
""", unsafe_allow_html=True)

        st.info(
            f"총 {total}개 거점 · "
            f"총 중량 {sum(i['weight'] for i in loads):.0f}kg · "
            f"적재율 {(stat['used_wt'] / stat['max_wt'] * 100):.0f}%"
        )


# ── 메인 렌더러 ───────────────────────────────────

@st.fragment
def render_report(res: dict, hub_loc: dict) -> None:
    """기사 현장 뷰 + 운행지시서 렌더링 (3탭).

    Args:
        res:     OptimizationResult dict
        hub_loc: 허브 거점 위치 dict (name, lat, lon)
    """
    if not hub_loc:
        return

    done      = st.session_state.get("delivery_done", {})
    hub_name  = hub_loc["name"]
    valid_rows = [r for r in res["report"] if _is_delivery_row(r, hub_name)]
    total_s    = len(valid_rows)
    done_cnt   = sum(1 for v in done.values() if v)

    view_tab, loading_tab, table_tab = st.tabs([
        "📱 기사 현장 뷰", "📦 적재 순서", "📋 전체 운행지시서",
    ])

    # ════════════════════════════════════
    # 탭 1: 기사 현장 뷰
    # ════════════════════════════════════
    with view_tab:
        st.caption("기사가 현장에서 직접 확인하고 완료 처리하는 화면입니다.")

        if total_s > 0:
            st.progress(
                min(1.0, done_cnt / total_s),
                text=f"오늘 배송 {done_cnt}/{total_s}건 완료",
            )
        if total_s > 0 and done_cnt >= total_s and not st.session_state.get("_balloons_shown"):
            st.success("🎉 오늘 배송을 모두 완료했습니다!")
            st.balloons()
            st.session_state["_balloons_shown"] = True
        if done_cnt < total_s:
            st.session_state["_balloons_shown"] = False

        truck_list = _get_truck_list(res["report"])
        if len(truck_list) > 1:
            selected_truck = st.selectbox(
                "내 차량 선택", ["전체 보기"] + truck_list, key="driver_truck_sel",
            )
        else:
            selected_truck = truck_list[0] if truck_list else "전체 보기"

        my_rows = [
            r for r in res["report"]
            if _is_delivery_row(r, hub_name)
            and (selected_truck == "전체 보기" or r.get("트럭") == selected_truck)
        ]

        if not my_rows:
            st.info("배송지가 없습니다.")
        else:
            _render_delay_alert(my_rows)
            cfg_start = st.session_state.get("cfg_start_time", "09:00")
            _render_reorder_preview(my_rows, cfg_start)
            st.markdown("---")
            _render_delivery_cards(my_rows, done)

    # ════════════════════════════════════
    # 탭 2: 적재 순서
    # ════════════════════════════════════
    with loading_tab:
        if len(truck_list) > 1:
            sel_truck_load = st.selectbox("차량 선택", truck_list, key="loading_truck_sel")
        else:
            sel_truck_load = truck_list[0] if truck_list else ""
        if sel_truck_load:
            _render_loading_guide(sel_truck_load, res)
        else:
            st.info("배차된 차량이 없습니다.")

    # ════════════════════════════════════
    # 탭 3: 전체 운행지시서
    # ════════════════════════════════════
    with table_tab:
        if total_s > 0:
            st.progress(
                min(1.0, done_cnt / total_s),
                text=f"배송 진행률: {done_cnt}/{total_s} 완료",
            )

        df_report = pd.DataFrame([
            {**row, "완료": done.get(f"{row.get('트럭','')}-{row.get('거점','')}", False)}
            for row in res["report"]
        ])

        edited_r = st.data_editor(
            df_report,
            column_config={
                "트럭":     st.column_config.TextColumn("트럭",      disabled=True),
                "거점":     st.column_config.TextColumn("거점",      disabled=True),
                "도착":     st.column_config.TextColumn("예상 도착", disabled=True),
                "약속시간": st.column_config.TextColumn("약속 시간", disabled=True),
                "거리":     st.column_config.TextColumn("거리",      disabled=True),
                "잔여무게": st.column_config.TextColumn("잔여 무게", disabled=True),
                "잔여부피": st.column_config.TextColumn("잔여 부피", disabled=True),
                "메모":     st.column_config.TextColumn("📝 메모",   disabled=True),
                "완료":     st.column_config.CheckboxColumn("✅ 완료"),
            },
            hide_index=True, use_container_width=True, key="report_editor",
        )

        new_done_t = {
            f"{row.get('트럭','')}-{row.get('거점','')}": bool(row.get("완료", False))
            for _, row in edited_r.iterrows()
            if _is_delivery_row(row, hub_name)
        }
        if new_done_t != done:
            st.session_state.delivery_done = new_done_t
            try:
                st.rerun(scope="fragment")
            except Exception:
                st.rerun()

        now_s = datetime.now().strftime("%Y%m%d_%H%M")
        st.download_button(
            "⬇️ 운행지시서 다운로드 (CSV)",
            data=df_report.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"운행지시서_{now_s}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        # 현장 이슈 목록
        issues = st.session_state.get("field_issues", [])
        if issues:
            st.markdown("---")
            st.subheader("🚨 오늘 접수된 현장 이슈")
            st.table(pd.DataFrame(issues))
            st.download_button(
                "⬇️ 이슈 목록 다운로드",
                data=pd.DataFrame(issues).to_csv(index=False, encoding="utf-8-sig"),
                file_name=f"현장이슈_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                use_container_width=True,
            )


def _render_delivery_cards(my_rows: list[dict], done: dict) -> None:
    """배송 카드 목록 렌더링 (분리).

    Args:
        my_rows: 현재 기사의 배송 행 리스트
        done:    {ck: bool} 완료 상태 dict
    """
    new_done = dict(done)

    for i, row in enumerate(my_rows):
        ck      = f"{row.get('트럭','')}-{row.get('거점','')}"
        is_done = done.get(ck, False)
        is_late = "⚠️지연" in row.get("도착", "")
        eta_txt = row.get("도착", "").replace(" ⚠️지연", "")
        tw      = row.get("약속시간", "")
        dist_t  = row.get("거리", "")
        memo_t  = row.get("메모", "")

        margin_txt = ""
        eta_dt = _parse_eta(row.get("도착", ""))
        if eta_dt and tw and "~" in tw:
            try:
                te_str   = tw.split("~")[1].strip()
                deadline = datetime.strptime(te_str, "%H:%M").replace(
                    year=datetime.now().year,
                    month=datetime.now().month,
                    day=datetime.now().day,
                )
                margin = int((deadline - eta_dt).total_seconds() / 60)
                margin_txt = (f"(마감까지 +{margin}분 여유)"
                              if margin >= 0 else f"(마감 {abs(margin)}분 초과)")
            except Exception:
                pass

        if is_done:
            border, bg, badge, badge_color = "2px solid #059669", "#063a28", "✅ 완료",   "#34d399"
        elif is_late:
            border, bg, badge, badge_color = "2px solid #dc2626", "#3b0a0a", "⚠️ 지연",  "#f87171"
        else:
            border, bg, badge, badge_color = "2px solid #3b82f6", "#1a3a6e", f"#{i+1} 대기", "#60a5fa"

        st.markdown(f"""
<div style="border:{border};border-radius:12px;padding:16px 18px;
            margin-bottom:12px;background:{bg};">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
    <span style="font-size:1rem;font-weight:700;color:#f1f5f9;">{row.get('거점','')}</span>
    <span style="background:{badge_color}22;color:{badge_color};border:1px solid {badge_color}55;
                 border-radius:12px;padding:3px 11px;font-size:0.78rem;font-weight:700;">{badge}</span>
  </div>
  <div style="color:#94a3b8;font-size:0.875rem;line-height:1.8;">
    🕐 예상 도착: <b style="color:#cbd5e1;">{eta_txt}</b> &nbsp; 약속 시간: <b style="color:#cbd5e1;">{tw}</b>
    {"&nbsp;<span style='color:#f87171;font-weight:600;'>" + margin_txt + "</span>" if margin_txt else ""}<br>
    🛣️ {dist_t} &nbsp;|&nbsp; 📝 {memo_t}
  </div>
</div>
""", unsafe_allow_html=True)

        col_done, col_issue = st.columns([2, 1])
        with col_done:
            if not is_done:
                if st.button(f"✅ {row.get('거점','')} 배송 완료", key=f"done_{ck}",
                             use_container_width=True, type="primary"):
                    new_done[ck] = True
                    st.session_state.delivery_done = new_done
                    st.rerun(scope="fragment")
            else:
                if st.button("↩️ 완료 취소", key=f"undo_{ck}", use_container_width=True):
                    new_done[ck] = False
                    st.session_state.delivery_done = new_done
                    st.rerun(scope="fragment")

        with col_issue:
            with st.popover("🚨 이슈"):
                issue = st.selectbox("유형", _ISSUE_TYPES, key=f"issue_type_{ck}")
                issue_memo = st.text_input("상세", key=f"issue_memo_{ck}",
                                           placeholder="예: 경비실 대리 수령")
                if st.button("등록", key=f"issue_btn_{ck}", use_container_width=True):
                    if issue != "선택":
                        issues = st.session_state.get("field_issues", [])
                        issues.append({
                            "시각": datetime.now().strftime("%H:%M"),
                            "거점": row.get("거점", ""),
                            "유형": issue,
                            "내용": issue_memo,
                        })
                        st.session_state.field_issues = issues
                        st.success("등록 완료")


@st.fragment
def render_map(res: dict, hub_loc: dict) -> None:
    """Folium 기반 실시간 관제 맵 렌더링.

    Args:
        res:     OptimizationResult dict
        hub_loc: 허브 거점 위치 dict (name, lat, lon)
    """
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:700;color:#94a3b8;'
        'text-transform:uppercase;letter-spacing:0.1em;margin:0 0 8px 0;">🗺 실시간 관제 맵</p>',
        unsafe_allow_html=True,
    )
    if not hub_loc:
        return

    late_spots = {
        (row["트럭"], row["거점"])
        for row in res["report"]
        if "⚠️지연" in row.get("도착", "")
    }
    all_lats = [loc["lat"] for p in res["routes"] for loc in p]
    all_lons = [loc["lon"] for p in res["routes"] for loc in p]

    zoom = 11
    if all_lats:
        ms   = max(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons))
        zoom = (14 if ms < 0.05 else 13 if ms < 0.15 else 12 if ms < 0.5
                else 11 if ms < 1.0 else 10 if ms < 2.0 else 9 if ms < 4.0
                else 8 if ms < 6.0 else 7)

    m = folium.Map(
        location=[hub_loc["lat"], hub_loc["lon"]],
        zoom_start=zoom,
        tiles="cartodbpositron",
    )
    if all_lats:
        m.fit_bounds([[min(all_lats), min(all_lons)], [max(all_lats), max(all_lons)]])

    # 경로 폴리라인
    for pi in res["paths"]:
        if not pi["path"]:
            continue
        fb = pi.get("is_fallback", False)
        folium.PolyLine(
            pi["path"],
            color="#888888" if fb else pi["color"],
            weight=4 if fb else 5,
            opacity=0.4 if fb else 0.8,
            dash_array="8 6" if fb else None,
            tooltip="⚠️ 추정 경로" if fb else None,
        ).add_to(m)
        if not fb:
            plugins.AntPath(
                locations=pi["path"],
                color="white",
                pulse_color=pi["color"],
                delay=800,
                weight=4,
            ).add_to(m)

    done       = st.session_state.get("delivery_done", {})
    report_idx = _build_report_index(res["report"])
    hub_drawn  = False

    for vi, p in enumerate(res["routes"]):
        tc     = _MAP_COLORS[vi % len(_MAP_COLORS)]
        prefix = f"T{vi+1}"
        sn     = 0

        for loc in p:
            if loc["name"] == hub_loc["name"]:
                if not hub_drawn:
                    folium.Marker(
                        [loc["lat"], loc["lon"]],
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
                matched_truck = report_idx.get((prefix, loc["name"]), prefix)
                ck       = f"{matched_truck}-{loc['name']}"
                is_done  = done.get(ck, False)
                is_late  = (matched_truck, loc["name"]) in late_spots

                if is_done:   mc, bd, si = "#16a34a", "3px solid #bbf7d0", "✓"
                elif is_late: mc, bd, si = "#ea580c", "3px solid #fed7aa", "!"
                else:         mc, bd, si = tc,        "2px solid white",   ""

                lbl = f"{si} T{vi+1}-{sn}".strip()
                tt  = (f"{'✅ 완료' if is_done else ('⚠️ 지연' if is_late else '⏳ 대기')} | "
                       f"{matched_truck} · {sn}번째 | {loc['name']}")
                mm = loc.get("memo", "")
                if mm:
                    tt += f" | 📝 {mm}"

                folium.Marker(
                    [loc["lat"], loc["lon"]],
                    tooltip=tt,
                    icon=folium.DivIcon(html=(
                        f'<div style="background:{mc};border:{bd};border-radius:50%;'
                        f'color:white;font-weight:bold;width:36px;height:36px;'
                        f'line-height:32px;text-align:center;font-size:8px;">{lbl}</div>'
                    )),
                ).add_to(m)

    st_folium(m, width="100%", height=520, key="final_map")
