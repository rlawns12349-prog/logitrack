"""
프로젝트.py — LogiTrack 앱 진입점 (리팩터링 버전)

변경 사항:
  - run_optimization()       → core/optimization.py
  - D9 클러스터링            → features/clustering.py  (버그 수정 포함)
  - D10~D14 분석 기능        → features/analytics.py   (규칙 기반 예보 개선 포함)
  - 사이드바                 → ui/sidebar.py            (컬럼 인덱스 캐싱 개선)
  - 이 파일에는 앱 초기화·레이아웃·UI 라우팅만 남김

v2 버그 수정 및 보완:
  - render_bulk_upload: f-string 따옴표 충돌(TypeError) 수정 (741번째 줄)
  - render_bulk_upload: _get() 함수가 루프 내에서 매 iteration 재정의되는 클로저 버그 수정
  - render_bulk_upload: start_offset·db_node_map을 루프 밖으로 이동해 불필요한 반복 연산 제거
  - render_learning_warning: res dict 대신 session_state에서 이전 값 읽도록 수정 (항상 -1 반환 버그)
    + 현재 실행 결과를 session_state에 저장해 다음 실행 비교 기준으로 활용
  - _call_anthropic: 포괄적 except 대신 HTTPError/URLError/파싱 오류를 분리 처리
  - _detect_deadhead: AttributeError/TypeError도 처리하도록 예외 범위 확장
  - render_dispatch_sheet: 운행지시서에 차량 수·총 비용·CO₂ 요약 추가
"""

import sys, os, json, io, re, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
from datetime import datetime, timedelta
from typing import Any

import nest_asyncio
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from db import DBManager
from geo import LRUCache, DIESEL_EMISSION_FACTOR
from solver import compute_truck_financials

# ── 분리된 모듈 임포트 ──────────────────────────
from core.optimization import run_optimization

from features.clustering import render_cluster_analysis
from features.analytics import (
    calc_fatigue, fatigue_label, log_run,
    render_run_trend, render_risk_screening,
    render_pre_dispatch_forecast, render_driver_equity,
)

try:
    from ui.sidebar   import render_sidebar
    from ui.dashboard import render_dashboard
    from ui.map_view  import render_report, render_map
except ModuleNotFoundError:
    from sidebar   import render_sidebar
    from dashboard import render_dashboard
    from map_view  import render_report, render_map

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("logitrack")
nest_asyncio.apply()

st.set_page_config(page_title="LogiTrack — 배차 최적화 시스템",
                   layout="wide", page_icon="🚚")

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path, override=True)


# ══════════════════════════════════════════════
# API 키 & 리소스
# ══════════════════════════════════════════════

@st.cache_resource
def _get_kakao_key() -> str | None:
    try:    return st.secrets["KAKAO_API_KEY"]
    except: return os.getenv("KAKAO_API_KEY")

@st.cache_resource
def _get_db_url() -> str | None:
    try:    return st.secrets["SUPABASE_DB_URL"]
    except: return os.getenv("SUPABASE_DB_URL")

@st.cache_resource
def _get_anthropic_key() -> str | None:
    try:    return st.secrets["ANTHROPIC_API_KEY"]
    except: return os.getenv("ANTHROPIC_API_KEY")

KAKAO_API_KEY     = _get_kakao_key()
SUPABASE_DB_URL   = _get_db_url()
ANTHROPIC_API_KEY = _get_anthropic_key()

_missing = []
if not KAKAO_API_KEY:   _missing.append("• **KAKAO_API_KEY**")
if not SUPABASE_DB_URL: _missing.append("• **SUPABASE_DB_URL**")
if _missing:
    st.error("🚨 환경 변수 누락:\n\n" + "\n".join(_missing)); st.stop()

@st.cache_resource
def _get_api_cache() -> LRUCache:  return LRUCache(maxsize=500)

@st.cache_resource
def _get_db() -> DBManager:        return DBManager(SUPABASE_DB_URL)

@st.cache_resource
def _run_startup_purge() -> bool:
    try:    _get_db().purge_old_route_cache(); logger.info("Purge OK.")
    except Exception as e: logger.warning("Purge failed: %s", e)
    return True

db        = _get_db()
api_cache = _get_api_cache()
_run_startup_purge()


# ══════════════════════════════════════════════
# 세션 초기화
# ══════════════════════════════════════════════

_SESSION_DEFAULTS: dict[str, Any] = {
    "db_data": [], "targets": [], "opt_result": None, "start_node": "",
    "delivery_done": {}, "cfg_1t_cnt": 2, "cfg_2t_cnt": 1, "cfg_5t_cnt": 0,
    "cfg_speed": 45, "cfg_service": 10, "cfg_service_sec_per_kg": 2,
    "cfg_fuel_price": 1500, "cfg_labor": 15000, "cfg_max_hours": 10,
    "cfg_balance": False, "cfg_vrptw_sec": 5, "cfg_congestion": 40,
    "cfg_start_time": "09:00", "cfg_weather": "맑음",
    "cfg_v1_skills": [], "cfg_v2_skills": [], "cfg_v5_skills": [],
    "_last_upload_id": "", "_balloons_shown": False, "_opt_in_progress": False,
    "_prev_sla": -1.0, "_prev_eff": -1.0,
    "_run_log": [], "_forecast_cache": None, "_forecast_key": "",
}

def init_session() -> None:
    for k, v in _SESSION_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session()
if not st.session_state.db_data:
    st.session_state.db_data = db.load_locations()


# ── CSS ─────────────────────────────────────────
st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&display=swap');

