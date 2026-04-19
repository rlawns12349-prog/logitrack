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
        if st.button("← 대기열로", use_container_width=True):
            st.session_state.opt_result = None
            st.rerun()

    # 배차 불가 경고
    for item in res.get("unassigned_diagnosed", []):
        st.error(f"🚨 **배차 불가: {item['name']}** — {item['reason']}")

    # KPI 구분선
    st.markdown(
        '<p style="font-size:0.72rem;font-weight:700;color:#94a3b8;'
        'text-transform:uppercase;letter-spacing:0.1em;margin:4px 0 10px 0;">'
        '📊 오늘의 배차 결과</p>',
        unsafe_allow_html=True,
    )

    # KPI 메트릭
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("총 예상 비용", f"₩{int(res['total_cost']):,}",
              help=f"고정비 ₩{res['fixed_cost']:,} + 톨 ₩{int(res['toll_cost']):,}")
    c2.metric("납기 준수율", f"{res.get('sla', 100.0)}%",
              delta="목표달성" if res.get('sla', 100) >= 95 else f"{-res.get('late_count', 0)}건 지연",
              delta_color="normal" if res.get('sla', 100) >= 95 else "inverse")
    c3.metric("거리 효율", f"{res.get('efficiency', 0):+.1f}%",
              help="최단 경로(NN) 대비 이동 거리 단축률. +가 클수록 효율적입니다.")
    c4.metric("탄소 배출량", f"{res.get('co2_total', 0):.1f} kg")
    c5.metric("운영 차량",   f"{len(res['routes'])} 대")

    st.markdown(
        f'<p style="font-size:0.8rem;color:#94a3b8;margin:6px 0 16px 0;">'
        f'⏱ 총 대기 <b style="color:#e2e8f0">{int(res.get("wait_time_total", 0))}분</b>'
        f'&nbsp;&nbsp;|&nbsp;&nbsp;'
        f'배차 거점 <b style="color:#e2e8f0">{sum(s["stops"] for s in res["truck_stats"].values())}개소</b>'
        f'</p>',
        unsafe_allow_html=True,
    )

    tstats = res.get('truck_stats', {})
    tab_sum, tab_util, tab_esg, tab_lifo, tab_cost, tab_llm = st.tabs([
        "🚛 운행요약", "📦 적재율", "🌱 탄소배출", "📋 상차 순서", "💰 비용", "🤖 AI 브리핑",
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
        wt_pct = (s['used_wt'] / s['max_wt'] * 100) if s['max_wt'] > 0 else 0
        bar_color = "#ef4444" if wt_pct > 90 else "#f59e0b" if wt_pct > 70 else "#22c55e"

        st.markdown(f"""
<div class="truck-card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
    <span style="font-size:1rem;font-weight:700;color:#e2e8f0;">{tn}</span>
    <span style="font-size:0.75rem;color:#94a3b8;font-family:'IBM Plex Mono',monospace;">
      {s['stops']}개소 &nbsp;·&nbsp; {s['dist']:.1f}km &nbsp;·&nbsp; {th}h {tm}m
    </span>
  </div>
  <div style="background:#0f1117;border-radius:6px;padding:8px 12px;
              font-size:0.82rem;color:#94a3b8;line-height:1.8;margin-bottom:10px;
              border:1px solid #2d3250;overflow-x:auto;white-space:nowrap;">
    {rstr}
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;">
    <div>
      <div class="truck-card-label">변동비</div>
      <div class="truck-card-value">₩{int(fin['total_variable']):,}</div>
    </div>
    <div>
      <div class="truck-card-label">대기</div>
      <div class="truck-card-value">{int(s['wait_time'])}분</div>
    </div>
    <div>
      <div class="truck-card-label">적재율</div>
      <div class="truck-card-value" style="color:{bar_color};">{wt_pct:.0f}%</div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)


def _tab_utilization(tstats):
    cols = st.columns(max(1, len(tstats)))
    for idx, (tn, s) in enumerate(tstats.items()):
        with cols[idx % len(cols)]:
            st.markdown(f"**{tn}**")
            wp = (s['used_wt']  / s['max_wt'])  * 100 if s['max_wt']  > 0 else 0
            vp = (s['used_vol'] / s['max_vol']) * 100 if s['max_vol'] > 0 else 0
            st.progress(wp / 100, text=f"중량 {s['used_wt']}/{s['max_wt']}kg ({wp:.1f}%)")
            st.progress(vp / 100, text=f"부피 {s['used_vol']}/{s['max_vol']}CBM ({vp:.1f}%)")
            if wp > 90: st.warning("⚠️ 중량이 거의 찼습니다 (90% 초과)")
            if vp > 90: st.warning("⚠️ 적재 공간이 거의 찼습니다 (90% 초과)")


def _tab_esg(tstats):
    st.caption("GLEC 기준 Scope 3 탄소 배출량 | 공차 복귀 시 연비 향상 반영")
    rows = [
        {
            "트럭": tn,
            "이동 거리": f"{s['dist']:.1f}km",
            "연료 소비(L)": f"{s['fuel_liter']:.1f}",
            "CO₂ 배출(kg)": f"{s['co2_kg']:.1f}",
            "상쇄 필요 소나무": f"{s['co2_kg'] / 6.6:.1f}그루",
        }
        for tn, s in tstats.items()
    ]
    st.table(pd.DataFrame(rows))


def _tab_lifo(tstats):
    st.caption("상차 순서 — 마지막 하차지 화물을 가장 안쪽에 적재합니다")
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
    st.caption("배차 결과를 AI가 요약한 브리핑 문서를 자동 생성합니다.")
    st.info("API 키가 있는 경우에만 사용 가능합니다. Claude, GPT-4o, Gemini 중 선택하세요.", icon="ℹ️")

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

    with st.expander("📊 상세 데이터 보기"):
        st.json(payload)

    llm     = st.selectbox("AI 모델 선택", ["Claude (Anthropic)", "GPT-4o (OpenAI)", "Gemini (Google)"])
    api_key = st.text_input("API 키", type="password", placeholder="선택한 모델의 API 키를 입력하세요")
    lang    = st.selectbox("브리핑 언어", ["한국어", "English"])

    if not st.button("🤖 브리핑 생성", type="primary", use_container_width=True):
        return
    if not api_key:
        st.error("API 키를 입력해야 브리핑을 생성할 수 있습니다.")
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
                "⬇️ 브리핑 다운로드", data=ai.encode('utf-8'),
                file_name=f"배차브리핑_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                mime="text/plain",
            )
        except requests.RequestException as e:
            safe_msg = _safe_log_replace(str(e), api_key)
            logger.warning("LLM network error: %s", safe_msg)
            st.error("네트워크 오류가 발생했습니다. 인터넷 연결을 확인해주세요.")
        except Exception as e:
            safe_msg = _safe_log_replace(str(e), api_key)
            logger.warning("LLM unexpected error: %s", safe_msg)
            st.error("브리핑 생성 중 오류가 발생했습니다. API 키가 올바른지 확인해주세요.")


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
