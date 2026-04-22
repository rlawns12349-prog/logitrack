"""
sidebar.py — 사이드바 (개선판)

개선 사항:
  - _process_csv: _gc() 내부의 반복적인 headers_row.index() 호출을
    _col_idx 사전으로 사전 캐싱해 O(1) 조회로 변경.
  - 나머지 로직은 원본과 동일.
"""
import asyncio
import csv as _csv
import hashlib
import logging
from datetime import datetime
from typing import Optional

import aiohttp
import streamlit as st

from routing import get_kakao_coordinate

logger = logging.getLogger("logitrack")

CFG_KEYS = [
    "cfg_start_time", "cfg_weather", "cfg_speed", "cfg_service",
    "cfg_service_sec_per_kg", "cfg_congestion", "cfg_1t_cnt", "cfg_2t_cnt",
    "cfg_5t_cnt", "cfg_max_hours", "cfg_balance", "cfg_fuel_price",
    "cfg_labor", "cfg_vrptw_sec",
]


def _cur_cfg() -> dict:
    return {k: st.session_state[k] for k in CFG_KEYS}


def _is_duplicate_target(name: str) -> bool:
    return any(t["name"] == name for t in st.session_state.get("targets", []))


def _start_offset_minutes() -> int:
    try:
        sh, sm = map(int, st.session_state.cfg_start_time.split(":"))
    except (ValueError, AttributeError):
        sh, sm = 9, 0
    return sh * 60 + sm


def _parse_tw_to_offsets(tw_disp: str, so: int) -> tuple[int, int]:
    try:
        ts, te = tw_disp.split("~")
        tw0 = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
        tw1 = max(tw0 + 1, int(te[:2]) * 60 + int(te[3:]) - so)
        return tw0, tw1
    except (ValueError, IndexError):
        return 0, 1000


# ── 메인 렌더러 ──────────────────────────────────

def render_sidebar(db, kakao_key: str) -> None:
    with st.sidebar:
        st.markdown("""
<div class="lt-logo">
  <div class="lt-logo-name">🚚 LogiTrack</div>
  <div class="lt-logo-sub">배차 최적화 시스템</div>
</div>
""", unsafe_allow_html=True)

        has_locations = bool(st.session_state.get("db_data"))
        has_hub       = bool(st.session_state.get("start_node"))
        has_targets   = bool(st.session_state.get("targets"))
        step1_done    = has_locations
        step2_done    = has_hub and has_targets

        def _step_badge(num: int, label: str, done: bool, active: bool) -> str:
            if done:
                bg, fg, icon = "var(--green-bg)", "var(--green)", "✓"
                circle_bg    = "var(--green)"
                border       = "1px solid var(--green)"
                fw           = "700"
            elif active:
                bg, fg, icon = "var(--blue-bg)", "var(--blue)", str(num)
                circle_bg    = "var(--blue)"
                border       = "2px solid var(--blue)"
                fw           = "700"
            else:
                bg, fg, icon = "var(--raised)", "var(--text3)", str(num)
                circle_bg    = "var(--border)"
                border       = "1px solid var(--border)"
                fw           = "400"
            return (
                f'<div style="display:flex;align-items:center;gap:9px;padding:9px 11px;'
                f'margin-bottom:6px;border-radius:9px;background:{bg};border:{border};">'
                f'<span style="width:24px;height:24px;border-radius:50%;background:{circle_bg};'
                f'color:#fff;font-size:0.72rem;font-weight:700;display:flex;align-items:center;'
                f'justify-content:center;flex-shrink:0;">{icon}</span>'
                f'<span style="font-size:0.82rem;font-weight:{fw};color:{fg};">{label}</span>'
                f'</div>'
            )

        st.markdown("""<div style="font-size:0.7rem;font-weight:600;color:var(--text2);
            letter-spacing:0.08em;margin-bottom:6px;">시작 가이드</div>""",
            unsafe_allow_html=True)

        # 단계별 설명 추가
        _step_descs = [
            "CSV 업로드로 주소 일괄 등록",
            "허브 지정 + 배송지 선택",
            "버튼 클릭 → 최적 경로 자동 계산",
        ]
        badges_html = ""
        for i, (label, desc) in enumerate([
            ("거점 등록",      _step_descs[0]),
            ("허브 & 배송지",  _step_descs[1]),
            ("최적화 실행",    _step_descs[2]),
        ], 1):
            _done   = (i == 1 and step1_done) or (i == 2 and step2_done)
            _active = (i == 1 and not step1_done) or \
                      (i == 2 and step1_done and not step2_done) or \
                      (i == 3 and step2_done)
            badges_html += _step_badge(i, label, _done, _active)
            # 현재 단계에 설명 추가
            if _active:
                badges_html += (
                    f'<div style="margin:-2px 0 6px 34px;font-size:.72rem;'
                    f'color:var(--blue);line-height:1.5;">{desc}</div>'
                )

        st.markdown(badges_html, unsafe_allow_html=True)

        st.markdown(
            '<div class="lt-step-header"><span class="step-num">STEP 1</span><span class="step-name">거점·배송지 등록</span></div>',
            unsafe_allow_html=True,
        )
        _render_csv_upload(db, kakao_key)

        st.markdown(
            '<div class="lt-step-header"><span class="step-num">STEP 2</span><span class="step-name">허브 & 배송지 확인</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div style="margin-top:8px;"></div>', unsafe_allow_html=True)
        st.divider()
        _render_scenario_panel(db)


