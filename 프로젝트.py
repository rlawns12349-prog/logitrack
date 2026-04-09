import streamlit as st
import folium
from folium import plugins
from streamlit_folium import st_folium
import requests
import pandas as pd
import math
from datetime import datetime, timedelta
import sqlite3
import json
import os
from dotenv import load_dotenv

# ==========================================
# 1. 완벽한 보안 설정 (.env 스마트 탐색 모드)
# ==========================================
st.set_page_config(page_title="LogiTrack Pro v22.2", layout="wide")

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, '.env')

load_dotenv(dotenv_path=env_path) 
KAKAO_API_KEY = os.getenv("KAKAO_API_KEY")

if not KAKAO_API_KEY:
    st.error("🚨 KAKAO_API_KEY를 찾을 수 없습니다!")
    st.warning(f"👉 파이썬이 파일을 뒤진 정확한 위치: {env_path}")
    st.info("위치에 파일이 없다면 옮겨주시고, 파일 이름이 '.env.txt'로 되어있지 않은지 윈도우 탐색기에서 확인해주세요.")
    st.stop()

# 세션 상태 초기화
if 'db_data' not in st.session_state: st.session_state.db_data = []
if 'targets' not in st.session_state: st.session_state.targets = []
if 'opt_result' not in st.session_state: st.session_state.opt_result = None

st.markdown("""
    <style>
    [data-testid="stMetricValue"] { font-size: 1.8rem; color: #4dabf7; font-weight: bold; }
    .stButton>button { border-radius: 6px; font-weight: bold; transition: 0.2s; }
    .stTable { font-size: 0.9rem !important; text-align: center; }
    th { text-align: center !important; background-color: #f8f9fa; }
    div[data-testid="stToast"] { border-left: 5px solid #4dabf7; }
    </style>
""", unsafe_allow_html=True)

# ==========================================
# 2. 영구 저장소 (SQLite3)
# ==========================================
class DBManager:
    def __init__(self, db_name='logitrack_final.db'):
        self.db_name = db_name
        self.init_db()
        
    def init_db(self):
        conn = sqlite3.connect(self.db_name)
        conn.execute('''CREATE TABLE IF NOT EXISTS locations
                     (name TEXT UNIQUE, lat REAL, lon REAL, addr TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS scenarios
                     (s_name TEXT UNIQUE, targets_data TEXT, result_data TEXT, created_at TEXT)''')
        conn.commit()
        conn.close()
        
    def load_all_locations(self):
        conn = sqlite3.connect(self.db_name)
        df = pd.read_sql_query("SELECT * FROM locations", conn)
        conn.close()
        return df.to_dict('records')
        
    def insert_location(self, name, lat, lon, addr):
        try:
            conn = sqlite3.connect(self.db_name)
            conn.execute("INSERT INTO locations VALUES (?,?,?,?)", (name, lat, lon, addr))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError: 
            return False

    def delete_location(self, name):
        conn = sqlite3.connect(self.db_name)
        conn.execute("DELETE FROM locations WHERE name=?", (name,))
        conn.commit()
        conn.close()

    def save_scenario(self, s_name, targets, result):
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect(self.db_name)
            conn.execute("INSERT OR REPLACE INTO scenarios VALUES (?,?,?,?)", 
                         (s_name, json.dumps(targets), json.dumps(result), now))
            conn.commit()
            conn.close()
            return True
        except: return False

    def load_scenarios(self):
        conn = sqlite3.connect(self.db_name)
        df = pd.read_sql_query("SELECT * FROM scenarios ORDER BY created_at DESC", conn)
        conn.close()
        return df.to_dict('records')

    def delete_scenario(self, s_name):
        conn = sqlite3.connect(self.db_name)
        conn.execute("DELETE FROM scenarios WHERE s_name=?", (s_name,))
        conn.commit()
        conn.close()

db = DBManager()

if not st.session_state.db_data:
    st.session_state.db_data = db.load_all_locations()

def refresh_db(): 
    st.session_state.db_data = db.load_all_locations()

# ==========================================
# 3. 데이터 엔진 (카카오 API)
# ==========================================
def get_kakao_coordinate(address):
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"query": address}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('documents'):
            match = res.json()['documents'][0]
            return float(match['y']), float(match['x']), match['address_name']
    except: pass
    return None, None, None