:root {
  --bg:      #0a0c10;
  --surface: #111318;
  --line:    #1e2330;
  --line2:   #2a3348;

  --text:    #e8edf5;
  --text2:   #6b7a94;
  --text3:   #3a4558;

  --blue:    #4f8ef7;
  --green:   #34d399;
  --amber:   #f59e0b;
  --red:     #ef4444;
  --purple:  #a78bfa;
  --teal:    #22d3ee;
  --orange:  #f97316;

  --blue-dim:   #0d1f3c;
  --green-dim:  #052e16;
  --amber-dim:  #2d1a00;
  --red-dim:    #2d0707;

  --r: 8px;
  --font: 'DM Sans', -apple-system, sans-serif;
}

/* ── 기반 ── */
html, body,
[data-testid="stApp"],
[data-testid="stAppViewContainer"] > .main {
  background: var(--bg) !important;
  color: var(--text) !important;
  font-family: var(--font) !important;
  font-size: 14px;
  letter-spacing: .01em;
}

/* ── 사이드바 ── */
section[data-testid="stSidebar"] {
  background: var(--surface) !important;
  border-right: 1px solid var(--line) !important;
}
section[data-testid="stSidebar"] * { color: var(--text) !important; }
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] select {
  background: var(--bg) !important;
  border: 1px solid var(--line2) !important;
  color: var(--text) !important;
  border-radius: 6px !important;
}

/* ── 버튼 ── */
.stButton > button {
  background: transparent !important;
  border: 1px solid var(--line2) !important;
  border-radius: var(--r) !important;
  color: var(--text2) !important;
  font-family: var(--font) !important;
  font-size: .875rem !important;
  font-weight: 500 !important;
  letter-spacing: .01em !important;
  transition: border-color .15s, color .15s !important;
}
.stButton > button:hover {
  border-color: var(--blue) !important;
  color: var(--text) !important;
}
.stButton > button[kind="primary"] {
  background: var(--blue) !important;
  border-color: var(--blue) !important;
  color: #fff !important;
  font-weight: 600 !important;
}
.stButton > button[kind="primary"]:hover {
  opacity: .9 !important;
}

/* ── 입력 ── */
input, select, textarea,
[data-baseweb="input"] input,
[data-baseweb="select"] div {
  background: var(--surface) !important;
  border: 1px solid var(--line2) !important;
  border-radius: var(--r) !important;
  color: var(--text) !important;
  font-family: var(--font) !important;
}
[data-baseweb="input"] input:focus {
  border-color: var(--blue) !important;
}

/* ── 메트릭 ── */
[data-testid="stMetric"] {
  background: transparent !important;
  border: none !important;
  border-top: 1px solid var(--line) !important;
  border-radius: 0 !important;
  padding: 16px 0 !important;
}
[data-testid="stMetricValue"] {
  font-size: 1.6rem !important;
  font-weight: 700 !important;
  color: var(--text) !important;
  letter-spacing: -.02em !important;
}
[data-testid="stMetricLabel"] {
  font-size: .7rem !important;
  color: var(--text2) !important;
  font-weight: 500 !important;
  text-transform: uppercase !important;
  letter-spacing: .08em !important;
}

/* ── 탭 ── */
[data-testid="stTabs"] [role="tablist"] {
  background: transparent !important;
  border-bottom: 1px solid var(--line) !important;
  padding: 0 !important;
  gap: 0 !important;
}
[data-testid="stTabs"] [role="tab"] {
  background: transparent !important;
  border: none !important;
  color: var(--text2) !important;
  font-family: var(--font) !important;
  font-size: .83rem !important;
  font-weight: 500 !important;
  padding: 10px 18px !important;
  border-radius: 0 !important;
}
[data-testid="stTabs"] [role="tab"]:hover { color: var(--text) !important; }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  color: var(--text) !important;
  border-bottom: 2px solid var(--blue) !important;
  font-weight: 600 !important;
}
[data-testid="stTabs"] [role="tabpanel"] {
  background: transparent !important;
  border: none !important;
  border-top: none !important;
  border-radius: 0 !important;
  padding: 20px 0 !important;
}

/* ── expander ── */
[data-testid="stExpander"] {
  background: var(--surface) !important;
  border: 1px solid var(--line) !important;
  border-radius: var(--r) !important;
  margin-bottom: 8px !important;
}
[data-testid="stExpander"] summary {
  font-weight: 500 !important;
  font-size: .875rem !important;
  padding: 12px 16px !important;
  color: var(--text) !important;
}

/* ── 테이블 ── */
th {
  background: transparent !important;
  color: var(--text2) !important;
  font-size: .7rem !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: .07em !important;
  border-bottom: 1px solid var(--line) !important;
}
td { color: var(--text) !important; font-size: .875rem !important; }

/* ── 프로그레스 ── */
[data-testid="stProgressBar"] > div > div {
  background: var(--blue) !important;
  border-radius: 99px !important;
}
[data-testid="stProgressBar"] > div {
  background: var(--line) !important;
  border-radius: 99px !important;
  height: 6px !important;
}

hr { border-color: var(--line) !important; margin: 20px 0 !important; }

/* ── 앱 타이틀 ── */
.lt-title {
  display: flex;
  align-items: baseline;
  gap: 12px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 20px;
}
.lt-title-name {
  font-size: 1.1rem;
  font-weight: 700;
  color: var(--text);
  letter-spacing: -.01em;
}
.lt-title-sub {
  font-size: .78rem;
  color: var(--text2);
  font-weight: 400;
}