# ── 시나리오 패널 ─────────────────────────────────

def _render_scenario_panel(db) -> None:
    with st.expander("💾 시나리오 저장/불러오기"):
        sc_name = st.text_input("시나리오 이름", placeholder="예: 2024-04-15 서울권 배차")
        c_sv, c_ld = st.columns(2)

        with c_sv:
            if st.button("💾 저장", use_container_width=True):
                if not sc_name:
                    st.warning("이름을 입력하세요.")
                else:
                    ok, err = db.save_scenario(
                        sc_name,
                        st.session_state.targets,
                        st.session_state.opt_result,
                        st.session_state.start_node,
                        _cur_cfg(),
                    )
                    st.toast("✅ 저장 완료!" if ok else f"❌ {err}")

        with c_ld:
            saved = db.list_scenarios()
            if saved:
                sel = st.selectbox(
                    "불러올 시나리오",
                    ["선택"] + [s["s_name"] for s in saved],
                    key="sc_sel",
                )
                if sel != "선택":
                    meta = next((s for s in saved if s["s_name"] == sel), {})
                    st.caption(f"📅 {meta.get('created_at','')} | 허브: {meta.get('start_node','')}")
                    if st.button("📂 불러오기", use_container_width=True):
                        sc = db.load_scenario(sel)
                        if sc:
                            st.session_state.targets    = sc["targets"]
                            st.session_state.opt_result = sc["result"]
                            st.session_state.start_node = sc["start_node"]
                            for k, v in sc["cfg"].items():
                                st.session_state[k] = v
                            st.toast("✅ 불러오기 완료!")
                            st.rerun()
                        else:
                            st.error("불러오기 실패")
                    if st.button("🗑️ 삭제", use_container_width=True):
                        db.delete_scenario(sel)
                        st.toast("🗑️ 삭제 완료")
                        st.rerun()




# ── CSV 업로드 패널 ───────────────────────────────

_CSV_TEMPLATE = (
    # ================================================================
    # LogiTrack 배차 계획 템플릿
    # ================================================================
    # 사용법:
    #   1) 이 파일을 엑셀로 열면 표 형태로 보입니다
    #   2) 초록색 '예시' 행들을 지우고 실제 데이터를 입력하세요
    #   3) 저장할 때 반드시 "CSV UTF-8" 형식으로 저장하세요
    #      (엑셀: 다른 이름으로 저장 → CSV UTF-8(쉼표로 구분))
    # ================================================================

    # ────────────────────────────────────────────────
    # [1단계] 오늘 운행 설정
    #   아래 '값' 열만 수정하세요. 나머지는 건드리지 마세요.
    # ────────────────────────────────────────────────
    "[설정]\n"
    "항목,값,설명\n"
    "출발시간,09:00,차량이 허브를 출발하는 시각 (HH:MM 형식 예: 08:30)\n"
    "1톤트럭수,2,오늘 운행할 1톤 냉탑 트럭 대수 (냉장·냉동 화물 가능)\n"
    "2.5톤트럭수,1,오늘 운행할 2.5톤 트럭 대수 (상온 화물 전용)\n"
    "5톤트럭수,0,오늘 운행할 5톤 트럭 대수 (상온 화물 전용)\n"
    "\n"

    # ────────────────────────────────────────────────
    # [2단계] 거점·배송지 목록
    #
    #  ★ 컬럼 설명 ★
    #  지점명   : 내부에서 사용할 짧은 이름 (예: 강남A, 서울허브)
    #  주소     : 실제 도로명 또는 지번 주소 (카카오 지도 검색 기준)
    #  허브여부 : 출발 거점은 반드시 "허브", 나머지는 "배송지"
    #             ※ 허브는 전체 목록에서 딱 1개만 지정하세요
    #  무게kg   : 배송할 화물의 무게 (kg). 허브는 0으로 입력
    #  온도관리 : 상온 / 냉장 / 냉동  중 하나 (기본값: 상온)
    #  우선순위 : VIP(반드시 정시) / 일반 / 여유  중 하나 (기본값: 일반)
    #  배송가능시간: 고객이 받을 수 있는 시간대 (HH:MM~HH:MM 형식)
    #              예) 09:00~12:00  /  14:00~18:00  /  00:00~23:59(종일)
    #  메모     : 현장 기사에게 전달할 내용 (없으면 비워두세요)
    # ────────────────────────────────────────────────
    "[거점]\n"
    "지점명,주소,허브여부,무게kg,온도관리,우선순위,배송가능시간,메모\n"

    # 예시 데이터 (실제 사용 시 아래 행들을 모두 지우고 입력하세요)
    "서울물류허브,서울특별시 중구 세종대로 110,허브,0,상온,일반,00:00~23:59,출발 거점\n"
    "강남A고객사,서울특별시 강남구 테헤란로 152,배송지,320,냉장,VIP,09:00~12:00,오전 배송 필수 / 경비실 수령\n"
    "마포B고객사,서울특별시 마포구 월드컵북로 400,배송지,150,상온,일반,10:00~18:00,\n"
    "송파C고객사,서울특별시 송파구 올림픽로 300,배송지,500,냉동,일반,13:00~17:00,지게차 하차\n"
    "여의도D고객사,서울특별시 영등포구 여의대로 24,배송지,80,상온,여유,09:00~20:00,건물 지하 1층 하차\n"
)



