"""
app.py — LogiTrack Pro V18 진입점

보완 사항:
  P-1  run_optimization 정의 전 호출 → 함수 정의를 진입점(if/else) 보다 먼저 배치
  P-2  매 리로드마다 DB 재조회 → 세션이 비어있을 때만 load_locations() 호출
  P-5  cont_min float 누적 오차 → int(cont_min) 비교
"""
import sys
import os

# app.py가 있는 폴더를 Python 경로에 추가 (ui, db 등 같은 폴더 모듈 인식)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import logging
from datetime import datetime, timedelta

import nest_asyncio
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from db import DBManager
from geo import LRUCache, DIESEL_EMISSION_FACTOR, get_dynamic_fuel_consumption
from routing import build_real_time_matrix
from solver import solve_vrptw, calc_nn_distance_real, diagnose_unassigned

# ui/ 폴더가 있으면 그 안에서, 없으면 같은 폴더에서 임포트
try:
    from ui.sidebar import render_sidebar
    from ui.dashboard import render_dashboard
    from ui.map_view import render_report, render_map
except ModuleNotFoundError:
    from sidebar import render_sidebar
    from dashboard import render_dashboard
    from map_view import render_report, render_map

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("logitrack")
nest_asyncio.apply()

st.set_page_config(page_title="LogiTrack Pro V18", layout="wide", page_icon="🚚")

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=_env_path, override=True)


@st.cache_resource
def _get_kakao_key():
    try:    return st.secrets["KAKAO_API_KEY"]
    except Exception: return os.getenv("KAKAO_API_KEY")


@st.cache_resource
def _get_db_url():
    try:    return st.secrets["SUPABASE_DB_URL"]
    except Exception: return os.getenv("SUPABASE_DB_URL")


KAKAO_API_KEY   = _get_kakao_key()
SUPABASE_DB_URL = _get_db_url()

if not SUPABASE_DB_URL or not KAKAO_API_KEY:
    st.error("🚨 API 키 또는 DB URL 없음. .env 또는 Streamlit Secrets를 확인하세요.")
    st.stop()


@st.cache_resource
def _get_api_cache():
    return LRUCache(maxsize=500)


@st.cache_resource
def _get_db():
    return DBManager(SUPABASE_DB_URL)


@st.cache_resource
def _run_startup_purge():
    try:
        _get_db().purge_old_route_cache(ttl_hours=12)
        logger.warning("Startup purge completed.")
    except Exception as e:
        logger.warning("Startup purge failed: %s", e)
    return True


db        = _get_db()
api_cache = _get_api_cache()
_run_startup_purge()