/* ── 진행 단계 ── */
.lt-steps {
  display: flex;
  align-items: center;
  gap: 0;
  margin-bottom: 20px;
}
.lt-step-item {
  display: flex;
  align-items: center;
  gap: 8px;
}
.lt-step-item + .lt-step-item::before {
  content: '';
  display: block;
  width: 32px;
  height: 1px;
  background: var(--line2);
  margin: 0 4px;
}
.lt-step-dot {
  width: 24px; height: 24px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: .72rem; font-weight: 700;
  flex-shrink: 0;
  background: var(--surface);
  border: 1px solid var(--line2);
  color: var(--text3);
}
.lt-step-item.active .lt-step-dot {
  background: var(--blue);
  border-color: var(--blue);
  color: #fff;
}
.lt-step-item.done .lt-step-dot {
  background: var(--green);
  border-color: var(--green);
  color: #0a0c10;
}
.lt-step-label {
  font-size: .8rem;
  color: var(--text3);
  white-space: nowrap;
}
.lt-step-item.active .lt-step-label { color: var(--text); font-weight: 600; }
.lt-step-item.done   .lt-step-label { color: var(--text2); }

/* ── 안내 텍스트 ── */
.lt-guide {
  font-size: .875rem;
  color: var(--text2);
  line-height: 1.7;
  margin-bottom: 8px;
}
.lt-guide b { color: var(--text); font-weight: 600; }
.lt-guide-success { color: var(--green); }

/* ── 대시보드 카드 ── */
.lt-card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--r);
  padding: 18px 20px;
  margin-bottom: 12px;
}
.lt-card-accent-blue   { border-left: 2px solid var(--blue); }
.lt-card-accent-green  { border-left: 2px solid var(--green); }
.lt-card-accent-amber  { border-left: 2px solid var(--amber); }
.lt-card-accent-red    { border-left: 2px solid var(--red); }
.lt-card-accent-purple { border-left: 2px solid var(--purple); }
.lt-card-accent-teal   { border-left: 2px solid var(--teal); }

/* ── KPI ── */
.lt-kpi {
  padding: 16px 0;
  border-top: 1px solid var(--line);
}
.lt-kpi-label {
  font-size: .68rem;
  font-weight: 600;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: .08em;
  margin-bottom: 6px;
}
.lt-kpi-value {
  font-size: 1.5rem;
  font-weight: 700;
  letter-spacing: -.02em;
  line-height: 1;
}
.lt-kpi-sub {
  font-size: .75rem;
  color: var(--text2);
  margin-top: 4px;
}

/* ── CSV 안내 ── */
.lt-csv-guide { margin-bottom: 12px; }
.lt-csv-guide-title {
  font-size: .78rem;
  font-weight: 600;
  color: var(--text2);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: .06em;
}
.lt-csv-step {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  margin-bottom: 8px;
}
.lt-csv-step-num {
  width: 18px; height: 18px;
  border-radius: 50%;
  background: var(--blue);
  color: #fff;
  display: flex; align-items: center; justify-content: center;
  font-size: .66rem; font-weight: 700;
  flex-shrink: 0; margin-top: 1px;
}
.lt-csv-step-text { font-size: .8rem; color: var(--text2); line-height: 1.55; }
.lt-csv-step-text b { color: var(--text); }
.lt-csv-note { font-size: .72rem; color: var(--text3); line-height: 1.5; margin-top: 8px; }

/* ── STEP 헤더 ── */
.lt-step-header { margin: 16px 0 8px; }
.lt-step-header .step-num  { font-size: .65rem; font-weight: 700; color: var(--blue); text-transform: uppercase; letter-spacing: .1em; }
.lt-step-header .step-name { font-size: .82rem; font-weight: 600; color: var(--text); margin-left: 6px; }

/* ── 사이드바 로고 ── */
.lt-logo { padding: 14px 0 12px; border-bottom: 1px solid var(--line); margin-bottom: 16px; }
.lt-logo-name { font-size: .95rem; font-weight: 700; color: var(--text); letter-spacing: -.01em; }
.lt-logo-sub  { font-size: .7rem; color: var(--text2); margin-top: 2px; }

