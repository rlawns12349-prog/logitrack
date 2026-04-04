import streamlit as st
import folium
from folium import plugins
from streamlit_folium import st_folium
import requests
import pandas as pd
import math
from geopy.geocoders import Nominatim
from geopy.point import Point
from datetime import datetime, timedelta
import sqlite3

# --- 1. 환경 설정 및 스타일링 ---
st.set_page_config(page_title="LogiTrack Pro v16.1 (Opt)", layout="wide")

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #004aad; font-weight: bold; }
    .stButton>button { border-radius: 6px; font-weight: bold; transition: 0.2s; }
    .stTable { font-size: 0.85rem !important; }
    div[data-testid="stToast"] { border-left: 5px solid #004aad; }
    </style>
""", unsafe_allow_html=True)

# --- [최적화 1] DB 커넥션 캐싱 ---
@st.cache_resource
def get_db_connection():
    # check_same_thread=False 옵션으로 Streamlit의 멀티스레드 환경 대응
    conn = sqlite3.connect('logitrack_v16_opt.db', check_same_thread=False)
    conn.execute('''CREATE TABLE IF NOT EXISTS locations
                    (name TEXT UNIQUE, lat REAL, lon REAL, addr TEXT)''')
    return conn

# --- 2. 영구 저장소 모듈화 ---
class DBManager:
    def __init__(self):
        self.conn = get_db_connection()

    def load_all(self):
        # [최적화 2] List 대신 Dictionary로 반환하여 O(1) 탐색 보장
        df = pd.read_sql_query("SELECT * FROM locations", self.conn)
        # 결과 형태: {'서울역': {'lat': 37.5, 'lon': 126.9, 'addr': '...'}, ...}
        return df.set_index('name').to_dict('index')
            
    def insert(self, name, lat, lon, addr):
        try:
            self.conn.execute("INSERT INTO locations VALUES (?,?,?,?)", (name, lat, lon, addr))
            self.conn.commit() # 트랜잭션 수동 커밋
            return True
        except sqlite3.IntegrityError:
            return False
            
    def delete(self, name):
        self.conn.execute("DELETE FROM locations WHERE name=?", (name,))
        self.conn.commit()

db = DBManager()

# --- 3. 데이터 엔진 캐싱 ---
@st.cache_data(ttl=3600, show_spinner=False)
def get_osrm_route(p1_lat, p1_lon, p2_lat, p2_lon):
    url = f"http://router.project-osrm.org/route/v1/driving/{p1_lon},{p1_lat};{p2_lon},{p2_lat}?overview=full&geometries=geojson"
    try:
        res = requests.get(url, timeout=3).json()
        if res.get('routes'):
            route = res['routes'][0]
            return route['distance']/1000, route['duration']/60, [[p[1], p[0]] for p in route['geometry']['coordinates']]
    except requests.exceptions.RequestException: 
        # [최적화 3] 광범위한 except: pass 대신 네트워크 예외만 명시적 처리
        pass
    
    # API 실패 시 하버사인 공식 기반 백업 연산
    d = math.dist((p1_lat, p1_lon), (p2_lat, p2_lon)) * 111
    return d, (d/25)*60, [[p1_lat, p1_lon], [p2_lat, p2_lon]]

@st.cache_resource
def get_geolocator():
    return Nominatim(user_agent="logitrack_v16_1_opt", timeout=5)

# --- 4. 세션 상태 초기화 ---
if 'db_data' not in st.session_state: st.session_state.db_data = db.load_all()
if 'targets' not in st.session_state: st.session_state.targets = []
if 'start_node' not in st.session_state: st.session_state.start_node = None
if 'end_node' not in st.session_state: st.session_state.end_node = None
if 'opt_result' not in st.session_state: st.session_state.opt_result = None

def refresh_db():
    st.session_state.db_data = db.load_all()

# --- 5. 사이드바: 직관적인 UI 플로우 ---
with st.sidebar:
    st.title("🛰️ LogiTrack v16.1 (Opt)")
    
    with st.expander("🏢 1. 거점 마스터 관리", expanded=False):
        n_name = st.text_input("지점 명칭", placeholder="예: 물류센터 A")
        n_addr = st.text_input("도로명 주소", placeholder="예: 테헤란로 123")
        
        if st.button("DB 영구 저장", use_container_width=True):
            if n_name and n_addr:
                with st.spinner('좌표 변환 중...'):
                    loc = get_geolocator().geocode(f"{n_addr}, South Korea")
                    if loc:
                        if db.insert(n_name, loc.latitude, loc.longitude, loc.address):
                            refresh_db()
                            st.toast(f"'{n_name}' 등록 완료", icon="✅")
                            import time; time.sleep(0.5); st.rerun()
                        else: st.error("이미 존재하는 명칭입니다.")
                    else: st.error("주소 검색 실패")
            else: st.warning("정보를 모두 입력하세요.")
        
        st.divider()
        if st.session_state.db_data:
            # Dictionary Key 기반 리스트 생성
            db_keys = list(st.session_state.db_data.keys())
            del_target = st.selectbox("삭제할 거점 선택", db_keys)
            if st.button("영구 삭제", type="primary"):
                db.delete(del_target)
                refresh_db()
                st.rerun()

    st.subheader("🚚 2. 배차 시나리오 구성")
    if st.session_state.db_data:
        all_names = list(st.session_state.db_data.keys())
        
        col_s, col_e = st.columns(2)
        with col_s:
            st.session_state.start_node = st.selectbox("🚩 출발지", all_names)
        with col_e:
            st.session_state.end_node = st.selectbox("🏁 도착지", ["출발지로 복귀"] + all_names)
        
        sel_targets = st.multiselect("📍 경유지 (배송처) 추가", all_names)
        prio = st.radio("해당 그룹 우선순위", [1, 2, 3], format_func=lambda x: {1:"긴급", 2:"보통", 3:"여유"}[x], horizontal=True)
        
        if st.button("➕ 리스트에 추가", use_container_width=True):
            existing_targets = {t['name'] for t in st.session_state.targets} # Set 탐색으로 속도 향상
            for t in sel_targets:
                if t not in existing_targets:
                    # O(1) 탐색을 통한 딕셔너리 데이터 병합
                    item_data = st.session_state.db_data[t]
                    st.session_state.targets.append({"name": t, "lat": item_data['lat'], "lon": item_data['lon'], "priority": prio})
            st.rerun()

    st.button("🔄 현재 배차 초기화", on_click=lambda: st.session_state.update({"targets":[], "opt_result":None}), type="secondary", use_container_width=True)

# --- 6. 메인 로직 및 대시보드 ---
st.title("🚚 지능형 배차 최적화 대시보드")

if st.button("🚀 AI 경로 시뮬레이션 가동", type="primary", use_container_width=True):
    if not st.session_state.start_node or not st.session_state.targets:
        st.error("출발지와 경유지를 설정해주세요.")
    else:
        with st.spinner('최적화 알고리즘 연산 중...'):
            # O(1) 탐색으로 딕셔너리에서 즉시 호출
            s_data = st.session_state.db_data[st.session_state.start_node]
            start_info = {"name": st.session_state.start_node, "lat": s_data['lat'], "lon": s_data['lon']}
            
            if st.session_state.end_node == "출발지로 복귀":
                end_info = start_info
            else:
                e_data = st.session_state.db_data[st.session_state.end_node]
                end_info = {"name": st.session_state.end_node, "lat": e_data['lat'], "lon": e_data['lon']}
            
            unvisited = st.session_state.targets.copy()
            route = [start_info]
            t_dist, t_time, t_geo, report = 0, 0, [], []
            curr_time = datetime.now().replace(hour=9, minute=0, second=0)

            while unvisited:
                curr = route[-1]
                next_n = min(unvisited, key=lambda x: math.dist((curr['lat'], curr['lon']), (x['lat'], x['lon'])) * (x['priority']**1.4))
                d, t, geo = get_osrm_route(curr['lat'], curr['lon'], next_n['lat'], next_n['lon'])
                
                arr_t = curr_time + timedelta(minutes=t)
                dep_t = arr_t + timedelta(minutes=15)
                report.append({
                    "순서": len(route), "거점": next_n['name'], "도착예정": arr_t.strftime("%H:%M"), "구간거리(km)": f"{d:.2f}"
                })
                t_dist += d; t_time += t + 15; t_geo.extend(geo); route.append(next_n); unvisited.remove(next_n)
                curr_time = dep_t
            
            d_last, t_last, geo_last = get_osrm_route(route[-1]['lat'], route[-1]['lon'], end_info['lat'], end_info['lon'])
            t_dist += d_last; t_time += t_last; t_geo.extend(geo_last); route.append(end_info)
            
            st.session_state.opt_result = {"route": route, "dist": t_dist, "time": t_time, "geo": t_geo, "report": report}
            st.rerun()

# 핵심 지표
m1, m2, m3, m4 = st.columns(4)
res = st.session_state.opt_result
m1.metric("총 이동 거리", f"{res['dist']:.2f} km" if res else "0.00 km")
m2.metric("총 예상 시간", f"{int(res['time']//60)}h {int(res['time']%60)}m" if res else "0h 0m")
m3.metric("출발 / 도착", f"{st.session_state.start_node[:5]}.. ➔ {st.session_state.end_node[:5]}.." if st.session_state.start_node else "-")
m4.metric("경유지 수", f"{len(st.session_state.targets)} 곳")

st.divider()

col_l, col_r = st.columns([1.1, 2])

with col_l:
    st.subheader("📋 실시간 배차 명단")
    if st.session_state.targets:
        df_display = pd.DataFrame(st.session_state.targets)
        st.dataframe(df_display[['name', 'priority']], use_container_width=True, hide_index=True)
    
    if res:
        st.markdown("---")
        st.subheader("📝 상세 운행 지시서")
        df_report = pd.DataFrame(res['report'])
        st.table(df_report)
        
        csv_data = df_report.to_csv(index=False).encode('utf-8-sig')
        st.download_button(label="📥 리포트 다운로드 (CSV)", data=csv_data, file_name="운행지시서.csv", mime="text/csv", use_container_width=True)

with col_r:
    st.subheader("🗺️ 시뮬레이션 맵")
    
    if st.session_state.start_node:
        # Dictionary 구조에 맞춘 데이터 추출
        s_data = st.session_state.db_data[st.session_state.start_node]
        center_loc = [s_data['lat'], s_data['lon']]
        init_zoom = 13
    else:
        center_loc = [37.5665, 126.9780]
        init_zoom = 11

    m = folium.Map(location=center_loc, zoom_start=init_zoom, tiles="cartodbpositron")
    bounds = []

    if res:
        plugins.AntPath(locations=res['geo'], delay=1200, color="#004aad", weight=6).add_to(m)
        for i, loc in enumerate(res['route']):
            bounds.append([loc['lat'], loc['lon']])
            if i == 0: m_color, label = "black", "START"
            elif i == len(res['route']) - 1: m_color, label = "red", "END"
            else: m_color, label = "#004aad", str(i)
            
            folium.Marker(
                [loc["lat"], loc["lon"]], tooltip=loc['name'],
                icon=folium.DivIcon(html=f"""<div style="background:{m_color}; border:2px solid white; border-radius:50%; color:white; font-weight:bold; width:34px; height:34px; line-height:30px; text-align:center; box-shadow:0 2px 4px rgba(0,0,0,0.3);">{label}</div>""")
            ).add_to(m)
        if len(bounds) > 1:
            m.fit_bounds(bounds)
            
    elif st.session_state.start_node:
        bounds.append(center_loc)
        for loc in st.session_state.targets:
            bounds.append([loc['lat'], loc['lon']])
            folium.Marker([loc['lat'], loc['lon']], tooltip=loc['name']).add_to(m)
        if len(bounds) > 1:
            m.fit_bounds(bounds)

    st_folium(m, width="100%", height=550, key="v16_1_opt_map", returned_objects=[])