@st.cache_data(ttl=300, show_spinner=False)
def get_kakao_traffic_routes(p1_lat, p1_lon, p2_lat, p2_lon):
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"origin": f"{p1_lon},{p1_lat}", "destination": f"{p2_lon},{p2_lat}", "priority": "RECOMMEND", "alternatives": "true"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=5)
        if res.status_code == 200 and res.json().get('routes'):
            all_paths = []
            for idx, route in enumerate(res.json()['routes']):
                summary = route['summary']
                traffic_segments = []
                for section in route['sections']:
                    for road in section['roads']:
                        state = road.get('traffic_state', 1)
                        color = "#2ecc71" if state == 1 else "#f1c40f" if state <= 2 else "#e67e22" if state == 3 else "#e74c3c"
                        v = road['vertexes']
                        path = [[v[i+1], v[i]] for i in range(0, len(v), 2)]
                        traffic_segments.append({"path": path, "color": color})
                all_paths.append({"name": f"경로 {idx+1} ({'최적' if idx==0 else '대안'})", "dist": summary['distance'] / 1000, "time": summary['duration'] / 60, "segments": traffic_segments})
            return all_paths
    except: pass
    d = math.dist((p1_lat, p1_lon), (p2_lat, p2_lon)) * 111
    return [{"name": "직선 경로 (API 응답 지연)", "dist": d, "time": (d/25)*60, "segments": [{"path": [[p1_lat, p1_lon], [p2_lat, p2_lon]], "color": "#808080"}]}]

# ==========================================
# 4. 사이드바: 관제 패널
# ==========================================
with st.sidebar:
    st.title("🛰️ LogiTrack Pro v22.2")
    
    with st.expander("🏢 1. 거점 마스터 관리", expanded=False):
        n_name = st.text_input("지점 명칭", placeholder="예: 서울센터")
        n_addr = st.text_input("도로명 주소", placeholder="예: 세종대로 110")
        
        if st.button("💾 DB 영구 저장", use_container_width=True):
            if n_name and n_addr:
                lat, lon, f_addr = get_kakao_coordinate(n_addr)
                if lat: 
                    if db.insert_location(n_name, lat, lon, f_addr):
                        refresh_db()
                        st.toast(f"'{n_name}' 등록 완료 ✅")
                        st.rerun()
                    else:
                        st.error("❌ 이미 존재하는 지점 명칭입니다.")
                else:
                    st.error("❌ 해당 주소를 찾을 수 없습니다.")
            else:
                st.warning("⚠️ 지점 명칭과 주소를 모두 입력해주세요.")

    st.subheader("🚚 2. 배차 시나리오 설정")
    truck_cap = st.number_input("🚛 차량 적재량 (Box)", min_value=10, value=100)
    
    if st.session_state.db_data:
        all_names = [d['name'] for d in st.session_state.db_data]
        start_node = st.selectbox("🚩 출발 허브", all_names)
        
        added_names = [t['name'] for t in st.session_state.targets]
        available_targets = [n for n in all_names if n not in added_names and n != start_node]
        
        if available_targets:
            sel_target = st.selectbox("📍 배송처 선택 (한 개씩 추가)", available_targets)
            col1, col2 = st.columns(2)
            with col1: prio = st.radio("우선순위", [1, 2, 3], format_func=lambda x:{1:"긴급", 2:"보통", 3:"여유"}[x])
            with col2: demand = st.number_input("물량(Box)", min_value=1, value=20)
            
            if st.button("➕ 리스트에 추가", use_container_width=True) and sel_target:
                item = next(i for i in st.session_state.db_data if i['name'] == sel_target)
                st.session_state.targets.append({**item, "priority": prio, "demand": demand})
                st.rerun()

    if st.session_state.targets:
        del_target = st.selectbox("리스트에서 제거", [t['name'] for t in st.session_state.targets])
        if st.button("배송지 제외", type="primary", use_container_width=True):
            st.session_state.targets = [t for t in st.session_state.targets if t['name'] != del_target]
            st.rerun()

    st.divider()
    st.subheader("📂 3. 시나리오 보관함")
    
    with st.expander("💾 현재 결과 저장하기", expanded=False):
        s_save_name = st.text_input("시나리오 명칭", placeholder="예: 월요 배송 A코스")
        if st.button("시나리오 DB 저장", use_container_width=True):
            if s_save_name and st.session_state.opt_result:
                if db.save_scenario(s_save_name, st.session_state.targets, st.session_state.opt_result):
                    st.toast("시나리오가 저장되었습니다. ✅")
                else: st.error("저장 실패")
            else: st.warning("저장할 결과가 없습니다.")

    saved_scenarios = db.load_scenarios()
    if saved_scenarios:
        s_list = [s['s_name'] for s in saved_scenarios]
        selected_s = st.selectbox("불러올 시나리오", s_list)
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            if st.button("📂 복구", use_container_width=True):
                data = next(s for s in saved_scenarios if s['s_name'] == selected_s)
                st.session_state.targets = json.loads(data['targets_data'])
                st.session_state.opt_result = json.loads(data['result_data'])
                st.rerun()
        with col_s2:
            if st.button("🗑️ 삭제", use_container_width=True):
                db.delete_scenario(selected_s)
                st.rerun()