/* ── 완료 배너 ── */
.lt-done { font-size: .82rem; color: var(--green); margin-top: 6px; }
</style>""", unsafe_allow_html=True)

_TRUCK_COLORS = ["#1E3A8A","#DC2626","#059669","#D97706",
                 "#7C3AED","#0891B2","#BE185D","#92400E","#065F46","#1D4ED8"]


# ══════════════════════════════════════════════
# Anthropic 헬퍼
# ══════════════════════════════════════════════

def _call_anthropic(messages: list[dict], system: str = "", max_tokens: int = 500) -> str:
    """Anthropic API 단일 호출. 실패 시 빈 문자열 반환."""
    if not ANTHROPIC_API_KEY:
        return ""
    import urllib.request
    import urllib.error
    body: dict[str, Any] = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        body["system"] = system
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(body).encode("utf-8"),
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["content"][0]["text"].strip()
    except urllib.error.HTTPError as e:
        logger.warning("Anthropic HTTP 오류 %d: %s", e.code, e.reason)
    except urllib.error.URLError as e:
        logger.warning("Anthropic 연결 실패: %s", e.reason)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning("Anthropic 응답 파싱 실패: %s", e)
    except Exception as e:
        logger.warning("Anthropic 호출 실패 (예상치 못한 오류): %s", e)
    return ""


# ══════════════════════════════════════════════
# D1. AI 코멘터리
# ══════════════════════════════════════════════

def _fallback_commentary(res: dict) -> str:
    msgs = []
    sla  = res.get("sla", 100); eff = res.get("efficiency", 0)
    late = res.get("late_count", 0); una = len(res.get("unassigned", []))
    if sla >= 95:   msgs.append(f"SLA {sla}%로 정시 배송 목표를 달성했습니다.")
    elif sla >= 80: msgs.append(f"SLA {sla}%로 양호하나 {late}건의 지연이 발생했습니다.")
    else:           msgs.append(f"⚠️ SLA {sla}%로 개선 필요 — 증차 또는 시간창 조정을 검토하세요.")
    if eff > 10: msgs.append(f"경로 효율 +{eff}%로 유의미한 거리를 절감했습니다.")
    if una > 0:  msgs.append(f"{una}건 미배차 — 차량 용량·시간창 완화를 검토하세요.")
    return " ".join(msgs)

def generate_ai_commentary(res: dict) -> str:
    summary = {
        "총경로수": len(res.get("routes", [])), "총거리km": round(res.get("dist", 0), 1),
        "SLA": res.get("sla", 100), "효율": res.get("efficiency", 0),
        "미배차": len(res.get("unassigned", [])), "지연": res.get("late_count", 0),
        "총비용": int(res.get("total_cost", 0)),
    }
    result = _call_anthropic(
        messages=[{"role": "user", "content":
            f"물류 배차 결과를 현장 관리자에게 3~5문장 한국어 평문으로 설명하세요. "
            f"잘된 점·개선점 모두 포함, 수치 인용 필수.\n\n결과: {json.dumps(summary, ensure_ascii=False)}"}],
        max_tokens=300,
    )
    return result or _fallback_commentary(res)


# ══════════════════════════════════════════════
# D6. 탄소 절감
# ══════════════════════════════════════════════

def _calc_carbon_saving(res: dict) -> dict:
    saved_km  = max(0.0, res.get("nn_real_dist", res.get("dist", 0)) - res.get("dist", 0))
    saved_co2 = round(saved_km / 12.0 * DIESEL_EMISSION_FACTOR, 2)
    nn_d      = res.get("nn_real_dist", 1)
    return {
        "saved_km":  round(saved_km, 1),
        "saved_co2": saved_co2,
        "pct":       round(saved_km / nn_d * 100, 1) if nn_d > 0 else 0.0,
        "trees":     round(saved_co2 / 22, 1),
    }


# ══════════════════════════════════════════════
# D7. 공차 구간
# ══════════════════════════════════════════════

def _detect_deadhead(res: dict) -> list[dict]:
    out = []
    for row in res.get("report", []):
        if "(복귀)" not in row.get("거점", ""): continue
        try:
            dist_str = str(row.get("거리", "0")).replace("km", "").replace("⚠️", "").strip()
            d = float(dist_str)
            if d >= 30:
                out.append({"truck": row["트럭"], "km": d,
                            "msg": f"{row['트럭']}: 복귀 {d:.1f}km — 귀로 픽업 또는 인근 환적 고려"})
        except (ValueError, AttributeError, TypeError):
            pass
    return out


# ══════════════════════════════════════════════
# D2. 시나리오 비교
# ══════════════════════════════════════════════

def render_scenario_panel(res: dict) -> None:
    with st.expander("🔬 차량 구성 시나리오 시뮬레이터 (추정치)", expanded=False):
        st.caption("재배차 없이 비용·탄소를 추정합니다.")
        sc1, sc2, sc3 = st.columns(3)
        with sc1: n1 = st.slider("1톤(냉탑)", 0, 10, st.session_state.cfg_1t_cnt, key="sc_1t")
        with sc2: n2 = st.slider("2.5톤",     0, 10, st.session_state.cfg_2t_cnt, key="sc_2t")
        with sc3: n3 = st.slider("5톤",       0, 10, st.session_state.cfg_5t_cnt, key="sc_3t")
        if n1 + n2 + n3 == 0: st.warning("차량을 1대 이상 선택하세요."); return
        orig_v    = max(len(res.get("routes", [])), 1)
        sim_fixed = n1 * 100_000 + n2 * 180_000 + n3 * 280_000
        factor    = (n1 + n2 + n3) / orig_v
        sim_fuel  = res.get("fuel_cost", 0) * min(factor, 1.25)
        sim_toll  = res.get("toll_cost", 0) * min(factor, 1.25)
        sim_labor = res.get("labor",     0) * min(factor, 1.15)
        sim_total = sim_fixed + sim_fuel + sim_toll + sim_labor
        diff      = sim_total - res.get("total_cost", 0)
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("고정비",      f"₩{int(sim_fixed):,}",   f"{int(sim_fixed - res.get('fixed_cost', 0)):+,}")
        r2.metric("연료+통행",   f"₩{int(sim_fuel + sim_toll):,}")
        r3.metric("총 예상비용", f"₩{int(sim_total):,}",   f"{int(diff):+,}")
        r4.metric("CO₂ 변화",   f"{(factor - 1) * res.get('co2_total', 0):+.1f}kg")


# ══════════════════════════════════════════════
# D4. 학습형 경고
# ══════════════════════════════════════════════

def render_learning_warning(res: dict) -> None:
    # 이전 실행 값은 res dict가 아닌 session_state에서 읽어야 함
    # res dict에는 _prev_sla/_prev_eff 키가 없으므로 항상 -1.0이 반환되는 버그 수정
    prev_sla = st.session_state.get("_prev_sla", -1.0)
    prev_eff = st.session_state.get("_prev_eff", -1.0)
    curr_sla = res.get("sla", 100)
    curr_eff = res.get("efficiency", 0)
    if prev_sla < 0: return
    msgs = []
    if curr_sla < prev_sla - 5:
        msgs.append(f"📉 SLA **{prev_sla:.1f}% → {curr_sla:.1f}%** 하락.")
    if curr_eff < prev_eff - 5:
        msgs.append(f"📉 경로 효율 **{prev_eff:.1f}% → {curr_eff:.1f}%** 저하.")
    if msgs:
        with st.expander("🧠 이전 실행 대비 분석", expanded=True):
            for m in msgs: st.warning(m)
    # 현재 값을 세션에 저장해 다음 실행 비교 기준으로 사용
    st.session_state["_prev_sla"] = curr_sla
    st.session_state["_prev_eff"] = curr_eff


# ══════════════════════════════════════════════
# D5. 운행지시서
# ══════════════════════════════════════════════

def render_dispatch_sheet(res: dict) -> None:
    report = res.get("report", [])
    trucks: dict[str, list] = {}
    for row in report:
        t = row["트럭"]
        if t not in trucks: trucks[t] = []
        trucks[t].append(row)
    total_cost = res.get("total_cost", 0)
    lines = ["="*60,
             f"LogiTrack 운행지시서  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"허브: {res.get('hub_name','-')}  |  총거리: {res.get('dist',0):.1f}km  |  SLA: {res.get('sla',0):.1f}%",
             f"차량 수: {len(trucks)}대  |  총 비용: ₩{int(total_cost):,}  |  CO₂: {res.get('co2_total',0):.1f}kg",
             "="*60, ""]
    for truck, rows in trucks.items():
        lines += [f"▶ {truck}", "-"*40]
        for idx, row in enumerate(rows, 1):
            lines.append(
                f"  {idx:2d}. {row.get('거점','')}\n"
                f"      도착: {row.get('도착','-')}  약속: {row.get('약속시간','-')}  잔여: {row.get('잔여무게','')}\n"
                f"      메모: {row.get('메모','')}"
            )
        lines += ["", ""]
    content = "\n".join(lines)
    st.download_button("📄 운행지시서 다운로드 (.txt)",
                       data=content.encode("utf-8"),
                       file_name=f"운행지시서_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                       mime="text/plain", use_container_width=True)


# ══════════════════════════════════════════════
# D12. 엑셀/CSV 일괄 업로드
# ══════════════════════════════════════════════

_COL_ALIASES: dict[str, list[str]] = {
    "name":          ["이름","거점","배송지","주소","거점명","name","배송처"],
    "weight":        ["무게","kg","무게(kg)","weight","중량"],
    "volume":        ["부피","cbm","부피(cbm)","volume","용적"],
    "tw_disp":       ["시간대","약속시간","배송시간","시간창","tw_disp","time_window"],
    "temperature":   ["온도","온도조건","temperature","냉장여부"],
    "priority":      ["우선순위","priority","vip"],
    "unload_method": ["하차방식","하차","unload_method"],
    "difficulty":    ["난이도","진입난이도","difficulty"],
    "memo":          ["메모","비고","memo","note"],
}

def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    mapping: dict[str, str] = {}
    cols_lower = {c.lower().replace(" ", ""): c for c in df.columns}
    for key, aliases in _COL_ALIASES.items():
        for alias in aliases:
            n = alias.lower().replace(" ", "").replace("(", "").replace(")", "")
            if n in cols_lower:
                mapping[key] = cols_lower[n]; break
    return mapping

def _parse_tw(tw_str: str, default: str = "09:00~18:00") -> str:
    if not tw_str or str(tw_str).strip() in ("", "nan", "None"):
        return default
    s = str(tw_str).strip()
    if re.match(r"^\d{2}:\d{2}~\d{2}:\d{2}$", s): return s
    m = re.match(r"(\d{1,2}):(\d{2})[-~](\d{1,2}):(\d{2})", s)
    if m: return f"{int(m.group(1)):02d}:{m.group(2)}~{int(m.group(3)):02d}:{m.group(4)}"
    m = re.match(r"(\d{1,2})시?(\d{0,2})분?[-~](\d{1,2})시?(\d{0,2})分?", s)
    if m:
        sh, sm = int(m.group(1)), int(m.group(2) or 0)
        eh, em = int(m.group(3)), int(m.group(4) or 0)
        return f"{sh:02d}:{sm:02d}~{eh:02d}:{em:02d}"
    return default

def render_bulk_upload(db_data: list[dict]) -> None:
    """[D12] 엑셀/CSV 일괄 업로드 + 자동 검증 패널."""
    with st.expander("📥 배송지 일괄 업로드 (CSV / 엑셀 붙여넣기)", expanded=False):
        tab_file, tab_paste = st.tabs(["📂 파일 업로드", "📋 텍스트 붙여넣기"])
        raw_df: pd.DataFrame | None = None

        with tab_file:
            uploaded = st.file_uploader("CSV 또는 XLSX 파일", type=["csv","xlsx"], key="bulk_upload_file")
            if uploaded:
                try:
                    raw_df = pd.read_excel(uploaded, dtype=str) if uploaded.name.endswith(".xlsx") \
                             else pd.read_csv(uploaded, dtype=str, comment="#", on_bad_lines="skip")
                except Exception as e:
                    st.error(f"파일 읽기 실패: {e}")

        with tab_paste:
            pasted = st.text_area("탭/쉼표로 구분된 데이터 붙여넣기 (첫 행 헤더)",
                                  height=150, key="bulk_upload_paste")
            if pasted.strip():
                try:
                    sep    = "\t" if "\t" in pasted else ","
                    raw_df = pd.read_csv(io.StringIO(pasted), sep=sep, dtype=str, on_bad_lines="skip")
                except Exception as e:
                    st.error(f"파싱 실패: {e}")

        if raw_df is None or raw_df.empty:
            return

        col_map = _map_columns(raw_df)
        if "name" not in col_map:
            col_map["name"] = raw_df.columns[0]

        db_names    = {d["name"] for d in db_data}
        valid_rows: list[dict] = []
        errors:     list[str]  = []
        skipped_dup: list[str] = []
        # 루프 진입 전 set으로 한 번만 만들어 O(1) 중복 검사
        existing_names: set[str] = {t["name"] for t in st.session_state.targets}

        # 출발 오프셋을 루프 밖에서 한 번만 계산
        try:
            _sh, _sm = map(int, st.session_state.cfg_start_time.split(":"))
        except ValueError:
            _sh, _sm = 9, 0
        _so = _sh * 60 + _sm

        # db_data를 dict로 인덱싱해 루프 내 O(n) 선형탐색 제거
        db_node_map: dict[str, dict] = {d["name"]: d for d in db_data}

        for idx, row in raw_df.iterrows():
            name = str(row.get(col_map.get("name", ""), "")).strip()
            if not name or name.lower() == "nan":
                errors.append(f"행 {idx+2}: 배송지 이름 없음"); continue
            if name not in db_names:
                errors.append("행 {}: \"{}\" — DB에 없는 거점".format(idx + 2, name)); continue
            if name in existing_names:
                skipped_dup.append(name); continue

            # _get을 루프 밖 일반 함수로 분리해 클로저 재생성 방지
            def _get_val(key: str, default: Any = "", _row: Any = row, _cm: dict = col_map) -> str:
                col = _cm.get(key)
                return str(_row.get(col, default)).strip() if col else str(default)

            try:    weight = float(_get_val("weight", 0))
            except: weight = 0.0
            try:    volume = float(_get_val("volume", 0))
            except: volume = 0.0

            raw_temp = _get_val("temperature", "상온")
            temp = ("냉동" if "냉동" in raw_temp else "냉장" if "냉장" in raw_temp else "상온")
            raw_pri  = _get_val("priority", "일반")
            priority = ("VIP" if "vip" in raw_pri.lower() else "여유" if "여유" in raw_pri else "일반")
            tw_disp  = _parse_tw(_get_val("tw_disp", "09:00~18:00"))

            so = _so
            try:
                ts, te   = tw_disp.split("~")
                tw_start = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
                tw_end   = max(tw_start + 1, int(te[:2]) * 60 + int(te[3:]) - so)
            except (ValueError, IndexError):
                tw_start, tw_end = 0, 540

            db_node = db_node_map.get(name)
            valid_rows.append({
                **(db_node or {}),
                "name": name, "weight": weight, "volume": volume,
                "temperature": temp, "priority": priority,
                "tw_disp": tw_disp, "tw_start": tw_start, "tw_end": tw_end,
                "tw_type": "Hard",
                "unload_method": _get_val("unload_method", "수작업"),
                "difficulty":    _get_val("difficulty",    "일반 (+0분)"),
                "memo":          _get_val("memo",          ""),
            })

        st.markdown(
            f"**✅ 추가 가능: {len(valid_rows)}건**"
            + (f"  |  ⚠️ 중복 제외: {len(skipped_dup)}건" if skipped_dup else "")
            + (f"  |  ❌ 오류: {len(errors)}건" if errors else "")
        )
        if errors:
            with st.expander(f"❌ 오류 목록 ({len(errors)}건)"):
                for e in errors: st.caption(e)

        if valid_rows:
            st.dataframe(pd.DataFrame([{
                "배송지": r["name"], "무게(kg)": r["weight"], "부피(CBM)": r["volume"],
                "온도": r["temperature"], "시간대": r["tw_disp"], "우선순위": r["priority"],
            } for r in valid_rows]), hide_index=True, use_container_width=True)

            if st.button(f"📥 {len(valid_rows)}건 대기열에 추가", type="primary",
                         use_container_width=True, key="bulk_confirm"):
                st.session_state.targets.extend(valid_rows)
                st.session_state.opt_result = None
                st.success(f"✅ {len(valid_rows)}건이 배차 대기열에 추가됐습니다.")
                st.rerun()


# ══════════════════════════════════════════════
# 대기열 페이지
# ══════════════════════════════════════════════

def _render_queue_page() -> None:
    hub_name    = st.session_state.get("start_node", "")
    _optimizing = st.session_state.get("_opt_in_progress", False)



    if st.session_state.targets:
        render_risk_screening()
        render_pre_dispatch_forecast(ANTHROPIC_API_KEY, _call_anthropic)

    col_btn, _ = st.columns([4, 1])
    with col_btn:
        total_v = (st.session_state.cfg_1t_cnt + st.session_state.cfg_2t_cnt
                   + st.session_state.cfg_5t_cnt)
        if hub_name and st.session_state.targets:
            if total_v == 0:
                st.markdown("""