def _render_csv_upload(db, kakao_key: str) -> None:
    has_data = bool(st.session_state.get("db_data"))
    with st.expander("📥 CSV로 거점·배송지 등록", expanded=not has_data):

        if not has_data:
            st.markdown("""
<div class="lt-csv-guide">
  <div class="lt-csv-guide-title">📋 처음이신가요? 3단계로 시작하세요</div>
  <div class="lt-csv-step"><span class="lt-csv-step-num">1</span><span class="lt-csv-step-text">아래 <b>템플릿 다운로드</b> 클릭 → 엑셀로 열기</span></div>
  <div class="lt-csv-step"><span class="lt-csv-step-num">2</span><span class="lt-csv-step-text">예시 행 삭제 후 실제 주소 입력 → <b>CSV로 저장</b></span></div>
  <div class="lt-csv-step"><span class="lt-csv-step-num">3</span><span class="lt-csv-step-text">아래 업로드 → 좌표 자동 변환 완료</span></div>
  <div class="lt-csv-note">💡 허브(출발 거점)는 반드시 1개를 <b>허브여부</b> 열에 <b>허브</b>로 표시하세요. 나머지는 모두 <b>배송지</b>입니다.</div>
</div>""", unsafe_allow_html=True)
        else:
            st.caption(f"✅ 거점 {len(st.session_state.db_data)}개 등록됨 — 추가 등록이 필요하면 CSV를 다시 업로드하세요.")

        st.download_button(
            "📄 템플릿 다운로드 (엑셀로 열기)",
            data=_CSV_TEMPLATE.encode("utf-8-sig"),
            file_name="배차계획_템플릿.csv",
            mime="text/csv",
            use_container_width=True,
            help="엑셀에서 열고 데이터 입력 후 'CSV UTF-8'로 저장해 업로드하세요.",
        )
        uploaded = st.file_uploader(
            "완성된 CSV 파일을 여기에 올려주세요",
            type=["csv"],
            help="엑셀 저장 시 '다른 이름으로 저장 → CSV UTF-8' 선택",
        )
        if uploaded is None:
            return

        raw     = uploaded.read()
        file_id = hashlib.md5(raw).hexdigest()
        if st.session_state.get("_last_upload_id") == file_id:
            st.info("✅ 이미 처리된 파일입니다.")
            return
        try:
            _process_csv(raw, file_id, db, kakao_key)
        except Exception as e:
            logger.exception("CSV upload error")
            st.error(f"❌ {e}")