# ==========================================
# 5. 메인 로직 및 대시보드
# ==========================================
st.title("🚚 지능형 배차 관제 및 시나리오 매니저")

if st.button("🚀 실시간 교통 분석 및 최적 경로 산출", type="primary", use_container_width=True):
    if not st.session_state.targets:
        st.error("배송지를 먼저 추가해주세요.")
    else:
        with st.spinner('카카오 내비 실시간 정체 구간 다중 경로 분석 중...'):
            start_info = next(i for i in st.session_state.db_data if i['name'] == start_node)
            unvisited = st.session_state.targets.copy()
            final_route = [start_info]
            
            total_dist, total_time = 0, 0
            main_segments, all_alternatives = [], [] # 💡 구간별 다중경로 비교 데이터 저장소
            curr_time = datetime.now().replace(hour=9, minute=0, second=0)
            current_load, trip_count = 0, 1

            report = [{
                "순서": "출발", "거점명": f"🚩 {start_info['name']} (허브)", 
                "도착/출발": curr_time.strftime("%H:%M"), "이동 거리": "-", "적재 상태": "0 Box"
            }]

            while unvisited:
                curr = final_route[-1]
                next_n = min(unvisited, key=lambda x: math.dist((curr['lat'], curr['lon']), (x['lat'], x['lon'])) * (x['priority']**1.4))
                is_reloading = (current_load + next_n['demand'] > truck_cap)
                dest = start_info if is_reloading else next_n
                
                # 💡 [핵심 추가] 카카오 API가 찾아낸 모든 다중 경로 데이터를 현재 구간정보(From->To)와 함께 저장합니다.
                routes = get_kakao_traffic_routes(curr['lat'], curr['lon'], dest['lat'], dest['lon'])
                best = routes[0]
                all_alternatives.append({"from": curr['name'], "to": dest['name'], "options": routes})
                
                total_dist += best['dist']; total_time += best['time'] + (30 if is_reloading else 15)
                main_segments.extend(best['segments'])
                arr_time = curr_time + timedelta(minutes=best['time'])
                
                if is_reloading:
                    report.append({"순서": f"T{trip_count} 회차", "거점명": "🔄 허브 복귀", "도착/출발": arr_time.strftime("%H:%M"), "이동 거리": f"{best['dist']:.2f} km", "적재 상태": "0 Box"})
                    final_route.append(start_info); curr_time = arr_time + timedelta(minutes=30); current_load = 0; trip_count += 1
                else:
                    current_load += next_n['demand']
                    report.append({"순서": f"배송 {len(final_route)}", "거점명": f"📦 {next_n['name']}", "도착/출발": arr_time.strftime("%H:%M"), "이동 거리": f"{best['dist']:.2f} km", "적재 상태": f"{current_load}/{truck_cap} Box"})
                    final_route.append(next_n); unvisited.remove(next_n); curr_time = arr_time + timedelta(minutes=15)
            
            # 마지막 허브 복귀 구간
            last_routes = get_kakao_traffic_routes(final_route[-1]['lat'], final_route[-1]['lon'], start_info['lat'], start_info['lon'])
            last = last_routes[0]
            all_alternatives.append({"from": final_route[-1]['name'], "to": start_info['name'], "options": last_routes})
            
            arr_time = curr_time + timedelta(minutes=last['time'])
            report.append({"순서": "종료", "거점명": f"🏁 {start_info['name']} (복귀)", "도착/출발": arr_time.strftime("%H:%M"), "이동 거리": f"{last['dist']:.2f} km", "적재 상태": "-"})
            total_dist += last['dist']; total_time += last['time']
            main_segments.extend(last['segments'])

            st.session_state.opt_result = {"route": final_route, "report": report, "dist": total_dist, "time": total_time, "segments": main_segments, "trips": trip_count, "alternatives": all_alternatives}
            st.rerun()