<div style="class=\"lt-status-warn\"">
  ⚠️ <b>차량 대수가 0대</b>입니다. 사이드바에서 차량을 1대 이상 설정해주세요.
</div>""", unsafe_allow_html=True)
            elif st.session_state.get("_opt_in_progress"):
                st.info('⏳ 경로를 계산하고 있습니다...')
            else:
                st.button(
                    f"🚀 배차 최적화 시작 — {len(st.session_state.targets)}개 배송지 / {total_v}대",
                    type="primary", use_container_width=True,
                    on_click=lambda: st.session_state.update(_run_opt=True, _opt_in_progress=True),
                    help="Kakao Mobility 실측 경로로 최적 배차를 계산합니다. 배송지 수에 따라 30초~2분 소요됩니다.",
                )
        elif not hub_name:
            st.info('👈 사이드바 STEP 1에서 거점을 등록하고, STEP 2에서 허브와 배송지를 선택하면 버튼이 활성화됩니다.')
        else:
            st.info('📋 사이드바 STEP 2에서 배송지를 선택해 추가하면 버튼이 활성화됩니다.')

    st.subheader("📋 배차 대기열")
    render_bulk_upload(st.session_state.db_data)

    if st.session_state.targets:
        try:    sh, sm = map(int, st.session_state.cfg_start_time.split(":"))
        except: sh, sm = 9, 0
        so = sh * 60 + sm

        _base_tw_opts = ["09:00~18:00","09:00~13:00","13:00~18:00","00:00~23:59","07:00~15:00"]
        _existing = list({t["tw_disp"] for t in st.session_state.targets
                          if t.get("tw_disp") and t["tw_disp"] not in _base_tw_opts})
        tw_opts = _base_tw_opts + _existing

        nc: dict[str, int] = {}
        for t in st.session_state.targets: nc[t["name"]] = nc.get(t["name"], 0) + 1
        dup = [n for n, c in nc.items() if c > 1]
        if dup: st.warning(f"⚠️ 중복 배송지: {', '.join(dup)}")

        # ── 즉시 삭제 (멀티셀렉트) ──────────────────────────────────────
        target_names = [t["name"] for t in st.session_state.targets]
        del_sel = st.multiselect(
            "배송지 삭제",
            target_names,
            placeholder="삭제할 배송지를 선택하세요",
            key="_del_targets_sel",
        )
        if del_sel and st.button(
            f"🗑️ 선택 {len(del_sel)}개 삭제",
            type="primary",
            key="_del_targets_btn",
            disabled=_optimizing,
        ):
            st.session_state.targets    = [t for t in st.session_state.targets if t["name"] not in del_sel]
            st.session_state.opt_result = None
            st.rerun()

        df_edit = pd.DataFrame([{
            "거점": t["name"], "약속시간": t.get("tw_disp", "09:00~18:00"),
            "제약유형": t.get("tw_type", "Hard"), "우선순위": t.get("priority", "일반"),
            "온도": t.get("temperature", "상온"), "무게(kg)": t.get("weight", 0),
            "부피(CBM)": t.get("volume", 0), "하차방식": t.get("unload_method", "수작업"),
            "난이도": t.get("difficulty", "일반 (+0분)"), "메모": t.get("memo", ""),
        } for t in st.session_state.targets])

        edited = st.data_editor(df_edit, column_config={
            "거점":     st.column_config.TextColumn("거점", disabled=True),
            "약속시간": st.column_config.SelectboxColumn("배송 시간대", options=tw_opts),
            "제약유형": st.column_config.SelectboxColumn("시간 준수", options=["Hard","Soft"]),
            "우선순위": st.column_config.SelectboxColumn("우선순위", options=["VIP","일반","여유"]),
            "온도":     st.column_config.SelectboxColumn("온도 조건", options=["상온","냉장","냉동"]),
            "무게(kg)": st.column_config.NumberColumn("무게(kg)", min_value=0.1),
            "부피(CBM)":st.column_config.NumberColumn("부피(CBM)", min_value=0.01),
            "하차방식": st.column_config.SelectboxColumn("하차방식", options=["수작업","지게차"]),
            "난이도":   st.column_config.SelectboxColumn("진입 난이도",
                          options=["일반 (+0분)","보안아파트 (+10분)","재래시장 (+15분)"]),
        }, hide_index=True, use_container_width=True)

        if st.button("✅ 변경사항 저장", use_container_width=True, disabled=_optimizing):
            # 거점 이름 → 원본 target dict 매핑 (인덱스 의존 제거)
            tgt_map = {t["name"]: t for t in st.session_state.targets}
            new_tgts: list[dict] = []
            for _, row in edited.reset_index(drop=True).iterrows():
                name = str(row["거점"]).strip()
                if name not in tgt_map:
                    continue
                t   = tgt_map[name].copy()
                twd = str(row.get("약속시간", "09:00~18:00")).strip()
                t.update({
                    "weight":       float(row["무게(kg)"]),
                    "volume":       float(row["부피(CBM)"]),
                    "difficulty":   str(row.get("난이도",   "일반 (+0분)")).strip(),
                    "temperature":  str(row.get("온도",     "상온")).strip(),
                    "unload_method":str(row.get("하차방식", "수작업")).strip(),
                    "priority":     str(row.get("우선순위", "일반")).strip(),
                    "tw_type":      str(row.get("제약유형", "Hard")).strip(),
                    "memo":         str(row.get("메모",     "")).strip(),
                })
                try:
                    ts, te = twd.split("~")
                    t["tw_start"] = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
                    t["tw_end"]   = max(t["tw_start"] + 1, int(te[:2]) * 60 + int(te[3:]) - so)
                    t["tw_disp"]  = twd
                except (ValueError, IndexError):
                    logger.warning("tw_disp 파싱 실패: %s", twd)
                new_tgts.append(t)
            st.session_state.targets    = new_tgts
            st.session_state.opt_result = None
            st.rerun()

    if st.session_state.pop("_run_opt", False):
        try:
            run_optimization(hub_name, db, api_cache, KAKAO_API_KEY)
        finally:
            st.session_state._opt_in_progress = False


# ══════════════════════════════════════════════
# 결과 페이지
# ══════════════════════════════════════════════

def _render_result_page() -> None:
    res = st.session_state.opt_result

    render_learning_warning(res)
    render_run_trend(res)

    unassigned = res.get("unassigned", [])
    if unassigned:
        diag = [f"• **{d['name']}** — {d['reason']}"
                for d in res.get("unassigned_diagnosed", [])]
        st.warning(f"⚠️ {len(unassigned)}건 미배차\n\n" + "\n".join(diag))

    st.markdown("### 🤖 AI 배차 분석")
    with st.spinner("AI가 결과를 분석하고 있습니다..."):
        commentary = generate_ai_commentary(res)
    st.info(commentary)

    render_dashboard(res)
    st.divider()

    render_cluster_analysis(res)
    render_driver_equity(res)

    with st.expander("😓 기사 피로도 지수", expanded=False):
        rows = [
            {"차량": truck, "피로도": fatigue_label(calc_fatigue(stat)),
             "운행(분)": int(stat.get("time", 0)),
             "적재율(%)": round(stat.get("used_wt", 0) / max(stat.get("max_wt", 1), 1) * 100, 1),
             "대기(분)": int(stat.get("wait_time", 0))}
            for truck, stat in res.get("truck_stats", {}).items()
        ]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.caption("🟢 양호(<60) · 🟡 주의(60~79) · 🔴 위험(80+)")

    with st.expander("🌱 탄소 절감 인증", expanded=False):
        cs = _calc_carbon_saving(res)
        c1, c2, c3 = st.columns(3)
        c1.metric("절감 거리", f"{cs['saved_km']}km", f"-{cs['pct']}%")
        c2.metric("절감 CO₂",  f"{cs['saved_co2']}kg")
        c3.metric("나무 환산", f"≈{cs['trees']}그루·년")

    dh = _detect_deadhead(res)
    if dh:
        with st.expander(f"🚛 공차 낭비 구간 ({len(dh)}건)", expanded=False):
            for d in dh: st.warning(d["msg"])

    render_scenario_panel(res)

    st.markdown("#### 📥 운행지시서")
    render_dispatch_sheet(res)
    st.divider()

    hub_loc = res.get("hub_loc") or next(
        (l for l in st.session_state.db_data if l["name"] == st.session_state.start_node),
        st.session_state.db_data[0] if st.session_state.db_data else None,
    )
    if hub_loc is None:
        st.error("❌ 허브 위치 정보를 찾을 수 없습니다."); return

    col1, col2 = st.columns([1.2, 2])
    with col1: render_report(res, hub_loc)
    with col2: render_map(res, hub_loc)


# ══════════════════════════════════════════════
# 앱 진입점
# ══════════════════════════════════════════════

render_sidebar(db, KAKAO_API_KEY)

# 라이트 모드 사용자 안내
# Streamlit이 html[data-theme] 속성을 주입하므로 JS로 감지
st.markdown("""
<div id="lt-theme-notice" style="display:none;
  background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;
  padding:10px 16px;margin-bottom:12px;font-size:.82rem;color:#93c5fd;
  display:flex;align-items:center;gap:10px;">
  🌙 이 앱은 <b style="color:#f0f6ff;">다크 모드</b>에 최적화되어 있습니다.
  &nbsp;오른쪽 상단 메뉴(⋮) → Settings → Theme → <b style="color:#f0f6ff;">Dark</b>으로 변경하시면 더 잘 보입니다.
