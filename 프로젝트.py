"""
프로젝트.py — LogiTrack 배차 최적화 시스템 (4차 차별화 개선판)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[기존 D1~D7 유지]
  D1. AI 코멘터리 (Anthropic API)
  D2. 시나리오 비교 슬라이더
  D3. 기사 피로도 지수
  D4. 학습형 경고
  D5. 원클릭 운행지시서
  D6. 탄소 절감 인증
  D7. 공차 구간 감지

[4차 신규 차별화 — 경쟁사 완전 공백 영역]

       매 최적화 실행 결과를 세션에 누적해서 오늘 몇 번 돌렸고
       총비용·총거리·평균 SLA가 어떻게 변했는지 인라인 차트로 표시.
       "오전 배차보다 오후 배차가 왜 더 비쌌나?" 즉시 확인 가능.
       경쟁사는 단일 결과만 저장, 당일 비교 누적 없음.

  D11. 배송지 위험도 사전 스크리닝
       최적화 실행 전에 각 배송지의 "배차 실패 위험도"를 0~100으로
       계산해서 대기열 화면에 표시. 위험 요인: 시간창 너무 좁음,
       무게가 가용 차량 최대 적재량 대비 90% 이상, 온도 조건이
       현재 투입 차량과 미스매치, 난이도 높음 + VIP 조합 등.
       미리 경고해서 관리자가 재설정하게 유도.
       경쟁사는 최적화 실패 후에야 원인 제공.

  D12. 엑셀 일괄 업로드 + 자동 검증
       배송지를 CSV/엑셀 형식으로 붙여넣기(paste)하면
       자동으로 파싱·검증·시간창 변환·중복 제거까지 완료.
       컬럼명 자동 매핑(이름|거점|주소 → name, 무게|kg → weight 등).
       경쟁사는 엑셀 업로드 지원하지만 검증 없이 실패함.

[5차 신규 차별화 — 경쟁사 완전 공백 영역]

  D13. 배차 전 AI SLA·비용 예보 (Pre-Dispatch Forecast)
       최적화 실행 전, 현재 배송지 목록·차량 구성·설정만 보고
       AI가 "이 조건으로 돌리면 SLA 약 XX%, 예상 비용 XXX만원"을
       30초 안에 미리 알려주는 사전 예보 패널.
       관리자가 최적화를 돌리기 전에 설정을 조정할 수 있게 해줌.
       루티·스마트로 등 경쟁사는 실행 후에만 결과 제공 — 사전 예보 전무.

  D14. 기사별 수익 공정성 지수 (Driver Equity Index)
       배차 결과를 기사(차량) 간에 "얼마나 공정하게 나눴는지" 0~100 점수로 환산.
       운행 거리 편차, 정지 횟수 편차, 피로도 편차를 종합 계산.
       "T1은 너무 몰리고 T2는 노는" 불균형을 수치와 시각적 바 차트로 즉시 파악.
       균등 배차 논문(한국정보통신학회, 2025)에서 지적된 공백 — 국내 배차 SaaS 미지원.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import sys, os, html, json, math, io, re, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio, logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TypedDict, Any

import nest_asyncio
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from db import DBManager
from geo import LRUCache, DIESEL_EMISSION_FACTOR, get_dynamic_fuel_consumption
from routing import build_real_time_matrix
from solver import solve_vrptw, calc_nn_distance_real, diagnose_unassigned

try:
    from ui.sidebar import render_sidebar
    from ui.dashboard import render_dashboard
    from ui.map_view import render_report, render_map
except ModuleNotFoundError:
    from sidebar import render_sidebar
    from dashboard import render_dashboard
    from map_view import render_report, render_map

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("logitrack")
nest_asyncio.apply()

st.set_page_config(page_title="LogiTrack — 배차 최적화 시스템",
                   layout="wide", page_icon="🚚")

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path, override=True)


# ── API 키 ───────────────────────────────────────────────────────────
@st.cache_resource
def _get_kakao_key()     -> str | None:
    try:    return st.secrets["KAKAO_API_KEY"]
    except: return os.getenv("KAKAO_API_KEY")

@st.cache_resource
def _get_db_url()        -> str | None:
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
def _get_db()        -> DBManager: return DBManager(SUPABASE_DB_URL)

@st.cache_resource
def _run_startup_purge() -> bool:
    try:    _get_db().purge_old_route_cache(ttl_hours=12); logger.info("Purge OK.")
    except Exception as e: logger.warning("Purge failed: %s", e)
    return True

db        = _get_db()
api_cache = _get_api_cache()
_run_startup_purge()


# ── 세션 초기화 ──────────────────────────────────────────────────────
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
    # D10: 누적 실행 기록
    "_run_log": [],
    # D13: 사전 예보 캐시
    "_forecast_cache": None,
    "_forecast_key": "",
}

def init_session() -> None:
    for k, v in _SESSION_DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session()
if not st.session_state.db_data:
    st.session_state.db_data = db.load_locations()

st.markdown("""
<style>
/* ── Google Fonts ───────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;600&display=swap');

/* ── 디자인 토큰 ─────────────────────────────── */
:root {
  --bg:        #0f1117;
  --surface:   #1a1d27;
  --surface2:  #22263a;
  --border:    #2d3250;
  --accent:    #4f8ef7;
  --accent-dk: #2563eb;
  --green:     #22c55e;
  --amber:     #f59e0b;
  --red:       #ef4444;
  --text:      #e2e8f0;
  --muted:     #94a3b8;
  --radius:    12px;
  --radius-sm: 7px;
  --font:      'IBM Plex Sans KR', -apple-system, sans-serif;
  --mono:      'IBM Plex Mono', monospace;
}

/* ── 전역 ────────────────────────────────────── */
html, body,
[data-testid="stApp"],
[data-testid="stAppViewContainer"] > .main {
  background: var(--bg) !important;
  color: var(--text) !important;
  font-family: var(--font) !important;
}
section[data-testid="stSidebar"] {
  background: var(--surface) !important;
  border-right: 1px solid var(--border) !important;
}
section[data-testid="stSidebar"] * { color: var(--text) !important; }
[data-testid="stSidebar"] input,
[data-testid="stSidebar"] select,
[data-testid="stSidebar"] textarea {
  background: var(--surface2) !important;
  border: 1px solid var(--border) !important;
  color: var(--text) !important;
  border-radius: var(--radius-sm) !important;
}

/* ── 타이포그래피 ────────────────────────────── */
h1 {
  font-size: 1.65rem !important;
  font-weight: 700 !important;
  letter-spacing: -0.02em !important;
  color: var(--text) !important;
  margin-bottom: 0 !important;
}
h2, h3 {
  font-weight: 600 !important;
  color: var(--text) !important;
}
.stCaption, [data-testid="stCaptionContainer"] {
  color: var(--muted) !important;
  font-size: 0.82rem !important;
}

/* ── 메트릭 카드 ─────────────────────────────── */
[data-testid="stMetric"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important;
  padding: 14px 18px !important;
}
[data-testid="stMetricValue"] {
  font-size: 1.6rem !important;
  font-weight: 700 !important;
  color: var(--accent) !important;
  font-family: var(--mono) !important;
}
[data-testid="stMetricLabel"] {
  font-size: 0.78rem !important;
  color: var(--muted) !important;
  font-weight: 500 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.06em !important;
}
[data-testid="stMetricDelta"] {
  font-size: 0.8rem !important;
}

/* ── 버튼 ────────────────────────────────────── */
.stButton > button {
  background: var(--surface2) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  color: var(--text) !important;
  font-family: var(--font) !important;
  font-weight: 600 !important;
  font-size: 0.85rem !important;
  transition: all 0.15s ease !important;
}
.stButton > button:hover {
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  background: rgba(79,142,247,0.08) !important;
}
.stButton > button[kind="primary"] {
  background: var(--accent-dk) !important;
  border-color: var(--accent-dk) !important;
  color: #fff !important;
}
.stButton > button[kind="primary"]:hover {
  background: var(--accent) !important;
  border-color: var(--accent) !important;
  color: #fff !important;
}

/* ── 입력 요소 ───────────────────────────────── */
input, select, textarea,
[data-baseweb="input"] input,
[data-baseweb="select"] div,
[data-baseweb="textarea"] textarea {
  background: var(--surface2) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius-sm) !important;
  color: var(--text) !important;
  font-family: var(--font) !important;
}
[data-baseweb="select"] > div {
  background: var(--surface2) !important;
  border-color: var(--border) !important;
}

/* ── expander ────────────────────────────────── */
[data-testid="stExpander"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important;
  margin-bottom: 10px !important;
}
[data-testid="stExpander"] summary {
  font-weight: 600 !important;
  font-size: 0.9rem !important;
  padding: 12px 16px !important;
  color: var(--text) !important;
}
[data-testid="stExpander"] summary:hover {
  color: var(--accent) !important;
  background: rgba(79,142,247,0.05) !important;
  border-radius: var(--radius) var(--radius) 0 0 !important;
}

/* ── 탭 ──────────────────────────────────────── */
[data-testid="stTabs"] [role="tablist"] {
  background: var(--surface) !important;
  border-bottom: 1px solid var(--border) !important;
  border-radius: var(--radius) var(--radius) 0 0 !important;
  gap: 2px !important;
  padding: 4px 8px 0 !important;
}
[data-testid="stTabs"] [role="tab"] {
  background: transparent !important;
  border: none !important;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0 !important;
  color: var(--muted) !important;
  font-size: 0.82rem !important;
  font-weight: 600 !important;
  padding: 8px 14px !important;
  transition: all 0.12s !important;
}
[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  background: var(--surface2) !important;
  color: var(--accent) !important;
  border-bottom: 2px solid var(--accent) !important;
}
[data-testid="stTabs"] [role="tabpanel"] {
  background: var(--surface) !important;
  border: 1px solid var(--border) !important;
  border-top: none !important;
  border-radius: 0 0 var(--radius) var(--radius) !important;
  padding: 16px !important;
}