def init_session():
    defaults = {
        'db_data': [], 'targets': [], 'opt_result': None, 'start_node': "",
        'delivery_done': {}, 'cfg_1t_cnt': 2, 'cfg_2t_cnt': 1, 'cfg_5t_cnt': 0,
        'cfg_speed': 45, 'cfg_service': 10, 'cfg_service_sec_per_kg': 2,
        'cfg_fuel_price': 1500, 'cfg_labor': 15000, 'cfg_max_hours': 10,
        'cfg_balance': False, 'cfg_vrptw_sec': 5,
        'cfg_congestion': 40, 'cfg_start_time': "09:00", 'cfg_weather': "맑음",
        'cfg_v1_skills': [], 'cfg_v2_skills': [], 'cfg_v5_skills': [],
        '_last_upload_id': "", '_balloons_shown': False, '_opt_in_progress': False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_session()
# P-2 fix: 세션이 비어있을 때만 DB 조회
if not st.session_state.db_data:
    st.session_state.db_data = db.load_locations()

st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size:1.8rem; color:#4dabf7; font-weight:bold; }
.stButton>button { border-radius:6px; font-weight:bold; }
th { background-color:#f8f9fa; text-align:center !important; }
div[data-testid="stToast"] { border-left:5px solid #4dabf7; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════
# P-1 fix: 모든 함수를 진입점(if/else) 보다 먼저 정의
# ══════════════════════════════════════════════

def run_optimization(hub_name: str):
    try:
        with st.status("🗺️ V18: 실측 매트릭스 구축 중...", expanded=True) as status:

            hub_candidates = [l for l in st.session_state.db_data if l['name'] == hub_name]
            if not hub_candidates:
                status.update(label="❌ 허브를 찾을 수 없습니다.", state="error")
                st.error(f"❌ 허브 '{hub_name}'이 DB에 없습니다.")
                st.session_state._opt_in_progress = False
                st.stop()
            hub = hub_candidates[0]

            nodes_data = [{
                **hub,
                "weight": 0, "volume": 0, "temperature": "상온",
                "unload_method": "지게차", "difficulty": "일반 (+0분)",
                "priority": "일반", "tw_type": "Hard", "tw_start": 0, "tw_end": 1000,
                "_node_uid": "hub_0",
            }] + [
                {**t, "_node_uid": f"target_{i}"}
                for i, t in enumerate(st.session_state.targets)
            ]
            node_idx_map: dict[str, int] = {
                n['_node_uid']: i for i, n in enumerate(nodes_data)
            }

            weather_f = (0.7 if "눈" in st.session_state.cfg_weather
                         else 0.8 if "비" in st.session_state.cfg_weather else 1.0)

            loop = asyncio.get_event_loop()
            combined, travel_tm, svc_list, dist_m, toll_m = loop.run_until_complete(
                build_real_time_matrix(
                    nodes_data,
                    st.session_state.cfg_speed,
                    st.session_state.cfg_congestion,
                    weather_f,
                    st.session_state.cfg_service,
                    st.session_state.cfg_service_sec_per_kg,
                    kakao_key=KAKAO_API_KEY,
                    api_cache=api_cache,
                    db=db,
                )
            )

            v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps = [], [], [], [], [], []
            v1s = st.session_state.get('cfg_v1_skills', [])
            v2s = st.session_state.get('cfg_v2_skills', [])
            v5s = st.session_state.get('cfg_v5_skills', [])

            for i in range(st.session_state.cfg_1t_cnt):
                v_wts.append(1000); v_vols.append(5.0); v_costs.append(100000)
                v_tcaps.append(["상온", "냉장", "냉동"])
                v_names.append(f"1톤(냉탑)/#{i+1}")
                v_skills.append(v1s[i] if i < len(v1s) else 1.0)
            for i in range(st.session_state.cfg_2t_cnt):
                v_wts.append(2500); v_vols.append(12.0); v_costs.append(180000)
                v_tcaps.append(["상온"])
                v_names.append(f"2.5톤(일반)/#{i+1}")
                v_skills.append(v2s[i] if i < len(v2s) else 1.0)
            for i in range(st.session_state.cfg_5t_cnt):
                v_wts.append(5000); v_vols.append(25.0); v_costs.append(280000)
                v_tcaps.append(["상온"])
                v_names.append(f"5톤(일반)/#{i+1}")
                v_skills.append(v5s[i] if i < len(v5s) else 1.0)

            mwm = st.session_state.cfg_max_hours * 60
            st.write("⚙️ OR-Tools VRPTW 솔버 실행 중...")
            plans, diag, unassigned, used_vi = solve_vrptw(
                nodes_data, v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps,
                combined, st.session_state.cfg_balance, mwm, st.session_state.cfg_vrptw_sec,
            )

            if not plans and not unassigned:
                status.update(label="❌ 최적화 실패", state="error")
                st.error("❌ " + "\n".join(diag))
                st.session_state._opt_in_progress = False
                st.stop()

            report, all_paths = [], []
            dist_tot = time_tot = fuel_tot = co2_tot = toll_tot = wait_tot = 0.0
            tstats   = {}
            colors   = ['#1E3A8A', '#DC2626', '#059669', '#D97706',
                        '#7C3AED', '#0891B2', '#BE185D']
            base_t   = datetime.strptime(st.session_state.cfg_start_time, "%H:%M")
            late_cnt = total_stops = 0

            for vi, p in enumerate(plans):
                col    = colors[vi % len(colors)]
                vtype  = used_vi[vi]['name']
                vsk    = used_vi[vi]['skill']
                vtm    = 1.15 if "2.5톤" in vtype else (1.3 if "5톤" in vtype else 1.0)
                vlabel = f"T{vi+1}({vtype})"

                delivery_nodes = [n for n in p if n['name'] != hub['name']]
                load_w = sum(n.get('weight', 0) for n in delivery_nodes)
                load_v = sum(n.get('volume', 0) for n in delivery_nodes)

                tstats[vlabel] = {
                    "dist": 0.0, "time": 0.0, "fuel_liter": 0.0, "co2_kg": 0.0,
                    "toll_cost": 0, "wait_time": 0, "stops": 0,
                    "route_names": [], "loads_detail": [],
                    "cost":    used_vi[vi]['cost'],
                    "used_wt": load_w, "max_wt": used_vi[vi]['max_weight'],
                    "used_vol": load_v, "max_vol": used_vi[vi]['max_volume'],
                }
                cont_min = 0.0
                curr_eta = base_t

                report.append({
                    "트럭": vlabel, "거점": f"🚩 {hub['name']} (출발)",
                    "도착": base_t.strftime("%H:%M"), "약속시간": "-",
                    "거리": "-", "잔여무게": f"{load_w:.1f}kg",
                    "잔여부피": f"{load_v:.1f}CBM", "메모": "허브 출발",
                })

                for i in range(len(p) - 1):
                    fi = node_idx_map.get(p[i].get('_node_uid', ''), 0)
                    ti = node_idx_map.get(p[i + 1].get('_node_uid', ''), 0)

                    dseg   = dist_m[fi][ti]
                    tseg   = toll_m[fi][ti]
                    trseg  = travel_tm[fi][ti] * vsk * vtm
                    is_ret = p[i + 1]['name'] == hub['name']

                    ck     = (f"DIR_{p[i]['lat']:.4f},{p[i]['lon']:.4f}"
                              f"_{p[i+1]['lat']:.4f},{p[i+1]['lon']:.4f}")
                    cd     = api_cache.get(ck)
                    path_d = cd['path'] if cd else [[p[i]['lat'], p[i]['lon']],
                                                    [p[i+1]['lat'], p[i+1]['lon']]]
                    is_fb  = cd.get('is_fallback', True) if cd else True

                    liter   = get_dynamic_fuel_consumption(vtype, load_w, dseg, is_ret)
                    seg_co2 = liter * DIESEL_EMISSION_FACTOR

                    fuel_tot += liter * st.session_state.cfg_fuel_price
                    co2_tot  += seg_co2
                    dist_tot += dseg
                    toll_tot += tseg
                    tstats[vlabel]["dist"]       += dseg
                    tstats[vlabel]["fuel_liter"] += liter
                    tstats[vlabel]["co2_kg"]     += seg_co2
                    tstats[vlabel]["toll_cost"]  += tseg

                    all_paths.append({"path": path_d, "color": col, "is_fallback": is_fb})

                    cont_min += trseg
                    curr_eta += timedelta(minutes=trseg)
                    time_tot += trseg
                    tstats[vlabel]["time"] += trseg

                    elapsed  = (curr_eta - base_t).total_seconds() / 60.0
                    tw_s_min = p[i + 1].get('tw_start', 0)
                    if elapsed < tw_s_min and not is_ret:
                        wait      = tw_s_min - elapsed
                        curr_eta += timedelta(minutes=wait)
                        time_tot += wait
                        tstats[vlabel]["time"]      += wait
                        tstats[vlabel]["wait_time"] += wait
                        wait_tot += wait

                    # P-5 fix: int 변환 후 비교로 float 오차 방지
                    if int(cont_min) >= 240 and not is_ret:
                        curr_eta += timedelta(minutes=30)
                        cont_min  = 0.0
                        time_tot += 30
                        tstats[vlabel]["time"] += 30
                        report.append({
                            "트럭": vlabel, "거점": "☕ 법정 휴게시간",
                            "도착": curr_eta.strftime("%H:%M"), "약속시간": "-",
                            "거리": "-", "잔여무게": f"{load_w:.1f}kg",
                            "잔여부피": f"{load_v:.1f}CBM",
                            "메모": "⚠️ 4시간 연속 — 30분 의무 휴식",
                        })

                    if not is_ret:
                        svc       = svc_list[ti]
                        curr_eta += timedelta(minutes=svc)
                        time_tot += svc
                        tstats[vlabel]["time"] += svc

                    elapsed2 = (curr_eta - base_t).total_seconds() / 60.0
                    is_late  = elapsed2 > p[i + 1].get('tw_end', 1000)

                    if is_ret:
                        report.append({
                            "트럭": vlabel, "거점": f"🏁 {hub['name']} (복귀)",
                            "도착": curr_eta.strftime("%H:%M"), "약속시간": "-",
                            "거리": f"{dseg:.1f}km" + (" ⚠️" if is_fb else ""),
                            "잔여무게": "0kg", "잔여부피": "0CBM",
                            "메모": f"허브 복귀 (통행료 ₩{int(tseg):,})",
                        })
                    else:
                        load_w = max(0.0, load_w - p[i + 1].get('weight', 0))
                        load_v = max(0.0, load_v - p[i + 1].get('volume', 0))
                        tstats[vlabel]["stops"] += 1
                        tstats[vlabel]["route_names"].append(p[i + 1]['name'])
                        tstats[vlabel]["loads_detail"].append({
                            "name":   p[i + 1]['name'],
                            "weight": p[i + 1].get('weight', 0),
                            "volume": p[i + 1].get('volume', 0),
                            "diff":   p[i + 1].get('difficulty', '일반').split(' ')[0],
                        })
                        total_stops += 1
                        if is_late:
                            late_cnt += 1
                        dl = p[i + 1].get('difficulty', '일반').split(' ')[0]
                        tl = p[i + 1].get('temperature', '상온')
                        ul = p[i + 1].get('unload_method', '수작업')
                        report.append({
                            "트럭":     vlabel,
                            "거점":     p[i + 1]['name'],
                            "도착":     (f"{curr_eta.strftime('%H:%M')} ⚠️지연"
                                        if is_late else curr_eta.strftime("%H:%M")),
                            "약속시간": p[i + 1].get('tw_disp', '종일'),
                            "거리":     f"{dseg:.1f}km" + (" ⚠️" if is_fb else ""),
                            "잔여무게": f"{load_w:.1f}kg",
                            "잔여부피": f"{load_v:.1f}CBM",
                            "메모":     (f"[{tl}|{dl}|{ul}] 통행료 ₩{int(tseg):,} | "
                                        f"{p[i+1].get('memo', '')}"),
                        })

            nn_d  = calc_nn_distance_real(dist_m, nodes_data)
            eff   = round((1 - dist_tot / nn_d) * 100, 1) if nn_d > 0 else 0
            sla   = round(((total_stops - late_cnt) / total_stops) * 100, 1) if total_stops > 0 else 100.0
            fixed = sum(i['cost'] for i in used_vi)
            unassigned_diag = [
                {"name": n['name'],
                 "reason": diagnose_unassigned(n, v_tcaps, v_wts, v_vols)}
                for n in unassigned
            ]

            st.session_state.opt_result = {
                "report":               report,
                "dist":                 dist_tot,
                "fuel_cost":            fuel_tot,
                "toll_cost":            toll_tot,
                "co2_total":            co2_tot,
                "labor":                (time_tot / 60) * st.session_state.cfg_labor,
                "fixed_cost":           fixed,
                "total_cost":           (fuel_tot + toll_tot
                                         + (time_tot / 60) * st.session_state.cfg_labor
                                         + fixed),
                "truck_stats":          tstats,
                "paths":                all_paths,
                "routes":               plans,
                "efficiency":           eff,
                "nn_real_dist":         nn_d,
                "hub_name":             hub['name'],
                "hub_loc":              hub,
                "unassigned":           unassigned,
                "unassigned_diagnosed": unassigned_diag,
                "sla":                  sla,
                "late_count":           late_cnt,
                "wait_time_total":      wait_tot,
            }
            status.update(label="✅ V18 최적화 완료!", state="complete")
    finally:
        st.session_state._opt_in_progress = False
    st.rerun()


def _render_queue_page():
    hub_name = st.session_state.get('start_node', '')
    col_btn, _ = st.columns([4, 1])
    with col_btn:
        if hub_name and st.session_state.targets:
            if st.session_state.get('_opt_in_progress'):
                st.info("⏳ 최적화 진행 중입니다...")
            elif st.button("🚀 V18 실측 최적화 시작", type="primary", use_container_width=True):
                st.session_state._run_opt        = True
                st.session_state._opt_in_progress = True
        elif not hub_name:
            st.warning("🚩 허브가 지정되지 않았습니다.")

    if len(st.session_state.db_data) > 1:
        with st.expander("ℹ️ 멀티 허브 안내"):
            st.info("현재 V18은 단일 허브 기준입니다.")

    st.subheader("📋 배차 대기열")
    if st.session_state.targets:
        sh, sm = map(int, st.session_state.cfg_start_time.split(':'))
        so = sh * 60 + sm

        _base_tw_opts = ["09:00~18:00", "09:00~13:00", "13:00~18:00", "00:00~23:59", "07:00~15:00"]
        _existing     = list({t['tw_disp'] for t in st.session_state.targets
                               if t['tw_disp'] not in _base_tw_opts})
        tw_opts = _base_tw_opts + _existing

        df_edit = pd.DataFrame([{
            "거점": t['name'], "약속시간": t['tw_disp'],
            "제약유형": t.get('tw_type', 'Hard'), "우선순위": t.get('priority', '일반'),
            "온도": t.get('temperature', '상온'), "무게(kg)": t['weight'],
            "부피(CBM)": t['volume'], "하차방식": t.get('unload_method', '수작업'),
            "난이도": t.get('difficulty', '일반 (+0분)'), "메모": t.get('memo', ''),
            "삭제": False,
        } for t in st.session_state.targets])

        edited = st.data_editor(df_edit, column_config={
            "거점":      st.column_config.TextColumn("거점",     disabled=True),
            "약속시간":  st.column_config.SelectboxColumn("시간약속", options=tw_opts),
            "제약유형":  st.column_config.SelectboxColumn("제약유형", options=["Hard", "Soft"]),
            "우선순위":  st.column_config.SelectboxColumn("우선순위", options=["VIP", "일반", "여유"]),
            "온도":      st.column_config.SelectboxColumn("온도", options=["상온", "냉장", "냉동"]),
            "무게(kg)":  st.column_config.NumberColumn("무게(kg)", min_value=0.1),
            "부피(CBM)": st.column_config.NumberColumn("부피(CBM)", min_value=0.01),
            "하차방식":  st.column_config.SelectboxColumn("하차방식", options=["수작업", "지게차"]),
            "난이도":    st.column_config.SelectboxColumn("난이도", options=[
                "일반 (+0분)", "보안아파트 (+10분)", "재래시장 (+15분)"]),
            "삭제":      st.column_config.CheckboxColumn("삭제"),
        }, hide_index=True, use_container_width=True)

        if st.button("✅ 변경사항 반영", use_container_width=True):
            new_tgts = []
            for i, row in edited.iterrows():
                if row["삭제"]:
                    continue
                t = st.session_state.targets[i].copy()
                t['weight']        = float(row["무게(kg)"])
                t['volume']        = float(row["부피(CBM)"])
                t['difficulty']    = str(row.get("난이도", "일반 (+0분)")).strip()
                t['temperature']   = str(row.get("온도", "상온")).strip()
                t['unload_method'] = str(row.get("하차방식", "수작업")).strip()
                t['priority']      = str(row.get("우선순위", "일반")).strip()
                t['tw_type']       = str(row.get("제약유형", "Hard")).strip()
                t['memo']          = str(row.get("메모", "")).strip()
                twd = str(row["약속시간"]).strip()
                try:
                    ts, te = twd.split('~')
                    t['tw_start'] = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
                    t['tw_end']   = max(t['tw_start'] + 1, int(te[:2]) * 60 + int(te[3:]) - so)
                    t['tw_disp']  = twd
                except (ValueError, IndexError):
                    pass
                new_tgts.append(t)
            st.session_state.targets    = new_tgts
            st.session_state.opt_result = None
            st.rerun()

    if st.session_state.pop("_run_opt", False):
        run_optimization(hub_name)


def _render_result_page():
    res = st.session_state.opt_result
    render_dashboard(res)
    st.divider()

    hub_loc = res.get('hub_loc') or next(
        (l for l in st.session_state.db_data if l['name'] == st.session_state.start_node),
        st.session_state.db_data[0] if st.session_state.db_data else None,
    )
    col1, col2 = st.columns([1.2, 2])
    with col1:
        render_report(res, hub_loc)
    with col2:
        render_map(res, hub_loc)


# ══════════════════════════════════════════════
# 진입점 — 함수 정의 완료 후 실행
# ══════════════════════════════════════════════
render_sidebar(db, KAKAO_API_KEY)

st.title("🚚 LogiTrack — 배차 최적화 시스템")
st.caption("실측 경로 기반 VRPTW 배차 최적화 | 운송비 자동 산출 | 실시간 관제 맵")
if not st.session_state.opt_result:
    _render_queue_page()
else:
    _render_result_page()