</div>
<script>
  // 라이트 모드일 때만 안내 표시
  (function() {
    var theme = document.documentElement.getAttribute('data-theme');
    if (theme === 'light') {
      var el = document.getElementById('lt-theme-notice');
      if (el) el.style.display = 'flex';
    }
  })();
</script>
""", unsafe_allow_html=True)

# ── 앱 헤더 ─────────────────────────────────────
has_loc     = bool(st.session_state.db_data)
has_hub     = bool(st.session_state.get("start_node"))
has_targets = bool(st.session_state.targets)
has_result  = bool(st.session_state.opt_result)

def _step_html(num, label, state):
    icon = "✓" if state == "done" else str(num)
    return (
        f'<div class="lt-step-item {state}">'
        f'<div class="lt-step-dot">{icon}</div>'
        f'<span class="lt-step-label">{label}</span>'
        f'</div>'
    )

if not has_result:
    st.markdown(
        '<div class="lt-title">'
        '<span class="lt-title-name">🚚 LogiTrack</span>'
        '<span class="lt-title-sub">배차 최적화 시스템</span>'
        '</div>',
        unsafe_allow_html=True,
    )

    s1 = "done"   if has_loc else "active"
    s2 = "done"   if (has_hub and has_targets) else ("active" if has_loc else "")
    s3 = "active" if (has_hub and has_targets) else ""

    st.markdown(
        '<div class="lt-steps">'
        + _step_html(1, "거점 등록",   s1)
        + _step_html(2, "배송지 설정", s2)
        + _step_html(3, "최적화",      s3)
        + '</div>',
        unsafe_allow_html=True,
    )

    if not has_loc:
        st.markdown('<p class="lt-guide">사이드바 <b>STEP 1</b>에서 CSV 파일로 거점을 등록하세요.</p>', unsafe_allow_html=True)
    elif not has_hub:
        st.markdown('<p class="lt-guide">사이드바 <b>STEP 2</b>에서 허브를 선택하고 배송지를 추가하세요.</p>', unsafe_allow_html=True)
    elif has_targets:
        st.markdown(f'<p class="lt-guide lt-guide-success">배송지 {len(st.session_state.targets)}개 준비됨 — 아래 버튼으로 최적화를 시작하세요.</p>', unsafe_allow_html=True)

else:
    st.markdown(
        '<div class="lt-title">'
        '<span class="lt-title-name">🚚 LogiTrack</span>'
        '<span class="lt-title-sub">배차 결과</span>'
        '</div>',
        unsafe_allow_html=True,
    )

if st.session_state.get("_opt_in_progress"):
    st.markdown(
        '<div style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;'
        'padding:14px 18px;font-size:.9rem;color:#93c5fd;margin-top:1rem;">'
        '⏳ <b style="color:#f0f6ff;">경로를 계산하고 있습니다...</b>&nbsp; '
        '잠시 후 자동으로 결과 페이지로 이동합니다. 이 페이지를 조작하지 마세요.'
        '</div>',
        unsafe_allow_html=True,
    )
    _render_queue_page()
elif not st.session_state.opt_result:
    _render_queue_page()
else:
    _render_result_page()