def _process_csv(raw: bytes, file_id: str, db, kakao_key: str) -> None:
    """CSV 바이트 → 거점 DB 등록 + 세션 업데이트.

    개선: 컬럼 인덱스를 _col_idx 사전으로 사전 캐싱해
         반복 호출마다 list.index() 선형 탐색을 O(1)로 단축.
    """
    text = _decode_csv(raw)
    settings, loc_lines, headers_row = _parse_csv_sections(text)
    _apply_settings(settings)

    if not headers_row:
        raise ValueError("[거점] 섹션 없음")

    def fc(kws: list[str]) -> Optional[str]:
        return next((h for h in headers_row if any(k in h for k in kws)), None)

    name_col    = fc(["지점", "name"])              or headers_row[0]
    addr_col    = fc(["주소", "addr"])              or headers_row[1]
    hub_col     = fc(["허브", "hub"])
    wt_col      = fc(["무게", "kg", "weight"])
    vol_col     = fc(["부피", "CBM", "volume"])
    temp_col    = fc(["온도", "temp", "냉장"])
    method_col  = fc(["하차방식", "지게차", "수작업"])
    diff_col    = fc(["난이도", "진입", "difficulty"])
    pri_col     = fc(["우선순위", "priority"])
    twtype_col  = fc(["시간제약", "제약유형", "Hard", "Soft"])
    tw_col      = fc(["배송가능시간", "시간약속", "tw", "time"])
    memo_col    = fc(["메모", "memo", "note"])

    # ── 핵심 개선: 컬럼 인덱스를 사전으로 사전 캐싱 ──────────────────────
    # 기존: r[headers_row.index(col_)] — 호출마다 O(n) 선형 탐색
    # 개선: _col_idx[col_]             — O(1) 사전 조회
    _col_idx: dict[str, int] = {col: i for i, col in enumerate(headers_row)}

    def _gc(col_: Optional[str], row: list[str]) -> str:
        """컬럼명으로 행의 값을 반환. None 또는 범위 초과 시 빈 문자열."""
        if col_ is None:
            return ""
        idx = _col_idx.get(col_, -1)
        if idx < 0 or idx >= len(row):
            return ""
        return row[idx].strip()
    # ────────────────────────────────────────────────────────────────────

    name_i = _col_idx[name_col]
    addr_i = _col_idx[addr_col]
    rows_p = list(_csv.reader(loc_lines))
    pairs  = [
        (r[name_i].strip(), r[addr_i].strip())
        for r in rows_p
        if len(r) > max(name_i, addr_i) and r[name_i].strip()
    ]

    with st.spinner(f"🌐 {len(pairs)}개 주소 조회 중..."):
        loop = asyncio.get_event_loop()
        geo  = loop.run_until_complete(_fetch_coords(pairs, kakao_key))

    coord_map = {n: (la, lo, fa) for n, la, lo, fa in geo if la}
    # load_locations()를 한 번만 호출해 loc_map 구성.
    # 이전: 여기서 1회 + 함수 끝 st.session_state 갱신 시 1회 = 총 2회 DB 조회
    raw_locations = db.load_locations()
    loc_map   = {l["name"]: l for l in raw_locations}
    success = dup = fail = 0
    new_tgts: list[dict] = []
    hub_name = ""
    so       = _start_offset_minutes()

    # _is_duplicate_target을 루프마다 호출하면 targets 전체를 매번 순회.
    # set으로 한 번만 만들어 O(1) 조회로 단축.
    existing_targets: set[str] = {t["name"] for t in st.session_state.get("targets", [])}

    _diff_map = {
        "보안아파트": "보안아파트 (+10분)",
        "재래시장":   "재래시장 (+15분)",
        "일반":       "일반 (+0분)",
    }

    for r in rows_p:
        if len(r) <= max(name_i, addr_i):
            continue
        name = r[name_i].strip()
        if not name:
            continue

        if name in coord_map:
            la, lo, fa = coord_map[name]
            ok, rsn    = db.insert_location(name, la, lo, fa)
            if ok:
                success += 1
                loc_map[name] = {"name": name, "lat": la, "lon": lo, "addr": fa}
            elif rsn == "duplicate":
                dup += 1
            else:
                fail += 1
                continue
        elif name not in loc_map:
            fail += 1
            continue

        is_hub = _gc(hub_col, r).upper() in ("O", "Y", "1", "허브", "허브여부")
        if is_hub:
            hub_name = name
            continue

        try:    wt  = float(_gc(wt_col,  r) or 50)
        except: wt  = 50.0
        try:    vol = float(_gc(vol_col, r) or 0.5)
        except: vol = 0.5

        tmp      = _gc(temp_col,   r) or "상온"
        mth      = _gc(method_col, r) or "수작업"
        raw_diff = _gc(diff_col,   r) or "일반 (+0분)"
        dif      = _diff_map.get(raw_diff, raw_diff)
        pri      = _gc(pri_col,    r) or "일반"
        twt_raw  = _gc(twtype_col, r) or "Hard"
        twt      = "Hard" if twt_raw in ("Hard", "고정") else "Soft"
        twr      = _gc(tw_col,     r)
        twd      = twr if "~" in twr else "09:00~18:00"
        raw_memo = _gc(memo_col,   r)
        mem      = "" if str(raw_memo).lower() in ("nan", "none", "") else str(raw_memo)

        tw0, tw1 = _parse_tw_to_offsets(twd, so)
        item     = loc_map.get(name)
        if item and name not in existing_targets:   # ← set 조회 O(1)
            new_tgts.append({
                **item,
                "tw_start": tw0, "tw_end": tw1,
                "difficulty": dif, "temperature": tmp,
                "unload_method": mth, "priority": pri,
                "tw_type": twt, "tw_disp": twd,
                "weight": wt, "volume": vol, "memo": mem,
            })
            existing_targets.add(name)  # 같은 CSV 내 중복도 방지

    # 새 거점이 추가됐을 수 있으므로 삽입 후 재조회 (1회만)
    final_locations = db.load_locations() if success > 0 else raw_locations
    st.session_state.db_data                = final_locations
    st.session_state.opt_result             = None
    st.session_state.delivery_done          = {}
    st.session_state["_last_upload_id"]     = file_id
    if hub_name:  st.session_state.start_node = hub_name
    if new_tgts:  st.session_state.targets    = new_tgts

    msg = f"✅ 등록 {success}개"
    if dup:      msg += f" / 중복 {dup}개"
    if fail:     msg += f" / 실패 {fail}개"
    if hub_name: msg += f" / 허브: {hub_name}"
    if new_tgts: msg += f" / 배송지 {len(new_tgts)}개"
    st.success(msg)
    st.rerun()