/* ── 데이터프레임 / 테이블 ────────────────────── */
[data-testid="stDataFrame"] iframe,
[data-testid="stTable"] table {
  border-radius: var(--radius-sm) !important;
}
th {
  background: var(--surface2) !important;
  color: var(--muted) !important;
  font-size: 0.75rem !important;
  text-transform: uppercase !important;
  letter-spacing: 0.05em !important;
  text-align: center !important;
  border-bottom: 1px solid var(--border) !important;
}
td { color: var(--text) !important; }

/* ── 알림/메시지 박스 ────────────────────────── */
[data-testid="stAlert"] {
  border-radius: var(--radius-sm) !important;
  border-left-width: 4px !important;
  font-size: 0.88rem !important;
}
div[data-testid="stToast"] {
  background: var(--surface2) !important;
  border: 1px solid var(--border) !important;
  border-left: 4px solid var(--accent) !important;
  border-radius: var(--radius-sm) !important;
  color: var(--text) !important;
}

/* ── progress bar ────────────────────────────── */
[data-testid="stProgressBar"] > div > div {
  background: var(--accent) !important;
  border-radius: 99px !important;
}
[data-testid="stProgressBar"] > div {
  background: var(--surface2) !important;
  border-radius: 99px !important;
  height: 8px !important;
}

/* ── divider ─────────────────────────────────── */
hr { border-color: var(--border) !important; }

/* ── 위험도·공정성 커스텀 클래스 ─────────────── */
.risk-high { color: var(--red)   !important; font-weight: 700 !important; }
.risk-mid  { color: var(--amber) !important; font-weight: 700 !important; }
.risk-low  { color: var(--green) !important; font-weight: 700 !important; }

/* ── 운행 요약 카드 (dashboard용) ────────────── */
.truck-card {
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 18px;
  margin-bottom: 10px;
  line-height: 1.7;
}
.truck-card-label {
  font-size: 0.72rem;
  font-weight: 700;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.truck-card-value {
  font-family: var(--mono);
  font-size: 0.9rem;
  color: var(--text);
}

/* ── 앱 타이틀 배지 ─────────────────────────── */
.lt-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 6px;
}
.lt-badge {
  background: var(--accent-dk);
  color: #fff;
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.1em;
  padding: 3px 8px;
  border-radius: 99px;
  text-transform: uppercase;
}

/* ── 체크박스 ────────────────────────────────── */
[data-baseweb="checkbox"] label span { color: var(--text) !important; }