# --- 7. 결과 대시보드 ---
res = st.session_state.opt_result
m1, m2, m3, m4 = st.columns(4)
m1.metric("총 이동 거리", f"{res['dist']:.2f} km" if res else "0.00 km")
m2.metric("총 예상 시간", f"{int(res['time']//60)}h {int(res['time']%60)}m" if res else "0h 0m")
m3.metric("필요 차량 (회차)", f"{res['trips']} 회" if res else "-")
m4.metric("총 배송 물량", f"{sum(t['demand'] for t in st.session_state.targets)} Box" if res else "0 Box")

st.divider()

if res:
    # 💡 [핵심 UI 추가] 모든 구간의 다중 경로를 비교하여 '왜 이 경로가 최적인지'를 보여주는 마스터 테이블
    st.subheader("📊 구간별 다중 경로 대안 분석표 (최적 경로 선정 근거)")
    
    comp_rows = []
    for alt in res['alternatives']:
        for opt in alt['options']:
            is_best = "최적" in opt['name']
            comp_rows.append({
                "이동 구간": f"{alt['from']} ➔ {alt['to']}",
                "탐색된 경로": opt['name'],
                "예상 소요 시간": f"{int(opt['time'])}분",
                "이동 거리": f"{opt['dist']:.2f} km",
                "채택 여부": "✅ 최적 채택" if is_best else "대안 (우회)"
            })
            
    st.dataframe(pd.DataFrame(comp_rows), use_container_width=True, hide_index=True)

    st.write("") # 간격 살짝 띄우기
    col_l, col_r = st.columns([1.1, 2])

    with col_l:
        st.subheader("📝 상세 운행 지시서")
        st.table(pd.DataFrame(res['report']))

    with col_r:
        st.subheader("🗺️ 실시간 교통 상황 맵")
        hub = next((i for i in st.session_state.db_data if i['name'] == (start_node if 'start_node' in locals() else st.session_state.db_data[0]['name'])), st.session_state.db_data[0]) if st.session_state.db_data else None
        c_loc = [hub['lat'], hub['lon']] if hub else [37.5665, 126.9780]
        m = folium.Map(location=c_loc, zoom_start=12, tiles="cartodbpositron")
        
        if res['route']:
            lats = [loc['lat'] for loc in res['route']]
            lons = [loc['lon'] for loc in res['route']]
            m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])

        for seg in res['segments']: 
            folium.PolyLine(locations=seg['path'], color=seg['color'], weight=6, opacity=0.8).add_to(m)
        plugins.AntPath(locations=[p for seg in res['segments'] for p in seg['path']], delay=1500, color="white", weight=2, opacity=0.5).add_to(m)
        for i, loc in enumerate(res['route']):
            icon_color = "black" if loc['name'] == hub['name'] else "#4dabf7"
            label = "H" if loc['name'] == hub['name'] else str(i)
            folium.Marker([loc["lat"], loc["lon"]], tooltip=f"{loc['name']} ({loc['addr']})", icon=folium.DivIcon(html=f"""<div style="background:{icon_color}; border:2px solid white; border-radius:50%; color:white; font-weight:bold; width:28px; height:28px; line-height:24px; text-align:center; font-size:10pt;">{label}</div>""")).add_to(m)
            
        st_folium(m, width="100%", height=550, key="final_map")

if not res and st.session_state.targets:
    st.info("👇 상단의 [🚀 경로 산출] 버튼을 누르면 아래 대기 중인 배송지들을 최적화하여 운행 지시서를 생성합니다.")
    st.table(pd.DataFrame(st.session_state.targets)[['name', 'priority', 'demand']].rename(columns={'name':'거점 명칭', 'priority':'긴급도 (1=높음)', 'demand':'필요 물량(Box)'}))