def _decode_csv(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("지원하는 인코딩으로 파일을 읽을 수 없습니다.")


def _parse_csv_sections(text: str) -> tuple[dict, list[str], Optional[list[str]]]:
    settings:    dict              = {}
    loc_lines:   list[str]         = []
    headers_row: Optional[list[str]] = None
    mode:        Optional[str]     = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[설정]":
            mode = "cfg"; continue
        if line == "[거점]":
            mode = "loc"; continue
        if mode == "cfg":
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                settings[parts[0]] = parts[1]
        elif mode == "loc":
            if headers_row is None:
                headers_row = [h.strip() for h in line.split(",")]
                continue
            loc_lines.append(line)

    return settings, loc_lines, headers_row


def _apply_settings(settings: dict) -> None:
    def _s(k_: str, d_: float) -> float:
        for k, v in settings.items():
            if k_ in k:
                try:    return float(v)
                except: pass
        return d_

    def _ss(k_: str, d_: str) -> str:
        for k, v in settings.items():
            if k_ in k:
                return v.strip()
        return d_

    st.session_state.update({
        "cfg_start_time":         _ss("출발",  "09:00"),
        "cfg_weather":            _ss("기상",  "맑음"),
        "cfg_1t_cnt":             int(_s("1톤",      2)),
        "cfg_2t_cnt":             int(_s("2.5톤",    1)),
        "cfg_5t_cnt":             int(_s("5톤",      0)),
        "cfg_speed":              int(_s("시속",     45)),
        "cfg_congestion":         int(_s("혼잡",     40)),
        "cfg_service":            int(_s("기본하차", 10)),
        "cfg_service_sec_per_kg": int(_s("kg당",     2)),
        "cfg_max_hours":          int(_s("최대근로", 10)),
        "cfg_balance":            bool(_s("균등화",   0)),
        "cfg_fuel_price":         int(_s("단가",   1500)),
        "cfg_labor":              int(_s("인건",  15000)),
        "cfg_vrptw_sec":          int(_s("최적화",   5)),
    })


async def _fetch_coords(
    pairs: list[tuple[str, str]],
    kakao_key: str,
) -> list[tuple[str, Optional[float], Optional[float], Optional[str]]]:
    sem = asyncio.Semaphore(8)

    async def _one(sess_, name_, addr_):
        async with sem:
            hdr = {"Authorization": f"KakaoAK {kakao_key}"}
            for url_ in [
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                "https://dapi.kakao.com/v2/local/search/address.json",
            ]:
                try:
                    async with sess_.get(
                        url_, headers=hdr, params={"query": addr_},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as r_:
                        if r_.status == 200:
                            d_ = await r_.json()
                            if d_.get("documents"):
                                doc = d_["documents"][0]
                                b_  = (doc.get("place_name")
                                       or doc.get("road_address_name")
                                       or doc.get("address_name"))
                                return name_, float(doc["y"]), float(doc["x"]), b_
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
            return name_, None, None, None

    async with aiohttp.ClientSession() as sess_:
        return list(await asyncio.gather(*[_one(sess_, n, a) for n, a in pairs]))