/* ── slider ──────────────────────────────────── */
[data-testid="stSlider"] [data-testid="stTickBar"] {
  color: var(--muted) !important;
}
</style>
""", unsafe_allow_html=True)

_TRUCK_COLORS = ["#1E3A8A","#DC2626","#059669","#D97706",
                 "#7C3AED","#0891B2","#BE185D","#92400E","#065F46","#1D4ED8"]


# ══════════════════════════════════════════════════════════════════════
# 공통 Anthropic 호출 헬퍼
# ══════════════════════════════════════════════════════════════════════
def _call_anthropic(messages: list[dict], system: str = "", max_tokens: int = 500) -> str:
    """Anthropic API 단일 호출. 실패 시 빈 문자열 반환."""
    if not ANTHROPIC_API_KEY:
        return ""
    import urllib.request
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
    except Exception as e:
        logger.warning("Anthropic 호출 실패: %s", e)
        return ""


# ══════════════════════════════════════════════════════════════════════
# D1. AI 코멘터리
# ══════════════════════════════════════════════════════════════════════
def _fallback_commentary(res: dict) -> str:
    msgs = []
    sla  = res.get("sla", 100);  eff = res.get("efficiency", 0)
    late = res.get("late_count", 0); una = len(res.get("unassigned", []))
    wait = res.get("wait_time_total", 0)
    if sla >= 95:  msgs.append(f"SLA {sla}%로 정시 배송 목표를 달성했습니다.")
    elif sla >= 80: msgs.append(f"SLA {sla}%로 양호하나 {late}건의 지연이 발생했습니다.")
    else:           msgs.append(f"⚠️ SLA {sla}%로 개선 필요 — 증차 또는 시간창 조정을 검토하세요.")
    if eff > 10:   msgs.append(f"경로 효율 +{eff}%로 유의미한 거리를 절감했습니다.")
    if una > 0:    msgs.append(f"{una}건 미배차 — 차량 용량·시간창 완화를 검토하세요.")
    if wait > 60:  msgs.append(f"누적 대기 {int(wait)}분 — 출발 시간 조정을 권장합니다.")
    return " ".join(msgs)

def generate_ai_commentary(res: dict) -> str:
    summary = {
        "총경로수": len(res.get("routes", [])), "총거리km": round(res.get("dist", 0), 1),
        "SLA": res.get("sla", 100), "효율": res.get("efficiency", 0),
        "미배차": len(res.get("unassigned", [])), "지연": res.get("late_count", 0),
        "대기분": round(res.get("wait_time_total", 0), 1),
        "총비용": int(res.get("total_cost", 0)),
    }
    result = _call_anthropic(
        messages=[{"role": "user", "content":
            f"물류 배차 결과를 현장 관리자에게 3~5문장 한국어 평문으로 설명하세요. "
            f"잘된 점·개선점 모두 포함, 수치 인용 필수.\n\n결과: {json.dumps(summary, ensure_ascii=False)}"}],
        max_tokens=300,
    )
    return result or _fallback_commentary(res)


# ══════════════════════════════════════════════════════════════════════
# D3. 기사 피로도
# ══════════════════════════════════════════════════════════════════════
def _calc_fatigue(stat: dict) -> float:
    t = min(50.0, (stat.get("time", 0) / 600) * 50)
    l = min(30.0, (stat.get("used_wt", 0) / max(stat.get("max_wt", 1), 1)) * 30)
    w = min(20.0, (stat.get("wait_time", 0) / 120) * 20)
    return round(t + l + w, 1)

def _fatigue_label(s: float) -> str:
    return f"🔴 위험({s})" if s >= 80 else f"🟡 주의({s})" if s >= 60 else f"🟢 양호({s})"


# ══════════════════════════════════════════════════════════════════════
# D6. 탄소 절감
# ══════════════════════════════════════════════════════════════════════
def _calc_carbon_saving(res: dict) -> dict:
    saved_km  = max(0.0, res.get("nn_real_dist", res.get("dist", 0)) - res.get("dist", 0))
    saved_co2 = round(saved_km / 12.0 * DIESEL_EMISSION_FACTOR, 2)
    nn_d      = res.get("nn_real_dist", 1)
    return {"saved_km": round(saved_km, 1),
            "saved_co2": saved_co2,
            "pct": round(saved_km / nn_d * 100, 1) if nn_d > 0 else 0.0,
            "trees": round(saved_co2 / 22, 1)}


# ══════════════════════════════════════════════════════════════════════
# D7. 공차 구간
# ══════════════════════════════════════════════════════════════════════
def _detect_deadhead(res: dict) -> list[dict]:
    out = []
    for row in res.get("report", []):
        if "(복귀)" not in row.get("거점", ""): continue
        try:
            d = float(row.get("거리", "0").replace("km","").replace("⚠️","").strip())
            if d >= 30:
                out.append({"truck": row["트럭"], "km": d,
                            "msg": f"{row['트럭']}: 복귀 {d:.1f}km — 귀로 픽업 또는 인근 환적 고려"})
        except ValueError: pass
    return out


# ══════════════════════════════════════════════════════════════════════
# D10. 일별 비용 트렌드 누적 추적기
# ══════════════════════════════════════════════════════════════════════
def _log_run(res: dict) -> None:
    """최적화 실행 결과를 세션 로그에 기록"""
    entry = {
        "시각":   datetime.now().strftime("%H:%M"),
        "총비용": int(res.get("total_cost", 0)),
        "총거리": round(res.get("dist", 0), 1),
        "SLA":    res.get("sla", 100),
        "효율":   res.get("efficiency", 0),
        "배송지": sum(v.get("stops", 0) for v in res.get("truck_stats", {}).values()),
        "미배차": len(res.get("unassigned", [])),
    }
    st.session_state._run_log.append(entry)
    # 당일 최대 50회 보관
    if len(st.session_state._run_log) > 50:
        st.session_state._run_log = st.session_state._run_log[-50:]


def render_run_trend(res: dict) -> None:
    """[D10] 당일 누적 실행 트렌드 패널"""
    log = st.session_state._run_log
    if len(log) < 2:
        return  # 2회 이상 실행해야 의미 있음

    with st.expander(f"📈 오늘 실행 트렌드 ({len(log)}회)", expanded=False):
        st.caption("오늘 실행한 최적화 결과를 비교합니다.")
        df = pd.DataFrame(log)

        col1, col2, col3 = st.columns(3)
        col1.metric("최저 비용",   f"₩{df['총비용'].min():,}",
                    f"최고 ₩{df['총비용'].max():,} 대비")
        col2.metric("최고 SLA",    f"{df['SLA'].max():.1f}%",
                    f"최저 {df['SLA'].min():.1f}%")
        col3.metric("총 실행 횟수", f"{len(log)}회",
                    f"미배차 최소 {df['미배차'].min()}건")

        # 실행별 비교 테이블
        st.dataframe(
            df.rename(columns={
                "시각":"실행시각", "총비용":"총비용(₩)", "총거리":"거리(km)",
                "SLA":"SLA(%)", "효율":"효율(%)", "배송지":"배송건수", "미배차":"미배차"
            }),
            hide_index=True, use_container_width=True
        )

        # 비용·SLA 추이 차트
        if len(log) >= 3:
            chart_df = pd.DataFrame(log[-10:]).set_index("시각")[["총비용", "SLA"]]
            c_ch1, c_ch2 = st.columns(2)
            with c_ch1:
                st.caption("💰 비용 추이")
                st.line_chart(chart_df[["총비용"]], height=120, use_container_width=True)
            with c_ch2:
                st.caption("📊 SLA 추이")
                st.line_chart(chart_df[["SLA"]], height=120, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# D11. 배송지 위험도 사전 스크리닝
# ══════════════════════════════════════════════════════════════════════
def _calc_risk(target: dict, max_1t: int, max_2t: int, max_5t: int,
               n_1t: int, n_2t: int, n_5t: int) -> tuple[int, list[str]]:
    """
    배차 실패 위험도 0~100 및 원인 목록 반환.
    빠른 계산이라 실제 솔버 결과와 다를 수 있음.
    """
    score = 0
    reasons: list[str] = []

    weight   = target.get("weight", 0)
    temp     = target.get("temperature", "상온")
    tw_disp  = target.get("tw_disp", "00:00~23:59")
    priority = target.get("priority", "일반")
    diff     = target.get("difficulty", "일반")

    # ① 무게 — 최대 가용 차량 적재량 대비
    max_cap = 0
    if n_1t > 0: max_cap = max(max_cap, 1000)
    if n_2t > 0: max_cap = max(max_cap, 2500)
    if n_5t > 0: max_cap = max(max_cap, 5000)
    if max_cap > 0:
        ratio = weight / max_cap
        if ratio > 0.95:
            score += 40; reasons.append(f"무게 {weight}kg — 최대 적재 {max_cap}kg의 {ratio*100:.0f}%")
        elif ratio > 0.80:
            score += 20; reasons.append(f"무게 {weight}kg — 적재율 주의")

    # ② 온도 — 냉장·냉동은 1톤 냉탑만 처리 가능
    if temp in ("냉장", "냉동"):
        if n_1t == 0:
            score += 50; reasons.append(f"{temp} 화물이지만 1톤 냉탑 차량 없음")
        elif weight > 1000:
            score += 30; reasons.append(f"{temp} 화물 {weight}kg — 냉탑 용량 초과 가능성")

    # ③ 시간창 — 3시간 미만이면 위험
    try:
        ts, te = tw_disp.split("~")
        tw_min = (int(te[:2]) - int(ts[:2])) * 60 + (int(te[3:]) - int(ts[3:]))
        if tw_min < 120:
            score += 30; reasons.append(f"시간창 {tw_min}분으로 매우 좁음")
        elif tw_min < 180:
            score += 15; reasons.append(f"시간창 {tw_min}분으로 다소 좁음")
    except (ValueError, IndexError):
        pass

    # ④ VIP + 난이도 높음 조합
    if priority == "VIP" and "재래시장" in diff:
        score += 20; reasons.append("VIP + 재래시장 진입 — 지연 위험 높음")

    return min(score, 100), reasons


def render_risk_screening() -> None:
    """[D11] 배차 대기열에서 위험도 사전 스크리닝 표시"""
    targets = st.session_state.targets
    if not targets:
        return

    n1 = st.session_state.cfg_1t_cnt
    n2 = st.session_state.cfg_2t_cnt
    n5 = st.session_state.cfg_5t_cnt

    has_risk = False
    risk_rows = []
    for t in targets:
        score, reasons = _calc_risk(t, 1000, 2500, 5000, n1, n2, n5)
        if score >= 20:
            has_risk = True
        label = ("🔴 위험" if score >= 60 else "🟡 주의" if score >= 30 else "🟢 정상")
        risk_rows.append({
            "배송지":  t["name"],
            "위험도":  f"{label} ({score})",
            "원인":    " / ".join(reasons) if reasons else "—",
        })

    if not has_risk:
        return  # 모두 정상이면 패널 숨김

    with st.expander(
        f"⚠️ 배차 위험 사전 스크리닝 — {sum(1 for r in risk_rows if '🔴' in r['위험도'] or '🟡' in r['위험도'])}건 주의",
        expanded=True
    ):
        st.caption(
            "최적화 실행 전 배차 실패 가능성이 높은 배송지를 미리 알려드립니다. "
            "위험도가 높은 항목은 조건을 수정하거나 차량 구성을 바꾸세요."
        )
        filtered = [r for r in risk_rows if "🔴" in r["위험도"] or "🟡" in r["위험도"]]
        st.dataframe(pd.DataFrame(filtered), hide_index=True, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# D12. 엑셀/CSV 일괄 업로드 + 자동 검증
# ══════════════════════════════════════════════════════════════════════
# 컬럼명 자동 매핑 딕셔너리
_COL_ALIASES: dict[str, list[str]] = {
    "name":         ["이름", "거점", "배송지", "주소", "거점명", "name", "배송처"],
    "weight":       ["무게", "kg", "무게(kg)", "weight", "중량"],
    "volume":       ["부피", "cbm", "부피(cbm)", "volume", "용적"],
    "tw_disp":      ["시간대", "약속시간", "배송시간", "시간창", "tw_disp", "time_window"],
    "temperature":  ["온도", "온도조건", "temperature", "냉장여부"],
    "priority":     ["우선순위", "priority", "vip"],
    "unload_method":["하차방식", "하차", "unload_method"],
    "difficulty":   ["난이도", "진입난이도", "difficulty"],
    "memo":         ["메모", "비고", "memo", "note"],
}

def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    """실제 컬럼명 → 내부 키 매핑 반환"""
    mapping: dict[str, str] = {}
    cols_lower = {c.lower().replace(" ", ""): c for c in df.columns}
    for internal_key, aliases in _COL_ALIASES.items():
        for alias in aliases:
            normalized = alias.lower().replace(" ", "").replace("(", "").replace(")", "")
            if normalized in cols_lower:
                mapping[internal_key] = cols_lower[normalized]
                break
    return mapping


def _parse_tw(tw_str: str, default: str = "09:00~18:00") -> str:
    """다양한 시간창 형식을 HH:MM~HH:MM으로 정규화"""
    if not tw_str or str(tw_str).strip() in ("", "nan", "None"):
        return default
    s = str(tw_str).strip()
    # 이미 올바른 형식
    if re.match(r"^\d{2}:\d{2}~\d{2}:\d{2}$", s):
        return s
    # 09:00-18:00 (하이픈)
    m = re.match(r"(\d{1,2}):(\d{2})[-~](\d{1,2}):(\d{2})", s)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}~{int(m.group(3)):02d}:{m.group(4)}"
    # 9시~18시 or 09시00분~18시00분
    m = re.match(r"(\d{1,2})시?(\d{0,2})분?[-~](\d{1,2})시?(\d{0,2})분?", s)
    if m:
        sh, sm = int(m.group(1)), int(m.group(2) or 0)
        eh, em = int(m.group(3)), int(m.group(4) or 0)
        return f"{sh:02d}:{sm:02d}~{eh:02d}:{em:02d}"
    return default


def render_bulk_upload(db_data: list[dict]) -> None:
    """[D12] 엑셀/CSV 일괄 업로드 + 자동 검증 패널"""
    with st.expander("📥 배송지 일괄 업로드 (CSV / 엑셀 붙여넣기)", expanded=False):
        st.caption(
            "CSV 파일을 업로드하거나 아래에 직접 붙여넣으세요. "
            "컬럼명이 달라도 자동으로 매핑합니다."
        )

        tab_file, tab_paste = st.tabs(["📂 파일 업로드", "📋 텍스트 붙여넣기"])

        raw_df: pd.DataFrame | None = None

        with tab_file:
            uploaded = st.file_uploader("CSV 또는 XLSX 파일", type=["csv", "xlsx"],
                                        key="bulk_upload_file")
            if uploaded:
                try:
                    if uploaded.name.endswith(".xlsx"):
                        raw_df = pd.read_excel(uploaded, dtype=str)
                    else:
                        raw_df = pd.read_csv(uploaded, dtype=str)
                except Exception as e:
                    st.error(f"파일 읽기 실패: {e}")

        with tab_paste:
            pasted = st.text_area(
                "탭/쉼표로 구분된 데이터 붙여넣기 (첫 행은 헤더)",
                height=150, key="bulk_upload_paste",
                placeholder="이름\t무게\t부피\t시간대\n서울창고A\t200\t1.5\t09:00~13:00"
            )
            if pasted.strip():
                try:
                    sep = "\t" if "\t" in pasted else ","
                    raw_df = pd.read_csv(io.StringIO(pasted), sep=sep, dtype=str)
                except Exception as e:
                    st.error(f"파싱 실패: {e}")

        if raw_df is None or raw_df.empty:
            return

        # 컬럼 매핑
        col_map = _map_columns(raw_df)
        if "name" not in col_map:
            st.error("❌ '거점명/배송지/이름' 컬럼을 찾을 수 없습니다. "
                     "첫 번째 컬럼을 배송지명으로 간주합니다.")
            col_map["name"] = raw_df.columns[0]

        # 변환
        db_names = {d["name"] for d in db_data}
        valid_rows: list[dict] = []
        errors: list[str]      = []
        skipped_dup: list[str] = []

        for idx, row in raw_df.iterrows():
            name = str(row.get(col_map.get("name", ""), "")).strip()
            if not name or name.lower() == "nan":
                errors.append(f"행 {idx+2}: 배송지 이름 없음 — 건너뜀")
                continue
            if name not in db_names:
                errors.append(f"행 {idx+2}: '{name}' — DB에 없는 거점 (주소 등록 필요)")
                continue
            if name in {t["name"] for t in st.session_state.targets}:
                skipped_dup.append(name)
                continue

            def _get(key: str, default: Any = "") -> str:
                col = col_map.get(key)
                return str(row.get(col, default)).strip() if col else str(default)

            # 무게/부피 파싱
            try:    weight = float(_get("weight", 0)) if _get("weight") else 0.0
            except: weight = 0.0
            try:    volume = float(_get("volume", 0)) if _get("volume") else 0.0
            except: volume = 0.0

            # 온도 정규화
            raw_temp = _get("temperature", "상온").replace(" ", "")
            temp = ("냉동" if "냉동" in raw_temp or "frozen" in raw_temp.lower()
                    else "냉장" if "냉장" in raw_temp or "cold" in raw_temp.lower()
                    else "상온")

            # 우선순위 정규화
            raw_pri = _get("priority", "일반")
            priority = ("VIP" if "vip" in raw_pri.lower() or "v" == raw_pri.lower()
                        else "여유" if "여유" in raw_pri
                        else "일반")

            # 시간창
            tw_raw  = _get("tw_disp", "09:00~18:00")
            tw_disp = _parse_tw(tw_raw)

            # 출발 시각 기준 tw_start/tw_end
            try:
                sh, sm = map(int, st.session_state.cfg_start_time.split(":"))
            except ValueError:
                sh, sm = 9, 0
            so = sh * 60 + sm
            try:
                ts, te = tw_disp.split("~")
                tw_start = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
                tw_end   = max(tw_start + 1, int(te[:2]) * 60 + int(te[3:]) - so)
            except (ValueError, IndexError):
                tw_start, tw_end = 0, 540

            # DB에서 좌표 조회
            db_node = next((d for d in db_data if d["name"] == name), None)
            entry   = {**(db_node or {}),
                       "name": name, "weight": weight, "volume": volume,
                       "temperature": temp, "priority": priority,
                       "tw_disp": tw_disp, "tw_start": tw_start, "tw_end": tw_end,
                       "tw_type": "Hard",
                       "unload_method": _get("unload_method", "수작업"),
                       "difficulty":    _get("difficulty", "일반 (+0분)"),
                       "memo":          _get("memo", "")}
            valid_rows.append(entry)

        # 결과 미리보기
        st.markdown(f"**✅ 추가 가능: {len(valid_rows)}건**"
                    + (f"  |  ⚠️ 중복 제외: {len(skipped_dup)}건" if skipped_dup else "")
                    + (f"  |  ❌ 오류: {len(errors)}건" if errors else ""))

        if errors:
            with st.expander(f"❌ 오류 목록 ({len(errors)}건)"):
                for e in errors:
                    st.caption(e)

        if valid_rows:
            preview_df = pd.DataFrame([{
                "배송지": r["name"], "무게(kg)": r["weight"], "부피(CBM)": r["volume"],
                "온도": r["temperature"], "시간대": r["tw_disp"], "우선순위": r["priority"],
            } for r in valid_rows])
            st.dataframe(preview_df, hide_index=True, use_container_width=True)

            if st.button(f"📥 {len(valid_rows)}건 대기열에 추가", type="primary",
                         use_container_width=True, key="bulk_confirm"):
                st.session_state.targets.extend(valid_rows)
                st.session_state.opt_result = None
                st.success(f"✅ {len(valid_rows)}건이 배차 대기열에 추가됐습니다.")
                st.rerun()


# ══════════════════════════════════════════════════════════════════════
# D13. 배차 전 AI SLA·비용 예보 (Pre-Dispatch Forecast)
# ══════════════════════════════════════════════════════════════════════
def _make_forecast_key() -> str:
    """현재 설정 조합을 고유 키로 변환 (캐시 무효화용)"""
    import hashlib
    s = st.session_state
    # 혼잡도(cfg_congestion)도 SLA에 영향 → 키에 포함
    data = (
        len(s.targets),
        s.cfg_1t_cnt, s.cfg_2t_cnt, s.cfg_5t_cnt,
        s.cfg_speed, s.cfg_weather, s.cfg_max_hours,
        s.cfg_start_time, s.cfg_fuel_price, s.cfg_labor,
        s.cfg_congestion,
    )
    return hashlib.md5(str(data).encode()).hexdigest()[:12]


def _rule_based_forecast() -> dict:
    """AI API 없을 때 규칙 기반 예보 (폴백)"""
    s = st.session_state
    targets = s.targets
    n1, n2, n5 = s.cfg_1t_cnt, s.cfg_2t_cnt, s.cfg_5t_cnt
    total_v = n1 + n2 + n5
    n_stops = len(targets)

    if total_v == 0 or n_stops == 0:
        return {}

    # 총 적재 용량 vs 총 무게
    total_cap_kg = n1 * 1000 + n2 * 2500 + n5 * 5000
    total_weight = sum(t.get("weight", 0) for t in targets)
    cap_ratio = total_weight / max(total_cap_kg, 1)

    # 시간창 분석
    tight_tw = sum(
        1 for t in targets
        if (t.get("tw_end", 540) - t.get("tw_start", 0)) < 120
    )
    tight_ratio = tight_tw / max(n_stops, 1)

    # 온도 미스매치
    cold_stops = sum(1 for t in targets if t.get("temperature") in ("냉장", "냉동"))
    temp_mismatch = cold_stops > 0 and n1 == 0

    # SLA 예측
    sla = 95.0
    if cap_ratio > 0.9:  sla -= 20
    elif cap_ratio > 0.7: sla -= 8
    if tight_ratio > 0.3: sla -= 15
    elif tight_ratio > 0.15: sla -= 7
    if temp_mismatch: sla -= 25
    weather = s.cfg_weather
    if "눈" in weather: sla -= 10
    elif "비" in weather: sla -= 5
    # 혼잡도 반영 (40% 기준, 초과 시 SLA 하락)
    congestion_penalty = max(0, (s.cfg_congestion - 40) / 10) * 2
    sla -= congestion_penalty
    stops_per_v = n_stops / total_v
    if stops_per_v > 12: sla -= 10
    elif stops_per_v > 8: sla -= 4
    sla = max(30.0, min(100.0, round(sla, 1)))

    # 비용 예측 (단순 추정)
    # 혼잡도에 따라 평균 구간거리 보정
    congestion_factor = 1 + (s.cfg_congestion - 40) / 200  # 40% 기준
    avg_dist_per_stop = 8 * max(0.8, congestion_factor)   # km (도심 기준)
    # est_dist = 전체 예상 운행 거리 (차량별 합산, 이중 곱 방지)
    est_dist_total = n_stops * avg_dist_per_stop           # 총 배송 구간 거리
    fuel_cost = est_dist_total / 10 * s.cfg_fuel_price
    labor_cost = (s.cfg_max_hours * total_v) * s.cfg_labor
    fixed = n1 * 100000 + n2 * 180000 + n5 * 280000
    total_cost = int(fuel_cost + labor_cost + fixed)

    # 위험 요인 리스트
    risks = []
    if cap_ratio > 0.85:
        risks.append(f"적재 용량 {cap_ratio*100:.0f}% — 차량 추가 또는 배송지 분리 권장")
    if tight_ratio > 0.2:
        risks.append(f"시간창 2시간 미만 배송지 {tight_tw}건 — SLA 리스크 높음")
    if temp_mismatch:
        risks.append(f"냉장·냉동 {cold_stops}건이지만 냉탑 차량(1톤) 없음")
    if stops_per_v > 10:
        risks.append(f"차량 1대당 배송지 {stops_per_v:.1f}개 — 과부하 위험")
    if "눈" in weather:
        risks.append("눈 날씨로 감속 30% — SLA 하락 예상")
    if s.cfg_congestion > 60:
        risks.append(f"혼잡도 {s.cfg_congestion}% — 예상 운행 시간 증가")

    # 개선 제안
    suggestions = []
    if cap_ratio > 0.85:
        suggestions.append("차량 1대 추가 시 SLA 약 +8~12% 예상")
    if tight_ratio > 0.2:
        suggestions.append("시간창 좁은 VIP 배송지를 우선 배차 허브 인근으로 재배치 검토")
    if stops_per_v > 10:
        suggestions.append(f"현재 {total_v}대 → {total_v+1}대로 늘리면 과부하 해소 가능")
    if not suggestions:
        suggestions.append("현재 구성으로 안정적인 배차 가능합니다.")

    return {
        "sla": sla,
        "total_cost": total_cost,
        "est_dist": round(est_dist_total, 1),
        "risks": risks,
        "suggestions": suggestions,
        "cap_ratio": round(cap_ratio * 100, 1),
        "tight_tw": tight_tw,
        "stops_per_v": round(stops_per_v, 1),
        "via_ai": False,
    }


_FORECAST_SYSTEM = """당신은 물류 배차 전문가입니다.
현재 배송지 목록과 차량 구성을 보고 최적화 실행 전에 결과를 예측하세요.
반드시 JSON만 반환하세요 (마크다운 없이). 형식:
{
  "sla": 숫자(0~100),
  "total_cost": 정수(원),
  "est_dist": 숫자(km),
  "risks": ["위험요인1", "위험요인2"],
  "suggestions": ["개선제안1", "개선제안2"],
  "reasoning": "2~3문장 한국어 근거"
}"""


def render_pre_dispatch_forecast() -> None:
    """[D13] 배차 전 AI SLA·비용 예보 패널"""
    targets = st.session_state.targets
    total_v = (st.session_state.cfg_1t_cnt + st.session_state.cfg_2t_cnt
               + st.session_state.cfg_5t_cnt)

    if not targets or total_v == 0:
        return

    with st.expander("🔮 배차 전 AI 예보 — 최적화 실행 전 SLA·비용 미리 보기", expanded=False):
        st.caption(
            "**[D13 신기능]** 최적화를 돌리기 전에 현재 조건으로 예상 SLA와 비용을 AI가 미리 계산합니다. "
            "경쟁사(루티·스마트로 등)는 실행 후에만 결과를 보여주지만, LogiTrack은 **사전 예보**를 제공합니다."
        )

        forecast_key = _make_forecast_key()
        cached = st.session_state.get("_forecast_cache")
        cached_key = st.session_state.get("_forecast_key", "")

        col_run, col_clear = st.columns([3, 1])
        with col_run:
            run_forecast = st.button(
                "🔮 지금 예보 실행",
                use_container_width=True,
                key="forecast_btn",
                help="현재 배송지·차량 설정으로 최적화 결과를 미리 예측합니다.",
            )
        with col_clear:
            if cached and st.button("🗑️ 초기화", use_container_width=True, key="forecast_clear"):
                st.session_state._forecast_cache = None
                st.session_state._forecast_key = ""
                st.rerun()

        # 설정 변경 시 캐시 무효화 안내
        if cached and cached_key != forecast_key:
            st.warning("⚠️ 배송지 또는 차량 설정이 변경됐습니다. 예보를 다시 실행하세요.")

        if run_forecast:
            with st.spinner("AI가 배차 결과를 사전 예측 중..."):
                if ANTHROPIC_API_KEY:
                    s = st.session_state
                    context = {
                        "배송지수": len(targets),
                        "차량구성": {"1톤": s.cfg_1t_cnt, "2.5톤": s.cfg_2t_cnt, "5톤": s.cfg_5t_cnt},
                        "기상": s.cfg_weather,
                        "최대운행시간": s.cfg_max_hours,
                        "출발시간": s.cfg_start_time,
                        "연료단가": s.cfg_fuel_price,
                        "인건비시간당": s.cfg_labor,
                        "배송지요약": [
                            {
                                "온도": t.get("temperature", "상온"),
                                "우선순위": t.get("priority", "일반"),
                                "무게kg": t.get("weight", 0),
                                "시간창분": (t.get("tw_end", 540) - t.get("tw_start", 0)),
                            }
                            for t in targets[:30]  # 토큰 절약
                        ],
                    }
                    raw = _call_anthropic(
                        messages=[{
                            "role": "user",
                            "content": (
                                f"아래 조건으로 배차를 최적화하면 어떤 결과가 나올지 예측하세요.\n"
                                f"{json.dumps(context, ensure_ascii=False)}"
                            ),
                        }],
                        system=_FORECAST_SYSTEM,
                        max_tokens=600,
                    )
                    try:
                        cleaned = raw.replace("```json", "").replace("```", "").strip()
                        fc = json.loads(cleaned)
                        fc["via_ai"] = True
                    except Exception:
                        fc = _rule_based_forecast()
                else:
                    fc = _rule_based_forecast()

            st.session_state._forecast_cache = fc
            st.session_state._forecast_key = forecast_key
            cached = fc

        if not cached:
            st.info("위 버튼을 누르면 최적화 전 예보를 제공합니다.")
            return

        fc = cached
        src = "🤖 AI 예보" if fc.get("via_ai") else "📐 규칙 기반 예보"

        # 메트릭 표시
        m1, m2, m3 = st.columns(3)
        sla = fc.get("sla", 0)
        sla_color = "🟢" if sla >= 90 else "🟡" if sla >= 75 else "🔴"
        m1.metric(f"{sla_color} 예상 SLA", f"{sla:.1f}%")
        m2.metric("💰 예상 총비용", f"₩{fc.get('total_cost', 0):,}")
        m3.metric("🛣️ 예상 총거리", f"{fc.get('est_dist', 0):.0f}km")

        st.caption(f"출처: {src}")

        # 위험 요인
        risks = fc.get("risks", [])
        if risks:
            st.markdown("**⚠️ 위험 요인**")
            for r in risks:
                st.error(f"• {r}")

        # 개선 제안
        suggestions = fc.get("suggestions", [])
        if suggestions:
            st.markdown("**💡 개선 제안**")
            for sg in suggestions:
                st.success(f"• {sg}")

        # AI 근거 (있을 경우)
        reasoning = fc.get("reasoning", "")
        if reasoning:
            st.info(f"🤖 AI 판단: {reasoning}")

        st.caption("※ 예보는 실제 최적화 전 추정치이며, 실행 결과와 다를 수 있습니다.")


# ══════════════════════════════════════════════════════════════════════
# D14. 기사별 수익 공정성 지수 (Driver Equity Index)
# ══════════════════════════════════════════════════════════════════════
def _calc_equity_index(truck_stats: dict) -> dict:
    """
    차량(기사)별 배차 공정성을 0~100으로 환산.
    100에 가까울수록 균등 배분. 0에 가까울수록 심각한 불균형.
    편차 지표: 운행거리, 정지 횟수, 피로도
    """
    if len(truck_stats) < 2:
        return {"index": 100, "detail": [], "worst": ""}

    stats = list(truck_stats.items())

    def _cv(values: list[float]) -> float:
        """변동계수 (표준편차/평균) — 낮을수록 균등"""
        if not values or sum(values) == 0:
            return 0.0
        mean = sum(values) / len(values)
        if mean == 0:
            return 0.0
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        return (variance ** 0.5) / mean

    dists    = [v.get("dist", 0) for _, v in stats]
    stops    = [v.get("stops", 0) for _, v in stats]
    fatigues = [_calc_fatigue(v) for _, v in stats]

    cv_dist    = _cv(dists)
    cv_stops   = _cv(stops)
    cv_fatigue = _cv(fatigues)

    # 가중 평균 (피로도 가중 높임)
    weighted_cv = cv_dist * 0.3 + cv_stops * 0.3 + cv_fatigue * 0.4

    # 0~100 변환 (CV 0 → 100, CV 0.5 이상 → 0)
    equity_index = max(0, min(100, int((1 - weighted_cv / 0.5) * 100)))

    # 차량별 상세
    detail = []
    for truck, v in stats:
        detail.append({
            "차량": truck,
            "거리km": round(v.get("dist", 0), 1),
            "정지수": v.get("stops", 0),
            "피로도": _calc_fatigue(v),
            "적재율%": round(v.get("used_wt", 0) / max(v.get("max_wt", 1), 1) * 100, 1),
            "연료비": int(v.get("fuel_cost", 0)),
        })

    # 가장 과부하 차량 — 거리·정지·피로도 종합 점수 기준
    if stats:
        scores = []
        for _, v in stats:
            # 각 지표를 정규화 후 합산
            norm_dist    = v.get("dist", 0) / max(max(dists), 1)
            norm_stops   = v.get("stops", 0) / max(max(stops), 1)
            norm_fatigue = _calc_fatigue(v) / 100.0
            scores.append(norm_dist * 0.3 + norm_stops * 0.3 + norm_fatigue * 0.4)
        worst = stats[scores.index(max(scores))][0]
    else:
        worst = ""

    return {
        "index": equity_index,
        "detail": detail,
        "worst": worst,
        "cv_dist": round(cv_dist, 3),
        "cv_stops": round(cv_stops, 3),
        "cv_fatigue": round(cv_fatigue, 3),
    }


def render_driver_equity(res: dict) -> None:
    """[D14] 기사별 수익 공정성 지수 패널"""
    truck_stats = res.get("truck_stats", {})
    if len(truck_stats) < 2:
        return

    eq = _calc_equity_index(truck_stats)
    idx = eq["index"]

    label = "🟢 균등" if idx >= 80 else "🟡 불균형 주의" if idx >= 55 else "🔴 심각한 불균형"

    with st.expander(f"⚖️ 기사별 공정성 지수 — {label} ({idx}/100)", expanded=False):
        st.caption(
            "**[D14 신기능]** 차량(기사)간 배차가 얼마나 공정하게 분배됐는지 수치화합니다. "
            "루티·스마트로 등 경쟁사에는 이 지표가 없습니다."
        )

        # 공정성 게이지
        st.markdown(f"**공정성 지수: `{idx}/100`** — {label}")
        st.progress(idx / 100)

        c1, c2, c3 = st.columns(3)
        c1.metric("거리 편차(CV)", f"{eq['cv_dist']:.3f}",
                  "✅ 균등" if eq["cv_dist"] < 0.2 else "⚠️ 편차 큼")
        c2.metric("정지수 편차(CV)", f"{eq['cv_stops']:.3f}",
                  "✅ 균등" if eq["cv_stops"] < 0.2 else "⚠️ 편차 큼")
        c3.metric("피로도 편차(CV)", f"{eq['cv_fatigue']:.3f}",
                  "✅ 균등" if eq["cv_fatigue"] < 0.2 else "⚠️ 편차 큼")

        if eq["worst"]:
            st.warning(f"🚨 가장 과부하 차량: **{eq['worst']}** — 일부 배송지를 다른 차량으로 이동 검토")

        # 차량별 상세 테이블
        detail = eq["detail"]
        if detail:
            df_eq = pd.DataFrame(detail)
            st.dataframe(
                df_eq.rename(columns={
                    "거리km": "거리(km)",
                    "정지수": "정지 수",
                    "적재율%": "적재율(%)",
                    "연료비": "연료비(₩)",
                }),
                hide_index=True, use_container_width=True,
            )

        # 개선 제안
        if idx < 80:
            worst = eq["worst"]
            st.markdown("**💡 균등화 제안**")
            if eq["cv_stops"] > 0.25:
                st.info(f"정지 수 불균형이 큽니다. {worst} 차량의 배송지 일부를 인근 차량으로 이동하세요.")
            if eq["cv_dist"] > 0.25:
                st.info(f"거리 불균형이 큽니다. {worst} 차량의 배송지 일부를 인근 차량으로 이동하세요.")
            if eq["cv_fatigue"] > 0.25:
                st.info(f"피로도 편차가 큽니다. {worst} 차량의 부담을 줄이는 방향으로 배송지를 재조정하세요.")

        st.caption("CV(변동계수) = 표준편차/평균. 0에 가까울수록 균등 분배.")


# ══════════════════════════════════════════════════════════════════════
# asyncio 안전 실행
# ══════════════════════════════════════════════════════════════════════
def _run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed(): raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════
# 최적화 메인 로직
# ══════════════════════════════════════════════════════════════════════
def run_optimization(hub_name: str) -> None:
    total_v = (st.session_state.cfg_1t_cnt + st.session_state.cfg_2t_cnt
               + st.session_state.cfg_5t_cnt)
    if total_v == 0:
        st.error("❌ 차량이 0대입니다.")
        st.session_state._opt_in_progress = False; return
    if not st.session_state.targets:
        st.error("❌ 배송지가 없습니다.")
        st.session_state._opt_in_progress = False; return
    try:
        datetime.strptime(st.session_state.cfg_start_time, "%H:%M")
    except ValueError:
        st.session_state.cfg_start_time = "09:00"
        st.session_state._opt_in_progress = False; return

    # 지역 변수 캐싱
    cfg = {k: st.session_state[k] for k in [
        "cfg_speed","cfg_congestion","cfg_service","cfg_service_sec_per_kg",
        "cfg_start_time","cfg_weather","cfg_max_hours","cfg_balance",
        "cfg_vrptw_sec","cfg_fuel_price","cfg_labor",
    ]}
    targets = st.session_state.targets
    db_data = st.session_state.db_data

    _abort = False; _abort_msg = ""

    try:
        with st.status("🗺️ 실측 경로 매트릭스 구축 중...", expanded=True) as status:

            hub_candidates = [l for l in db_data if l["name"].strip() == hub_name.strip()]
            if not hub_candidates:
                _abort = True; _abort_msg = f"❌ 허브 '{hub_name}'이 DB에 없습니다."
                status.update(label="❌ 허브 없음", state="error")
            else:
                hub = hub_candidates[0]
                nodes_data = [
                    {**hub, "weight":0,"volume":0,"temperature":"상온",
                     "unload_method":"지게차","difficulty":"일반 (+0분)",
                     "priority":"일반","tw_type":"Hard","tw_start":0,"tw_end":1000,
                     "_node_uid":"hub_0"}
                ] + [{**t, "_node_uid": f"target_{i}"} for i, t in enumerate(targets)]

                missing = [n["name"] for n in nodes_data if "lat" not in n or "lon" not in n]
                if missing:
                    st.warning(f"⚠️ 좌표 누락 제외: {', '.join(missing)}")
                    nodes_data = [n for n in nodes_data if "lat" in n and "lon" in n]

                node_idx_map = {n["_node_uid"]: i for i, n in enumerate(nodes_data)}
                weather_f    = 0.7 if "눈" in cfg["cfg_weather"] else \
                               0.8 if "비" in cfg["cfg_weather"] else 1.0

                combined, travel_tm, svc_list, dist_m, toll_m = _run_async(
                    build_real_time_matrix(
                        nodes_data, cfg["cfg_speed"], cfg["cfg_congestion"], weather_f,
                        cfg["cfg_service"], cfg["cfg_service_sec_per_kg"],
                        kakao_key=KAKAO_API_KEY, api_cache=api_cache, db=db,
                    )
                )

                v_wts,v_vols,v_costs,v_names,v_skills,v_tcaps = [],[],[],[],[],[]
                v1s = st.session_state.get("cfg_v1_skills",[])
                v2s = st.session_state.get("cfg_v2_skills",[])
                v5s = st.session_state.get("cfg_v5_skills",[])
                for i in range(st.session_state.cfg_1t_cnt):
                    v_wts.append(1000);v_vols.append(5.0);v_costs.append(100000)
                    v_tcaps.append(["상온","냉장","냉동"]);v_names.append(f"1톤(냉탑)/#{i+1}")
                    sk=v1s[i] if i<len(v1s) else 1.0; v_skills.append(sk if sk>0 else 1.0)
                for i in range(st.session_state.cfg_2t_cnt):
                    v_wts.append(2500);v_vols.append(12.0);v_costs.append(180000)
                    v_tcaps.append(["상온"]);v_names.append(f"2.5톤(일반)/#{i+1}")
                    sk=v2s[i] if i<len(v2s) else 1.0; v_skills.append(sk if sk>0 else 1.0)
                for i in range(st.session_state.cfg_5t_cnt):
                    v_wts.append(5000);v_vols.append(25.0);v_costs.append(280000)
                    v_tcaps.append(["상온"]);v_names.append(f"5톤(일반)/#{i+1}")
                    sk=v5s[i] if i<len(v5s) else 1.0; v_skills.append(sk if sk>0 else 1.0)

                mwm = cfg["cfg_max_hours"] * 60
                st.write("⚙️ 최적 배차 경로 계산 중...")
                plans, diag, unassigned, used_vi = solve_vrptw(
                    nodes_data, v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps,
                    combined, cfg["cfg_balance"], mwm, cfg["cfg_vrptw_sec"],
                )

                if not plans and not unassigned:
                    _abort=True; _abort_msg="❌ "+"\\n".join(diag)
                    status.update(label="❌ 최적화 실패", state="error")
                else:
                    report, all_paths = [], []
                    dist_tot=time_tot=fuel_liter_tot=co2_tot=toll_tot=wait_tot=0.0
                    tstats: dict[str, Any] = {}
                    base_t   = datetime.strptime(cfg["cfg_start_time"], "%H:%M")
                    late_cnt = total_stops = 0

                    for vi, p in enumerate(plans):
                        col    = _TRUCK_COLORS[vi % len(_TRUCK_COLORS)]
                        vinfo  = used_vi[vi]
                        vtype  = vinfo["name"]
                        vsk    = vinfo["skill"]
                        vtm    = 1.15 if "2.5톤" in vtype else (1.3 if "5톤" in vtype else 1.0)
                        vlabel = f"T{vi+1}({vtype})"

                        delivery_nodes = [n for n in p if n["name"] != hub["name"]]
                        load_w = sum(n.get("weight",0) for n in delivery_nodes)
                        load_v = sum(n.get("volume",0) for n in delivery_nodes)

                        tstats[vlabel] = {
                            "dist":0.0,"time":0.0,"fuel_liter":0.0,"fuel_cost":0.0,
                            "co2_kg":0.0,"toll_cost":0,"wait_time":0,"stops":0,
                            "route_names":[],"loads_detail":[],
                            "cost":vinfo["cost"],
                            "used_wt":load_w,"max_wt":vinfo["max_weight"],
                            "used_vol":load_v,"max_vol":vinfo["max_volume"],
                        }
                        cont_min = 0.0; curr_eta = base_t

                        report.append({
                            "트럭":vlabel,"거점":f"🚩 {hub['name']} (출발)",
                            "도착":base_t.strftime("%H:%M"),"약속시간":"-",
                            "거리":"-","잔여무게":f"{load_w:.1f}kg",
                            "잔여부피":f"{load_v:.1f}CBM","메모":"허브 출발",
                        })

                        for i in range(len(p)-1):
                            fi = node_idx_map.get(p[i].get("_node_uid",""),0)
                            ti = node_idx_map.get(p[i+1].get("_node_uid",""),0)
                            dseg  = dist_m[fi][ti]; tseg = toll_m[fi][ti]
                            trseg = travel_tm[fi][ti]*vsk*vtm
                            is_ret = p[i+1]["name"] == hub["name"]

                            p_i_lat=p[i].get("lat",0.0);   p_i_lon=p[i].get("lon",0.0)
                            p_n_lat=p[i+1].get("lat",0.0); p_n_lon=p[i+1].get("lon",0.0)
                            ck  = f"DIR_{p_i_lat:.4f},{p_i_lon:.4f}_{p_n_lat:.4f},{p_n_lon:.4f}"
                            cd  = api_cache.get(ck)
                            path_d = cd["path"] if cd else [[p_i_lat,p_i_lon],[p_n_lat,p_n_lon]]
                            is_fb  = cd.get("is_fallback",True) if cd else True

                            liter   = get_dynamic_fuel_consumption(vtype, load_w, dseg, is_ret)
                            seg_co2 = liter * DIESEL_EMISSION_FACTOR
                            fuel_liter_tot+=liter; co2_tot+=seg_co2
                            dist_tot+=dseg; toll_tot+=tseg
                            tstats[vlabel]["dist"]       +=dseg
                            tstats[vlabel]["fuel_liter"] +=liter
                            tstats[vlabel]["co2_kg"]     +=seg_co2
                            tstats[vlabel]["toll_cost"]  +=tseg
                            all_paths.append({"path":path_d,"color":col,"is_fallback":is_fb})

                            cont_min+=trseg; curr_eta+=timedelta(minutes=trseg)
                            time_tot+=trseg; tstats[vlabel]["time"]+=trseg

                            elapsed  = (curr_eta-base_t).total_seconds()/60.0
                            tw_s_min = p[i+1].get("tw_start",0)
                            if elapsed < tw_s_min and not is_ret:
                                wait = tw_s_min - elapsed
                                curr_eta+=timedelta(minutes=wait); time_tot+=wait
                                tstats[vlabel]["time"]     +=wait
                                tstats[vlabel]["wait_time"]+=wait; wait_tot+=wait

                            if cont_min >= 240 and not is_ret:
                                curr_eta+=timedelta(minutes=30); cont_min=0.0
                                time_tot+=30; tstats[vlabel]["time"]+=30
                                report.append({
                                    "트럭":vlabel,"거점":"☕ 법정 휴게시간",
                                    "도착":curr_eta.strftime("%H:%M"),"약속시간":"-",
                                    "거리":"-","잔여무게":f"{load_w:.1f}kg",
                                    "잔여부피":f"{load_v:.1f}CBM","메모":"⚠️ 4시간 연속 — 30분 의무 휴식",
                                })

                            if not is_ret:
                                svc=svc_list[ti]; curr_eta+=timedelta(minutes=svc)
                                time_tot+=svc; tstats[vlabel]["time"]+=svc

                            elapsed2 = (curr_eta-base_t).total_seconds()/60.0
                            is_late  = elapsed2 > p[i+1].get("tw_end",1000) and not is_ret

                            if is_ret:
                                report.append({
                                    "트럭":vlabel,"거점":f"🏁 {hub['name']} (복귀)",
                                    "도착":curr_eta.strftime("%H:%M"),"약속시간":"-",
                                    "거리":f"{dseg:.1f}km"+(" ⚠️" if is_fb else ""),
                                    "잔여무게":"0.0kg","잔여부피":"0.0CBM",
                                    "메모":f"허브 복귀 (통행료 ₩{int(tseg):,})",
                                })
                            else:
                                load_w = max(0.0, load_w - p[i+1].get("weight",0))
                                load_v = max(0.0, load_v - p[i+1].get("volume",0))
                                tstats[vlabel]["stops"]        +=1
                                tstats[vlabel]["route_names"].append(p[i+1]["name"])
                                tstats[vlabel]["loads_detail"].append({
                                    "name":p[i+1]["name"],
                                    "weight":p[i+1].get("weight",0),
                                    "volume":p[i+1].get("volume",0),
                                    "diff":p[i+1].get("difficulty","일반").split(" ")[0],
                                })
                                total_stops+=1
                                if is_late: late_cnt+=1
                                dl=p[i+1].get("difficulty","일반").split(" ")[0]
                                tl=p[i+1].get("temperature","상온")
                                ul=p[i+1].get("unload_method","수작업")
                                safe_memo=html.escape(str(p[i+1].get("memo","") or ""))
                                memo_parts=[f"{tl} | {dl} | {ul}"]
                                if tseg>0:  memo_parts.append(f"통행료 ₩{int(tseg):,}")
                                if safe_memo: memo_parts.append(safe_memo)
                                report.append({
                                    "트럭":vlabel,
                                    "거점":p[i+1]["name"],
                                    "도착":(f"{curr_eta.strftime('%H:%M')} ⚠️지연"
                                           if is_late else curr_eta.strftime("%H:%M")),
                                    "약속시간":p[i+1].get("tw_disp","종일"),
                                    "거리":f"{dseg:.1f}km"+(" ⚠️" if is_fb else ""),
                                    "잔여무게":f"{load_w:.1f}kg",
                                    "잔여부피":f"{load_v:.1f}CBM",
                                    "메모":" | ".join(memo_parts),
                                })

                    fuel_tot = fuel_liter_tot * cfg["cfg_fuel_price"]
                    for stat in tstats.values():
                        stat["fuel_cost"] = stat["fuel_liter"] * cfg["cfg_fuel_price"]

                    nn_d  = calc_nn_distance_real(dist_m, nodes_data)
                    eff   = round((1-dist_tot/nn_d)*100,1) if nn_d>0 else 0.0
                    sla   = (round(((total_stops-late_cnt)/total_stops)*100,1)
                             if total_stops>0 else 100.0)
                    fixed = sum(vi_["cost"] for vi_ in used_vi)

                    unassigned_diag = [
                        {"name":n["name"],
                         "reason":diagnose_unassigned(n,v_tcaps,v_wts,v_vols)}
                        for n in unassigned
                    ]

                    result = {
                        "report":report,"dist":dist_tot,"fuel_cost":fuel_tot,
                        "toll_cost":toll_tot,"co2_total":co2_tot,
                        "labor":(time_tot/60)*cfg["cfg_labor"],"fixed_cost":fixed,
                        "total_cost":fuel_tot+toll_tot+(time_tot/60)*cfg["cfg_labor"]+fixed,
                        "truck_stats":tstats,"paths":all_paths,"routes":plans,
                        "efficiency":eff,"nn_real_dist":nn_d,
                        "hub_name":hub["name"],"hub_loc":hub,
                        "unassigned":unassigned,"unassigned_diagnosed":unassigned_diag,
                        "sla":sla,"late_count":late_cnt,"wait_time_total":wait_tot,
                        "_prev_sla":st.session_state.get("_prev_sla",-1.0),
                        "_prev_eff":st.session_state.get("_prev_eff",-1.0),
                    }
                    st.session_state.opt_result = result
                    st.session_state._prev_sla  = sla
                    st.session_state._prev_eff  = eff

                    # [D10] 실행 로그 누적
                    _log_run(result)

                    logger.info("최적화 완료 | %d경로 | %.1fkm | SLA %.1f%%",
                                len(plans), dist_tot, sla)
                    status.update(label="✅ 배차 최적화 완료!", state="complete")

    except Exception as exc:
        logger.exception("run_optimization 예외: %s", exc)
        st.error(f"❌ 최적화 중 오류: {exc}")
    finally:
        st.session_state._opt_in_progress = False

    if _abort: st.error(_abort_msg); return
    st.rerun()


# ══════════════════════════════════════════════════════════════════════
# D2. 시나리오 비교
# ══════════════════════════════════════════════════════════════════════
def render_scenario_panel(res: dict) -> None:
    with st.expander("🔬 차량 구성 시나리오 시뮬레이터 (추정치)", expanded=False):
        st.caption("재배차 없이 비용·탄소를 추정합니다.")
        sc1,sc2,sc3 = st.columns(3)
        with sc1: n1=st.slider("1톤(냉탑)",0,10,st.session_state.cfg_1t_cnt,key="sc_1t")
        with sc2: n2=st.slider("2.5톤",    0,10,st.session_state.cfg_2t_cnt,key="sc_2t")
        with sc3: n3=st.slider("5톤",      0,10,st.session_state.cfg_5t_cnt,key="sc_3t")
        if n1+n2+n3==0: st.warning("차량을 1대 이상 선택하세요."); return
        orig_v    = max(len(res.get("routes",[])),1)
        sim_fixed = n1*100000+n2*180000+n3*280000
        factor    = (n1+n2+n3)/orig_v
        sim_fuel  = res.get("fuel_cost",0)*min(factor,1.25)
        sim_toll  = res.get("toll_cost",0)*min(factor,1.25)
        sim_labor = res.get("labor",0)*min(factor,1.15)
        sim_total = sim_fixed+sim_fuel+sim_toll+sim_labor
        diff      = sim_total-res.get("total_cost",0)
        r1,r2,r3,r4 = st.columns(4)
        r1.metric("고정비",     f"₩{int(sim_fixed):,}", f"{int(sim_fixed-res.get('fixed_cost',0)):+,}")
        r2.metric("연료+통행",  f"₩{int(sim_fuel+sim_toll):,}")
        r3.metric("총 예상비용",f"₩{int(sim_total):,}",f"{int(diff):+,}")
        r4.metric("CO₂ 변화",  f"{(factor-1)*res.get('co2_total',0):+.1f}kg")


# ══════════════════════════════════════════════════════════════════════
# D4. 학습형 경고
# ══════════════════════════════════════════════════════════════════════
def render_learning_warning(res: dict) -> None:
    prev_sla=res.get("_prev_sla",-1.0); prev_eff=res.get("_prev_eff",-1.0)
    curr_sla=res.get("sla",100);        curr_eff=res.get("efficiency",0)
    if prev_sla < 0: return
    msgs=[]
    if curr_sla < prev_sla-5:
        msgs.append(f"📉 SLA **{prev_sla:.1f}% → {curr_sla:.1f}%** 하락. 시간창·차량 조건을 확인하세요.")
    if curr_eff < prev_eff-5:
        msgs.append(f"📉 경로 효율 **{prev_eff:.1f}% → {curr_eff:.1f}%** 저하. 허브·권역 재검토 권장.")
    if msgs:
        with st.expander("🧠 이전 실행 대비 분석", expanded=True):
            for m in msgs: st.warning(m)


# ══════════════════════════════════════════════════════════════════════
# D5. 운행지시서
# ══════════════════════════════════════════════════════════════════════
def render_dispatch_sheet(res: dict) -> None:
    report = res.get("report", [])
    trucks: dict[str,list] = {}
    for row in report:
        t=row["트럭"]
        if t not in trucks: trucks[t]=[]
        trucks[t].append(row)
    lines=["="*60,
           f"LogiTrack 운행지시서  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
           f"허브: {res.get('hub_name','-')}  |  총거리: {res.get('dist',0):.1f}km  |  SLA: {res.get('sla',0):.1f}%",
           "="*60,""]
    for truck,rows in trucks.items():
        lines+=[f"▶ {truck}","-"*40]
        for idx,row in enumerate(rows,1):
            lines.append(
                f"  {idx:2d}. {row.get('거점','')}\n"
                f"      도착: {row.get('도착','-')}  약속: {row.get('약속시간','-')}  잔여: {row.get('잔여무게','')}\n"
                f"      메모: {row.get('메모','')}"
            )
        lines+=["",""]
    content="\n".join(lines)
    st.download_button("📄 운행지시서 다운로드 (.txt)",
                       data=content.encode("utf-8"),
                       file_name=f"운행지시서_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                       mime="text/plain", use_container_width=True)


# ══════════════════════════════════════════════════════════════════════
# 대기열 페이지
# ══════════════════════════════════════════════════════════════════════
def _render_queue_page() -> None:
    hub_name = st.session_state.get("start_node", "")

    with st.expander("⚙️ 운행 설정", expanded=not st.session_state.targets):
        col_t,col_v1,col_v2,col_v3 = st.columns([2,1,1,1])
        with col_t:
            try:    _sv=datetime.strptime(st.session_state.cfg_start_time,"%H:%M")
            except: _sv=datetime.strptime("09:00","%H:%M")
            c_start=st.time_input("⏰ 출발 시간",value=_sv,key="queue_start_time")
            st.session_state.cfg_start_time=c_start.strftime("%H:%M")
        with col_v1:
            st.session_state.cfg_1t_cnt=st.number_input("1톤(냉탑)",0,15,
                st.session_state.cfg_1t_cnt,key="q_1t")
        with col_v2:
            st.session_state.cfg_2t_cnt=st.number_input("2.5톤",0,15,
                st.session_state.cfg_2t_cnt,key="q_2t")
        with col_v3:
            st.session_state.cfg_5t_cnt=st.number_input("5톤",0,15,
                st.session_state.cfg_5t_cnt,key="q_5t")
        col_w,col_h=st.columns(2)
        with col_w:
            wo=["맑음","비 (감속 20%)","눈 (감속 30%)"]
            st.session_state.cfg_weather=st.selectbox("☁️ 기상",wo,
                index=wo.index(st.session_state.cfg_weather)
                      if st.session_state.cfg_weather in wo else 0,key="q_weather")
        with col_h:
            st.session_state.cfg_max_hours=st.number_input("최대 운행 시간",4,16,
                st.session_state.cfg_max_hours,key="q_hours")

    # [D11] 위험도 스크리닝 — 배송지 있을 때 자동 표시
    if st.session_state.targets:
        render_risk_screening()

    # [D13] 배차 전 AI 예보 — 배송지 있을 때 자동 표시
    if st.session_state.targets:
        render_pre_dispatch_forecast()

    col_btn,_=st.columns([4,1])
    with col_btn:
        total_v=(st.session_state.cfg_1t_cnt+st.session_state.cfg_2t_cnt
                 +st.session_state.cfg_5t_cnt)
        if hub_name and st.session_state.targets:
            if total_v==0:
                st.warning("⚠️ 차량 대수가 0대입니다.")
            elif st.session_state.get("_opt_in_progress"):
                st.info("⏳ 경로를 계산하고 있습니다...")
            else:
                st.button(
                    f"🚀 배차 최적화 시작 — {len(st.session_state.targets)}개 배송지 / {total_v}대",
                    type="primary",use_container_width=True,
                    on_click=lambda: st.session_state.update(_run_opt=True,_opt_in_progress=True),
                )
        elif not hub_name:
            st.warning("왼쪽 사이드바에서 허브(출발 거점)를 먼저 선택해주세요.")
        else:
            st.info("배송지를 추가하면 최적화 버튼이 활성화됩니다.")

    st.subheader("📋 배차 대기열")

    # [D12] 일괄 업로드 패널 (대기열 위)
    render_bulk_upload(st.session_state.db_data)

    if not st.session_state.targets:
        st.info(
            "**시작하는 방법**\n\n"
            "1. 왼쪽 사이드바에서 허브(출발점)와 배송지를 등록하세요.\n"
            "2. 위 **일괄 업로드** 또는 사이드바에서 배송지를 추가하세요.\n"
            "3. 배송지가 추가되면 위 **배차 최적화 시작** 버튼이 활성화됩니다."
        )

    if st.session_state.targets:
        try:    sh,sm=map(int,st.session_state.cfg_start_time.split(":"))
        except: sh,sm=9,0
        so=sh*60+sm

        _base_tw_opts=["09:00~18:00","09:00~13:00","13:00~18:00","00:00~23:59","07:00~15:00"]
        _existing=list({t["tw_disp"] for t in st.session_state.targets
                        if t.get("tw_disp") and t["tw_disp"] not in _base_tw_opts})
        tw_opts=_base_tw_opts+_existing

        # 중복 경고
        nc: dict[str,int]={}
        for t in st.session_state.targets: nc[t["name"]]=nc.get(t["name"],0)+1
        dup=[n for n,c in nc.items() if c>1]
        if dup: st.warning(f"⚠️ 중복 배송지: {', '.join(dup)}")

        df_edit=pd.DataFrame([{
            "거점":t["name"],"약속시간":t.get("tw_disp","09:00~18:00"),
            "제약유형":t.get("tw_type","Hard"),"우선순위":t.get("priority","일반"),
            "온도":t.get("temperature","상온"),"무게(kg)":t.get("weight",0),
            "부피(CBM)":t.get("volume",0),"하차방식":t.get("unload_method","수작업"),
            "난이도":t.get("difficulty","일반 (+0분)"),"메모":t.get("memo",""),"삭제":False,
        } for t in st.session_state.targets])

        edited=st.data_editor(df_edit,column_config={
            "거점":      st.column_config.TextColumn("거점",disabled=True),
            "약속시간":  st.column_config.SelectboxColumn("배송 시간대",options=tw_opts),
            "제약유형":  st.column_config.SelectboxColumn("시간 준수",options=["Hard","Soft"],
                          help="Hard: 시간창 엄수 / Soft: 초과 시 페널티"),
            "우선순위":  st.column_config.SelectboxColumn("우선순위",options=["VIP","일반","여유"]),
            "온도":      st.column_config.SelectboxColumn("온도 조건",options=["상온","냉장","냉동"]),
            "무게(kg)":  st.column_config.NumberColumn("무게(kg)",min_value=0.1),
            "부피(CBM)": st.column_config.NumberColumn("부피(CBM)",min_value=0.01),
            "하차방식":  st.column_config.SelectboxColumn("하차방식",options=["수작업","지게차"]),
            "난이도":    st.column_config.SelectboxColumn("진입 난이도",
                          options=["일반 (+0분)","보안아파트 (+10분)","재래시장 (+15분)"]),
            "삭제":      st.column_config.CheckboxColumn("삭제"),
        },hide_index=True,use_container_width=True)

        if st.button("✅ 변경사항 저장",use_container_width=True):
            new_tgts: list[dict]=[]
            orig_idx=0
            for _,row in edited.iterrows():
                if row["삭제"]: orig_idx+=1; continue
                if orig_idx>=len(st.session_state.targets): break
                t=st.session_state.targets[orig_idx].copy()
                t["weight"]       =float(row["무게(kg)"])
                t["volume"]       =float(row["부피(CBM)"])
                t["difficulty"]   =str(row.get("난이도","일반 (+0분)")).strip()
                t["temperature"]  =str(row.get("온도","상온")).strip()
                t["unload_method"]=str(row.get("하차방식","수작업")).strip()
                t["priority"]     =str(row.get("우선순위","일반")).strip()
                t["tw_type"]      =str(row.get("제약유형","Hard")).strip()
                t["memo"]         =str(row.get("메모","")).strip()
                twd=str(row.get("약속시간","09:00~18:00")).strip()
                try:
                    ts,te=twd.split("~")
                    t["tw_start"]=max(0,int(ts[:2])*60+int(ts[3:])-so)
                    t["tw_end"]  =max(t["tw_start"]+1,int(te[:2])*60+int(te[3:])-so)
                    t["tw_disp"] =twd
                except (ValueError,IndexError):
                    logger.warning("tw_disp 파싱 실패: %s",twd)
                new_tgts.append(t); orig_idx+=1
            st.session_state.targets   =new_tgts
            st.session_state.opt_result=None
            st.rerun()

    if st.session_state.pop("_run_opt",False):
        run_optimization(hub_name)


# ══════════════════════════════════════════════════════════════════════
# 결과 페이지
# ══════════════════════════════════════════════════════════════════════
def _render_result_page() -> None:
    res = st.session_state.opt_result

    # D4: 학습형 경고
    render_learning_warning(res)

    # D10: 누적 트렌드
    render_run_trend(res)

    # 미배차
    unassigned=res.get("unassigned",[])
    if unassigned:
        diag=[f"• **{d['name']}** — {d['reason']}"
              for d in res.get("unassigned_diagnosed",[])]
        st.warning(f"⚠️ {len(unassigned)}건 미배차\n\n"+"\n".join(diag))

    # D1: AI 코멘터리
    st.markdown("### 🤖 AI 배차 분석")
    with st.spinner("AI가 결과를 분석하고 있습니다..."):
        commentary=generate_ai_commentary(res)
    st.info(commentary)

    # 기존 대시보드
    render_dashboard(res)
    st.divider()


    # D14: 기사별 공정성 지수
    render_driver_equity(res)

    # D3: 기사 피로도
    with st.expander("😓 기사 피로도 지수",expanded=False):
        rows=[]
        for truck,stat in res.get("truck_stats",{}).items():
            s=_calc_fatigue(stat)
            rows.append({"차량":truck,"피로도":_fatigue_label(s),
                         "운행(분)":int(stat.get("time",0)),
                         "적재율(%)":round(stat.get("used_wt",0)/max(stat.get("max_wt",1),1)*100,1),
                         "대기(분)":int(stat.get("wait_time",0))})
        if rows:
            st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
            st.caption("🟢 양호(<60) · 🟡 주의(60~79) · 🔴 위험(80+)")

    # D6: 탄소 절감
    with st.expander("🌱 탄소 절감 인증",expanded=False):
        cs=_calc_carbon_saving(res)
        c1,c2,c3=st.columns(3)
        c1.metric("절감 거리",  f"{cs['saved_km']}km",  f"-{cs['pct']}%")
        c2.metric("절감 CO₂",  f"{cs['saved_co2']}kg")
        c3.metric("나무 환산", f"≈{cs['trees']}그루·년")
        st.caption("미최적화(NN 기준) 대비 | 나무 1그루 연간 ≈22kg CO₂ 흡수 기준")

    # D7: 공차 구간
    dh=_detect_deadhead(res)
    if dh:
        with st.expander(f"🚛 공차 낭비 구간 ({len(dh)}건)",expanded=False):
            for d in dh: st.warning(d["msg"])

    # D2: 시나리오
    render_scenario_panel(res)

    # D5: 운행지시서
    st.markdown("#### 📥 운행지시서")
    render_dispatch_sheet(res)

    st.divider()
    hub_loc=res.get("hub_loc") or next(
        (l for l in st.session_state.db_data if l["name"]==st.session_state.start_node),
        st.session_state.db_data[0] if st.session_state.db_data else None,
    )
    if hub_loc is None:
        st.error("❌ 허브 위치 정보를 찾을 수 없습니다."); return

    col1,col2=st.columns([1.2,2])
    with col1: render_report(res, hub_loc)
    with col2: render_map(res, hub_loc)


# ── 앱 진입점 ────────────────────────────────────────────────────────
render_sidebar(db, KAKAO_API_KEY)

st.markdown("""
<div class="lt-header">
  <span style="font-size:1.65rem;font-weight:700;letter-spacing:-0.02em;">🚚 LogiTrack</span>
  <span class="lt-badge">배차 최적화</span>
</div>
<p style="color:#94a3b8;font-size:0.82rem;margin:0 0 12px 0;">
  실측 경로 최적화 &nbsp;·&nbsp; 사전 예보 &nbsp;·&nbsp; 공정성 지수
</p>
""", unsafe_allow_html=True)

if not st.session_state.opt_result:
    _render_queue_page()
else:
    _render_result_page()
