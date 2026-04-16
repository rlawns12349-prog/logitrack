"""
ui/dashboard.py — 최적화 결과 대시보드 (탭 6개 + 요약 메트릭)
  render_dashboard(res) 를 app.py에서 호출
"""
import json
import logging
from datetime import datetime

import pandas as pd
import requests
import streamlit as st

from solver import compute_truck_financials, _safe_log_replace

logger = logging.getLogger("logitrack")


def render_dashboard(res: dict):
    # 뒤로가기
    cb, _ = st.columns([1, 4])
    with cb:
        if st.button("🔙 대기열로 돌아가기", use_container_width=True):
            st.session_state.opt_result = None
            st.rerun()

    # 배차 불가 경고
    for item in res.get("unassigned_diagnosed", []):
        st.error(f"🚨 **배차 불가: {item['name']}** — {item['reason']}")

    # KPI 메트릭
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("총 예상 비용", f"₩{int(res['total_cost']):,}",
              help=f"고정비 ₩{res['fixed_cost']:,} + 톨 ₩{int(res['toll_cost']):,}")
    c2.metric("SLA 정시율", f"{res.get('sla', 100.0)}%",
              delta="목표달성" if res.get('sla', 100) >= 95 else f"{-res.get('late_count', 0)}건 지연",
              delta_color="normal" if res.get('sla', 100) >= 95 else "inverse")
    c3.metric("거리 효율",    f"{res.get('efficiency', 0):+.1f}%")
    c4.metric("Scope 3 CO₂", f"{res.get('co2_total', 0):.1f} kg")
    c5.metric("운영 차량",    f"{len(res['routes'])} 대")
    st.caption(
        f"⏱️ 총 대기: **{int(res.get('wait_time_total', 0))}분** | "
        f"배차 거점: **{sum(s['stops'] for s in res['truck_stats'].values())}개소**"
    )

    tstats = res.get('truck_stats', {})
    tab_sum, tab_util, tab_esg, tab_lifo, tab_cost, tab_llm = st.tabs([
        "🚛 운행요약", "📦 적재율", "🌱 ESG", "📋 LIFO", "💰 비용", "🤖 AI리포트",
    ])

    with tab_sum:
        _tab_summary(tstats)

    with tab_util:
        _tab_utilization(tstats)

    with tab_esg:
        _tab_esg(tstats)

    with tab_lifo:
        _tab_lifo(tstats)

    with tab_cost:
        _tab_cost(res, tstats)

    with tab_llm:
        _tab_llm(res, tstats)


