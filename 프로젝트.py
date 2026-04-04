import streamlit as st
import folium
from folium import plugins
from streamlit_folium import st_folium
import requests
import pandas as pd
import math
from datetime import datetime, timedelta
import sqlite3

# --- 1. 환경 설정 및 스타일링 ---
st.set_page_config(page_title="LogiTrack Pro v17.0 (Kakao Edition)", layout="wide")

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #004aad; font-weight: bold; }
    .stButton>button { border-radius: 6px; font-weight: bold; transition: 0.2s; }
    .stTable { font-size: 0.85rem !important; }
    div[data-testid="stToast"] { border-left: 5px solid #004aad; }
    </style>
""", unsafe_allow_html=True)

# --- 2. 영구 저장소 (SQLite3) ---
class DBManager:
    def __init__(self, db_name='logitrack_final.db'):
        self.db_name = db_name
        self.init_db()
    def init_db(self):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS locations
                         (name TEXT UNIQUE, lat REAL, lon REAL, addr TEXT)''')
    def load_all(self):
        with sqlite3.connect(self.db_name) as conn:
            return pd.read_sql_query("SELECT * FROM locations", conn).to_dict('records')
    def insert(self, name, lat, lon, addr):
        try:
            with sqlite3.connect(self.db_name) as conn:
                conn.execute("INSERT INTO locations VALUES (?,?,?,?)", (name, lat, lon, addr))
            return True
        except sqlite3.IntegrityError: return False
    def delete(self, name):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("DELETE FROM locations WHERE name=?", (name,))

db = DBManager()

# --- 3. 데이터 엔진 (카카오 로컬 & OSRM) ---
KAKAO_API_KEY = "fe0dfac392ddccedff5878a56a36feb9"

@st.cache_data(ttl=3600, show_spinner=False)
def get_kakao_coordinate(address):
    """카카오 로컬 API를 활용한 고정밀 주소 검색"""
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"query": address}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=3).json()
        if res.get('documents'):
            match = res['documents'][0]
            return float(match['y']), float(match['x']), match['address_name']
        return None, None, None
    except: return None, None, None

@st.cache_data(ttl=3600, show_spinner=False)
def get_osrm_route(p1_lat, p1_lon, p2_lat, p2_lon):
    url = f"http://router.project-osrm.org/route/v1/driving/{p1_lon},{p1_lat};{p2_lon},{p2_lat}?overview=full&geometries=geojson"
    try:
        res = requests.get(url, timeout=3).json()
        if res.get('routes'):
            route = res['routes'][0]
            return route['distance']/1000, route['duration']/60, [[p[1], p[0]] for p in route['geometry']['coordinates']]
    except: pass
    d = math.dist((p1_lat, p1_lon), (p2_lat, p2_lon)) * 111
    return d, (d/25)*60, [[p1_lat, p1_lon], [p2_lat, p2_lon]]

# --- 4. 세션 상태 관리 ---
if 'db_data' not in st.session_state: st.session_state.db_data = db.load_all()
if 'targets' not in st.session_state: st.session_state.targets = []
if 'opt_result' not in st.session_state: st.session_state.opt_result = None

def refresh_db(): st.session_state.db_data = db.load_all()

# --- 5. 사이드바: 관제 패널 ---
with st.sidebar:
    st.title("🛰️ LogiTrack Pro v17.0")
    
    with st.expander("🏢 1. 거점 마스터 관리", expanded=False):
        n_name = st.text_input("지점 명칭", placeholder="예: 서울센터")
        n_addr = st.text_input("도로명 주소", placeholder="예: 세종대로 110")
        if st.button("💾 DB 영구 저장", use_container_width=True):
            if n_name and n_addr:
                with st.spinner('카카오 지도로 정밀 변환 중...'):
                    lat, lon, f_addr = get_kakao_coordinate(n_addr)
                    if lat and db.insert(n_name, lat, lon, f_addr):
                        refresh_db(); st.toast(f"'{n_name}' 등록 완료", icon="✅")
                        import time; time.sleep(0.5); st.rerun()
                    else: st.error("주소 검색 실패 또는 중복 명칭")
        
        if st.session_state.db_data:
            st.divider()
            del_target = st.selectbox("삭제할 거점", [d['name'] for d in st.session_state.db_data])
            if st.button("영구 삭제", type="primary"):
                db.delete(del_target); refresh_db(); st.rerun()

    st.subheader("🚚 2. 배차 시나리오 & 적재 설정")
    truck_cap = st.number_input("🚛 차량 최대 적재량 (Box)", min_value=10, value=100, step=10)
    
    if st.session_state.db_data:
        all_names = [d['name'] for d in st.session_state.db_data]
        start_node = st.selectbox("🚩 출발 허브", all_names)
        
        st.divider()
        sel_targets = st.multiselect("📍 배송처 추가", all_names)
        col1, col2 = st.columns(2)
        with col1: prio = st.radio("우선순위", [1, 2, 3], format_func=lambda x:{1:"긴급", 2:"보통", 3:"여유"}[x])
        with col2: demand = st.number_input("물량(Box)", min_value=1, value=20)
        
        if st.button("➕ 리스트에 추가", use_container_width=True):
            for t in sel_targets:
                if not any(d['name'] == t for d in st.session_state.targets):
                    item = next(i for i in st.session_state.db_data if i['name'] == t)
                    st.session_state.targets.append({**item, "priority": prio, "demand": demand})
            st.rerun()

    st.button("🔄 배차 초기화", on_click=lambda: st.session_state.update({"targets":[], "opt_result":None}), use_container_width=True)

# --- 6. 메인 로직: CVRP 알고리즘 ---
st.title("🚚 지능형 용량 제약 배차 대시보드")

if st.button("🚀 AI 경로 최적화 시뮬레이션 가동", type="primary", use_container_width=True):
    if not st.session_state.targets:
        st.error("배송지를 추가해주세요.")
    else:
        with st.spinner('적재 제약을 고려한 최적 경로 산출 중...'):
            start_info = next(i for i in st.session_state.db_data if i['name'] == start_node)
            unvisited = st.session_state.targets.copy()
            route, t_dist, t_time, t_geo, report = [start_info], 0, 0, [], []
            curr_time = datetime.now().replace(hour=9, minute=0, second=0)
            current_load, trip_count = 0, 1

            while unvisited:
                curr = route[-1]
                # 가중치 알고리즘 (Priority^1.4)
                next_n = min(unvisited, key=lambda x: math.dist((curr['lat'], curr['lon']), (x['lat'], x['lon'])) * (x['priority']**1.4))
                
                # 용량 초과 시 센터 복귀 (Reload)
                if current_load + next_n['demand'] > truck_cap:
                    d_back, t_back, geo_back = get_osrm_route(curr['lat'], curr['lon'], start_info['lat'], start_info['lon'])
                    report.append({"구분": f"T{trip_count}", "거점": "🔄 허브 복귀(재적재)", "도착": (curr_time + timedelta(minutes=t_back)).strftime("%H:%M"), "구간(km)": f"{d_back:.2f}", "적재": "0/0"})
                    t_dist += d_back; t_time += t_back + 30; t_geo.extend(geo_back)
                    route.append(start_info); curr_time += timedelta(minutes=t_back + 30); current_load = 0; trip_count += 1
                    continue
                
                d, t, geo = get_osrm_route(curr['lat'], curr['lon'], next_n['lat'], next_n['lon'])
                arr_t = curr_time + timedelta(minutes=t)
                current_load += next_n['demand']
                report.append({"구분": f"T{trip_count}", "거점": next_n['name'], "도착": arr_t.strftime("%H:%M"), "구간(km)": f"{d:.2f}", "적재": f"{current_load}/{truck_cap}"})
                t_dist += d; t_time += t + 15; t_geo.extend(geo); route.append(next_n); unvisited.remove(next_n)
                curr_time = arr_t + timedelta(minutes=15)
            
            # 최종 복귀
            d_last, t_last, geo_last = get_osrm_route(route[-1]['lat'], route[-1]['lon'], start_info['lat'], start_info['lon'])
            t_dist += d_last; t_time += t_last; t_geo.extend(geo_last); route.append(start_info)
            report.append({"구분": "종료", "거점": "🏁 허브 복귀", "도착": (curr_time + timedelta(minutes=t_last)).strftime("%H:%M"), "구간(km)": f"{d_last:.2f}", "적재": "-"})

            st.session_state.opt_result = {"route": route, "dist": t_dist, "time": t_time, "geo": t_geo, "report": report, "trips": trip_count}
            st.rerun()

# --- 7. 결과 대시보드 ---
res = st.session_state.opt_result
m1, m2, m3, m4 = st.columns(4)
m1.metric("총 이동 거리", f"{res['dist']:.2f} km" if res else "0.00 km")
m2.metric("총 예상 시간", f"{int(res['time']//60)}h {int(res['time']%60)}m" if res else "0h 0m")
m3.metric("필요 차량 (회차)", f"{res['trips']} 회" if res else "-")
m4.metric("총 배송 물량", f"{sum(t['demand'] for t in st.session_state.targets)} Box")

st.divider()
col_l, col_r = st.columns([1.1, 2])

with col_l:
    st.subheader("📋 배송 현황")
    if st.session_state.targets:
        st.dataframe(pd.DataFrame(st.session_state.targets)[['name', 'priority', 'demand']], use_container_width=True, hide_index=True)
    if res:
        st.markdown("---")
        st.subheader("📝 운행 지시서")
        st.table(pd.DataFrame(res['report']))

with col_r:
    st.subheader("🗺️ 경로 최적화 맵")
    # 지도 중심 설정
    if st.session_state.db_data:
        hub = next((i for i in st.session_state.db_data if i['name'] == (start_node if 'start_node' in locals() else st.session_state.db_data[0]['name'])), st.session_state.db_data[0])
        c_loc = [hub['lat'], hub['lon']]
    else: c_loc = [37.5665, 126.9780]

    m = folium.Map(location=c_loc, zoom_start=12, tiles="cartodbpositron")
    if res:
        plugins.AntPath(locations=res['geo'], delay=1200, color="#004aad", weight=6).add_to(m)
        for i, loc in enumerate(res['route']):
            icon_color = "black" if loc['name'] == hub['name'] else "#004aad"
            label = "H" if loc['name'] == hub['name'] else str(i)
            folium.Marker([loc["lat"], loc["lon"]], tooltip=loc['name'], icon=folium.DivIcon(html=f"""<div style="background:{icon_color}; border:2px solid white; border-radius:50%; color:white; font-weight:bold; width:30px; height:30px; line-height:26px; text-align:center;">{label}</div>""")).add_to(m)
    st_folium(m, width="100%", height=500, key="final_map")