# ── 탭별 렌더러 ──────────────────────────────────
def _tab_summary(tstats):
    for tn, s in tstats.items():
        rstr = " → ".join(s['route_names']) or "배송지 없음"
        th, tm = int(s['time'] // 60), int(s['time'] % 60)
        fin = compute_truck_financials(s, st.session_state.cfg_fuel_price,
                                       st.session_state.cfg_labor)
        st.info(
            f"**{tn}**: {rstr} | {s['stops']}개소 · {s['dist']:.1f}km · "
            f"{th}h{tm}m (대기{int(s['wait_time'])}분) | 변동비 ₩{int(fin['total_variable']):,}"
        )


def _tab_utilization(tstats):
    cols = st.columns(max(1, len(tstats)))
    for idx, (tn, s) in enumerate(tstats.items()):
        with cols[idx % len(cols)]:
            st.markdown(f"**{tn}**")
            wp = (s['used_wt']  / s['max_wt'])  * 100 if s['max_wt']  > 0 else 0
            vp = (s['used_vol'] / s['max_vol']) * 100 if s['max_vol'] > 0 else 0
            st.progress(wp / 100, text=f"중량 {s['used_wt']}/{s['max_wt']}kg ({wp:.1f}%)")
            st.progress(vp / 100, text=f"부피 {s['used_vol']}/{s['max_vol']}CBM ({vp:.1f}%)")
            if wp > 90: st.warning("중량 한계 임박")
            if vp > 90: st.warning("부피 한계 임박")


def _tab_esg(tstats):
    st.caption("GLEC Scope 3 | 공차 복귀 연비 향상 반영")
    rows = [
        {
            "트럭": tn,
            "거리": f"{s['dist']:.1f}km",
            "연료(L)": f"{s['fuel_liter']:.1f}",
            "CO2e(kg)": f"{s['co2_kg']:.1f}",
            "소나무": f"{s['co2_kg'] / 6.6:.1f}그루",
        }
        for tn, s in tstats.items()
    ]
    st.table(pd.DataFrame(rows))


def _tab_lifo(tstats):
    st.caption("LIFO 상차 순서 — 마지막 하차지부터 안쪽에 적재")
    for tn, s in tstats.items():
        if not s['loads_detail']:
            continue
        seq = " ➔ ".join(
            f"**{d['name']}** [{d['diff']}]" for d in reversed(s['loads_detail'])
        )
        st.success(f"**{tn}:** {seq}")


def _tab_cost(res, tstats):
    def fw(v): return f"₩{int(v):,}"
    def ft(t): return f"{int(t // 60)}h{int(t % 60)}m"

    rows = []
    for tn, s in tstats.items():
        fin = compute_truck_financials(s, st.session_state.cfg_fuel_price,
                                       st.session_state.cfg_labor)
        rows.append({
            "트럭": tn, "거점수": s['stops'],
            "거리": f"{s['dist']:.1f}km", "시간": ft(s['time']),
            "고정비": fw(s['cost']), "통행료": fw(s['toll_cost']),
            "연료비": fw(fin['fuel_cost']), "인건비": fw(fin['labor_cost']),
            "소계": fw(fin['grand_total']),
        })
    rows.append({
        "트럭": "합계",
        "거점수": sum(s['stops'] for s in tstats.values()),
        "거리": f"{res['dist']:.1f}km",
        "시간": ft(sum(s['time'] for s in tstats.values())),
        "고정비": fw(res['fixed_cost']), "통행료": fw(res['toll_cost']),
        "연료비": fw(res['fuel_cost']),  "인건비": fw(res['labor']),
        "소계": fw(res['total_cost']),
    })
    st.table(pd.DataFrame(rows))


def _tab_llm(res, tstats):
    st.caption("AI 배차 브리핑 자동 생성 (Claude / GPT-4o / Gemini)")
    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "hub":  res['hub_name'],
        "summary": {
            "vehicles":       len(res['routes']),
            "total_cost":     int(res['total_cost']),
            "sla_pct":        res.get('sla', 100),
            "co2_kg":         round(res.get('co2_total', 0), 1),
            "dist_km":        round(res['dist'], 1),
            "efficiency_pct": res.get('efficiency', 0),
            "wait_min":       int(res.get('wait_time_total', 0)),
        },
        "unassigned": [
            {"name": n['name'], "reason": n['reason']}
            for n in res.get('unassigned_diagnosed', [])
        ],
        "vehicles": {
            k: {
                "stops":        v['stops'],
                "dist_km":      round(v['dist'], 1),
                "co2_kg":       round(v['co2_kg'], 1),
                "wt_util_pct":  round((v['used_wt']  / v['max_wt'])  * 100, 1) if v['max_wt']  > 0 else 0,
                "vol_util_pct": round((v['used_vol'] / v['max_vol']) * 100, 1) if v['max_vol'] > 0 else 0,
            }
            for k, v in tstats.items()
        },
    }

    with st.expander("📋 API 페이로드"):
        st.json(payload)

    llm     = st.selectbox("LLM", ["Claude (Anthropic)", "GPT-4o (OpenAI)", "Gemini (Google)"])
    api_key = st.text_input("API 키", type="password", placeholder="입력 후 생성 클릭")
    lang    = st.selectbox("언어", ["한국어", "English"])

    if not st.button("🤖 브리핑 생성", type="primary", use_container_width=True):
        return
    if not api_key:
        st.error("API 키를 입력하세요.")
        return

    prompt = (
        f"다음 물류 배차 데이터:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"{'한국어' if lang == '한국어' else 'English'} 경영진 보고용 일일 배차 브리핑:\n"
        f"① 전체 현황 ② 성과·위험 ③ 배차불가 원인·대안 ④ ESG ⑤ 내일 권고"
    )

    with st.spinner("생성 중..."):
        try:
            ai = _call_llm(llm, api_key, prompt)
            st.subheader("📄 배차 브리핑")
            st.markdown(ai)
            st.download_button(
                "⬇️ 다운로드", data=ai.encode('utf-8'),
                file_name=f"브리핑_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                mime="text/plain",
            )
        except requests.RequestException as e:
            safe_msg = _safe_log_replace(str(e), api_key)
            logger.warning("LLM network error: %s", safe_msg)
            st.error("❌ 네트워크 오류 (로그 확인)")
        except Exception as e:
            # DB-1: JSON 파싱 오류, KeyError 등 requests 외 예외도 처리
            safe_msg = _safe_log_replace(str(e), api_key)
            logger.warning("LLM unexpected error: %s", safe_msg)
            st.error("❌ LLM 응답 처리 오류 (로그 확인)")


def _call_llm(llm: str, api_key: str, prompt: str) -> str:
    if "Claude" in llm:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": "claude-opus-4-6", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()['content'][0]['text']
        safe_body = _safe_log_replace(r.text[:200], api_key)
        logger.warning("Claude API %d: %s", r.status_code, safe_body)
        return f"API 오류 {r.status_code} (자세한 내용은 로그 확인)"

    if "GPT" in llm:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "gpt-4o",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 2000},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json()['choices'][0]['message']['content']
        safe_body = _safe_log_replace(r.text[:200], api_key)
        logger.warning("GPT API %d: %s", r.status_code, safe_body)
        return f"API 오류 {r.status_code} (자세한 내용은 로그 확인)"

    # Gemini — 키를 헤더로 전달 (R-4)
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    if r.status_code == 200:
        return r.json()['candidates'][0]['content']['parts'][0]['text']
    safe_body = _safe_log_replace(r.text[:200], api_key)
    logger.warning("Gemini API %d: %s", r.status_code, safe_body)
    return f"API 오류 {r.status_code} (자세한 내용은 로그 확인)"
