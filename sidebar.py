"""
ui/sidebar.py — 사이드바 전체

개선 사항:
  - 모든 함수에 타입 힌팅·docstring 추가
  - _process_csv: 설정 파싱 함수 _apply_settings 분리
  - _render_location_panel: 좌표 수정 UI 분리 (_render_location_edit)
  - 중복 코드 제거 (시간창 파싱 로직 _parse_start_offset으로 통합)
  - 비정상 memo 값 방어 로직 강화
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
    """현재 세션 설정 값 dict 반환."""
    return {k: st.session_state[k] for k in CFG_KEYS}


def _is_duplicate_target(name: str) -> bool:
    """배송 대기열에 이미 동일 이름의 배송지가 있는지 확인."""
    return any(t["name"] == name for t in st.session_state.get("targets", []))


def _start_offset_minutes() -> int:
    """출발 시각을 분 단위 오프셋으로 반환."""
    try:
        sh, sm = map(int, st.session_state.cfg_start_time.split(":"))
    except (ValueError, AttributeError):
        sh, sm = 9, 0
    return sh * 60 + sm


def _parse_tw_to_offsets(tw_disp: str, so: int) -> tuple[int, int]:
    """시간창 문자열 → (tw_start, tw_end) 분 단위 오프셋.

    Args:
        tw_disp: "HH:MM~HH:MM" 형식
        so:      출발 시각 오프셋 (분)

    Returns:
        (tw_start, tw_end) — 음수 방지·최소 간격 1분 보장
    """
    try:
        ts, te = tw_disp.split("~")
        tw0 = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
        tw1 = max(tw0 + 1, int(te[:2]) * 60 + int(te[3:]) - so)
        return tw0, tw1
    except (ValueError, IndexError):
        return 0, 1000


# ── 메인 렌더러 ──────────────────────────────────

def render_sidebar(db, kakao_key: str) -> None:
    """사이드바 전체 렌더링.

    Args:
        db:        DBManager 인스턴스
        kakao_key: Kakao REST API 키
    """
    with st.sidebar:
        # ── 헤더 ──
        st.markdown("""
<div style="padding:12px 0 8px 0;border-bottom:1px solid #2d3250;margin-bottom:16px;">
  <div style="font-size:1.1rem;font-weight:700;letter-spacing:-0.01em;">🚚 LogiTrack</div>
  <div style="font-size:0.72rem;color:#94a3b8;margin-top:2px;font-weight:500;">
    배차 최적화 시스템
  </div>
</div>
""", unsafe_allow_html=True)

        # ── 진행 상태 계산 ──
        has_locations = bool(st.session_state.get("db_data"))
        has_hub       = bool(st.session_state.get("start_node"))
        has_targets   = bool(st.session_state.get("targets"))

        step1_done = has_locations
        step2_done = has_hub and has_targets

        def _step_badge(num: int, label: str, done: bool, active: bool) -> str:
            if done:
                bg, fg, icon       = "#052e16", "#4ade80", "✓"
                circle_bg          = "#16a34a"
                border             = "1px solid #16a34a"
                fw                 = "600"
            elif active:
                bg, fg, icon       = "#172554", "#93c5fd", str(num)
                circle_bg          = "#2563eb"
                border             = "2px solid #3b82f6"
                fw                 = "700"
            else:
                bg, fg, icon       = "#0f1117", "#334155", str(num)
                circle_bg          = "#1e293b"
                border             = "1px solid #1e293b"
                fw                 = "400"
            return (
                f'<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;'
                f'margin-bottom:6px;border-radius:8px;background:{bg};border:{border};">'
                f'<span style="width:22px;height:22px;border-radius:50%;background:{circle_bg};'
                f'color:#fff;font-size:0.7rem;font-weight:700;display:flex;align-items:center;'
                f'justify-content:center;flex-shrink:0;">{icon}</span>'
                f'<span style="font-size:0.8rem;font-weight:{fw};color:{fg};">{label}</span>'
                f'</div>'
            )

        st.markdown("""<div style="font-size:0.7rem;font-weight:600;color:#64748b;
            letter-spacing:0.08em;margin-bottom:6px;">시작 가이드</div>""",
            unsafe_allow_html=True)

        guide_html = (
            _step_badge(1, "거점 등록 (출발·도착지 주소)", step1_done, not step1_done) +
            _step_badge(2, "허브 선택 & 배송지 추가",      step2_done, step1_done and not step2_done) +
            _step_badge(3, "메인 화면에서 최적화 실행",     False,      step2_done)
        )
        st.markdown(guide_html, unsafe_allow_html=True)

        # ── STEP 1: CSV 업로드 ──
        st.markdown(
            '<div style="margin:16px 0 6px 0;padding:6px 10px;'
            'background:#0f2040;border-left:3px solid #3b82f6;border-radius:0 6px 6px 0;">'
            '<span style="font-size:0.7rem;font-weight:700;color:#3b82f6;letter-spacing:0.1em;">STEP 1</span>'
            '<span style="font-size:0.78rem;font-weight:600;color:#cbd5e1;margin-left:8px;">거점·배송지 등록</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_csv_upload(db, kakao_key)

        # ── STEP 2: 허브 & 배송지 ──
        st.markdown(
            '<div style="margin:16px 0 6px 0;padding:6px 10px;'
            'background:#0f2040;border-left:3px solid #3b82f6;border-radius:0 6px 6px 0;">'
            '<span style="font-size:0.7rem;font-weight:700;color:#3b82f6;letter-spacing:0.1em;">STEP 2</span>'
            '<span style="font-size:0.78rem;font-weight:600;color:#cbd5e1;margin-left:8px;">허브 & 배송지 확인</span>'
            '</div>',
            unsafe_allow_html=True,
        )
        _render_target_queue(db)

        # ── 보조 기능 ──
        st.markdown('<div style="margin-top:8px;"></div>', unsafe_allow_html=True)
        st.divider()
        _render_scenario_panel(db)
        _render_location_edit_standalone(db, kakao_key)
        _render_location_delete(db)


# ── 시나리오 패널 ─────────────────────────────────

def _render_scenario_panel(db) -> None:
    """시나리오 저장/불러오기/삭제 패널."""
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


# ── 거점 등록/수정 패널 ───────────────────────────

def _render_location_edit_standalone(db, kakao_key: str) -> None:
    """거점 좌표 수정 (보조 기능 — CSV 등록 후 잘못된 좌표 수정용)."""
    if not st.session_state.db_data:
        return
    with st.expander("✏️ 거점 좌표 수정"):
        edit_n = st.selectbox(
            "수정할 거점",
            ["선택 안함"] + [l["name"] for l in st.session_state.db_data],
            key="edit_loc_sel",
        )
        if edit_n != "선택 안함":
            edit_addr = st.text_input("새 주소", key="edit_addr_input")
            if st.button("🔄 갱신", use_container_width=True):
                if not edit_addr:
                    st.warning("새 주소를 입력하세요.")
                else:
                    from routing import get_kakao_coordinate
                    lat, lon, f_addr, err = get_kakao_coordinate(edit_addr, kakao_key)
                    if lat:
                        ok, e2 = db.update_location(edit_n, lat, lon, f_addr)
                        if ok:
                            st.session_state.db_data = db.load_locations()
                            st.toast(f"✅ '{edit_n}' 갱신 완료")
                            st.rerun()
                        else:
                            st.error(f"❌ {e2}")
                    else:
                        st.error(f"❌ {err}")


# ── CSV 업로드 패널 ───────────────────────────────

_CSV_TEMPLATE = (
    # ── [설정] 섹션: 오늘 운행 기본 조건 ──────────────────────────────────────────
    # 값만 바꾸면 됩니다. 항목명·설명 열은 수정하지 마세요.
    "[설정]\n"
    "항목,값,설명\n"
    "출발시간,09:00,HH:MM 형식 (예: 08:30)\n"
    "1톤트럭수,2,운행할 1톤 트럭 대수\n"
    "2.5톤트럭수,1,운행할 2.5톤 트럭 대수\n"
    "5톤트럭수,0,운행할 5톤 트럭 대수\n"
    "\n"
    # ── [거점] 섹션 ────────────────────────────────────────────────────────────────
    # ★ 굵게 표시된 열이 필수입니다. 나머지는 비워두면 기본값이 적용됩니다.
    # 허브여부: 허브 또는 배송지  (출발 거점은 반드시 1개 '허브'로 지정)
    # 온도관리: 상온 / 냉장 / 냉동  (기본값: 상온)
    # 우선순위: VIP / 일반 / 여유   (기본값: 일반)
    # 배송가능시간: HH:MM~HH:MM 형식  (기본값: 09:00~18:00)
    "[거점]\n"
    "지점명(필수),주소(필수),허브여부(필수),무게kg(필수),온도관리,우선순위,배송가능시간,메모\n"
    "# 아래 예시를 지우고 실제 데이터를 입력하세요\n"
    "서울허브,서울특별시 중구 세종대로 110,허브,0,상온,일반,00:00~23:59,출발 거점\n"
    "강남고객사,서울특별시 강남구 테헤란로 152,배송지,450,냉장,VIP,09:00~13:00,오전 배송 필수\n"
    "마포고객사,서울특별시 마포구 월드컵북로 400,배송지,300,상온,일반,09:00~18:00,\n"
    "송파고객사,서울특별시 송파구 올림픽로 300,배송지,600,냉동,여유,13:00~18:00,오후 배송\n"
)


def _render_csv_upload(db, kakao_key: str) -> None:
    """CSV 일괄 거점 등록 패널."""
    has_data = bool(st.session_state.get("db_data"))
    with st.expander("📥 CSV로 거점·배송지 등록", expanded=not has_data):
        st.markdown("""
<div style="background:#0f2240;border:1px solid #1e40af;border-radius:8px;padding:12px 14px;margin-bottom:12px;">
  <div style="font-size:0.8rem;font-weight:600;color:#93c5fd;margin-bottom:6px;">📋 사용 방법 (3단계)</div>
  <div style="font-size:0.78rem;color:#94a3b8;line-height:1.8;">
    <b style="color:#e2e8f0;">① 템플릿 다운로드</b> → 엑셀로 열기<br>
    <b style="color:#e2e8f0;">② 예시 행 삭제</b> 후 실제 데이터 입력 → CSV로 저장<br>
    <b style="color:#e2e8f0;">③ 아래에 파일 업로드</b> → 자동으로 거점·배송 대기열 등록
  </div>
</div>
""", unsafe_allow_html=True)

        st.download_button(
            "📄 템플릿 다운로드 (엑셀로 열기)",
            data=_CSV_TEMPLATE.encode("utf-8-sig"),
            file_name="배차계획_템플릿.csv",
            mime="text/csv",
            use_container_width=True,
        )

        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        uploaded = st.file_uploader(
            "완성된 CSV 파일을 여기에 올려주세요",
            type=["csv"],
            help="엑셀에서 저장 시 '다른 이름으로 저장 → CSV UTF-8' 을 선택하세요.",
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

    Args:
        raw:       업로드된 파일 원시 바이트
        file_id:   MD5 해시 (중복 처리 방지)
        db:        DBManager 인스턴스
        kakao_key: Kakao REST API 키
    """
    text = _decode_csv(raw)
    settings, loc_lines, headers_row = _parse_csv_sections(text)
    _apply_settings(settings)

    if not headers_row:
        raise ValueError("[거점] 섹션 없음")

    def fc(kws: list[str]) -> Optional[str]:
        return next((h for h in headers_row if any(k in h for k in kws)), None)

    name_col    = fc(["지점", "name"])                     or headers_row[0]
    addr_col    = fc(["주소", "addr"])                     or headers_row[1]
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

    name_i = headers_row.index(name_col)
    addr_i = headers_row.index(addr_col)
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
    loc_map   = {l["name"]: l for l in db.load_locations()}
    success = dup = fail = 0
    new_tgts: list[dict] = []
    hub_name = ""
    so       = _start_offset_minutes()

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

        def _gc(col_: Optional[str]) -> str:
            if col_ is None:
                return ""
            try:
                return r[headers_row.index(col_)].strip()
            except (ValueError, IndexError):
                return ""

        is_hub = _gc(hub_col).upper() in ("O", "Y", "1", "허브", "허브여부")
        if is_hub:
            hub_name = name
            continue

        try:    wt = float(_gc(wt_col) or 50)
        except: wt = 50.0
        try:    vol = float(_gc(vol_col) or 0.5)
        except: vol = 0.5

        tmp  = _gc(temp_col)   or "상온"
        mth  = _gc(method_col) or "수작업"
        raw_diff = _gc(diff_col) or "일반 (+0분)"
        dif  = _diff_map.get(raw_diff, raw_diff)
        pri  = _gc(pri_col)    or "일반"
        twt  = _gc(twtype_col) or "Hard"
        twt  = "Hard" if twt in ("Hard", "고정") else "Soft"
        twr  = _gc(tw_col)
        twd  = twr if "~" in twr else "09:00~18:00"
        raw_memo = _gc(memo_col)
        mem  = "" if str(raw_memo).lower() in ("nan", "none", "") else str(raw_memo)

        tw0, tw1 = _parse_tw_to_offsets(twd, so)
        item     = loc_map.get(name)
        if item and not _is_duplicate_target(name):
            new_tgts.append({
                **item,
                "tw_start": tw0, "tw_end": tw1,
                "difficulty": dif, "temperature": tmp,
                "unload_method": mth, "priority": pri,
                "tw_type": twt, "tw_disp": twd,
                "weight": wt, "volume": vol, "memo": mem,
            })

    st.session_state.db_data       = db.load_locations()
    st.session_state.opt_result    = None
    st.session_state.delivery_done = {}
    st.session_state["_last_upload_id"] = file_id
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
    """다양한 인코딩을 시도해 CSV 바이트를 문자열로 변환.

    Args:
        raw: 원시 바이트

    Returns:
        디코딩된 텍스트

    Raises:
        ValueError: 지원 인코딩으로 모두 실패
    """
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("지원하는 인코딩으로 파일을 읽을 수 없습니다. (UTF-8 또는 CP949 권장)")


def _parse_csv_sections(text: str) -> tuple[dict, list[str], Optional[list[str]]]:
    """CSV 텍스트를 [설정] / [거점] 섹션으로 파싱.

    Args:
        text: CSV 전체 텍스트

    Returns:
        (settings dict, loc_lines, headers_row)
    """
    settings:    dict         = {}
    loc_lines:   list[str]    = []
    headers_row: Optional[list[str]] = None
    mode:        Optional[str] = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):  # 빈 행·주석 행 무시
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
    """파싱된 설정 dict를 세션 상태에 적용.

    Args:
        settings: {항목명: 값} dict
    """
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
    """여러 (이름, 주소) 쌍의 좌표를 비동기 병렬 조회.

    Args:
        pairs:     [(거점명, 주소), ...] 리스트
        kakao_key: Kakao REST API 키

    Returns:
        [(거점명, lat, lon, formatted_addr), ...] — 실패 시 lat/lon/addr은 None
    """
    sem = asyncio.Semaphore(8)

    async def _one(
        sess_: aiohttp.ClientSession,
        name_: str,
        addr_: str,
    ) -> tuple[str, Optional[float], Optional[float], Optional[str]]:
        async with sem:
            hdr = {"Authorization": f"KakaoAK {kakao_key}"}
            for url_ in [
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                "https://dapi.kakao.com/v2/local/search/address.json",
            ]:
                try:
                    async with sess_.get(
                        url_, headers=hdr,
                        params={"query": addr_},
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


# ── 거점 삭제 / 배송지 추가 ─────────────────────

def _render_location_delete(db) -> None:
    """거점 삭제 패널."""
    if not st.session_state.db_data:
        return
    with st.expander("🗑️ 거점 삭제"):
        del_n = st.selectbox(
            "삭제할 거점",
            ["선택 안함"] + [l["name"] for l in st.session_state.db_data],
        )
        if del_n != "선택 안함" and st.button(
            "🗑️ 선택한 거점 삭제", use_container_width=True, type="primary"
        ):
            db.delete_location(del_n)
            st.session_state.db_data = db.load_locations()
            st.session_state.targets = [
                t for t in st.session_state.targets if t["name"] != del_n
            ]
            if st.session_state.start_node == del_n:
                rem = [l["name"] for l in st.session_state.db_data]
                st.session_state.start_node = rem[0] if rem else ""
                st.toast("⚠️ 허브가 초기화되었습니다.")
            st.session_state.opt_result = None
            st.rerun()


def _render_target_queue(db) -> None:
    """배송지 추가 패널."""
    st.markdown(
        '<div style="font-size:0.7rem;font-weight:700;color:#3b82f6;letter-spacing:0.1em;'
        'margin-bottom:4px;">STEP 2</div>',
        unsafe_allow_html=True,
    )
    st.subheader("🚚 배송지 추가")
    if not st.session_state.db_data:
        return

    all_names = [l["name"] for l in st.session_state.db_data]
    cur_hub   = st.session_state.get("start_node", "")
    hub_idx   = all_names.index(cur_hub) if cur_hub in all_names else 0
    st.session_state.start_node = st.selectbox("🚩 허브(출발점)", all_names, index=hub_idx)

    avail = [
        n for n in all_names
        if not _is_duplicate_target(n) and n != st.session_state.start_node
    ]
    if avail:
        sel_list  = st.multiselect("배송지 선택", avail, placeholder="클릭해서 선택")
        bulk_memo = st.text_input("일괄 메모(선택)", placeholder="예: 냉장 보관")
        if st.button("➕ 배송지 추가", use_container_width=True) and sel_list:
            so = _start_offset_minutes()
            for sel in sel_list:
                item = next(l for l in st.session_state.db_data if l["name"] == sel)
                if not _is_duplicate_target(sel):
                    st.session_state.targets.append({
                        **item,
                        "tw_start":     0,
                        "tw_end":       600,
                        "difficulty":   "일반 (+0분)",
                        "temperature":  "상온",
                        "unload_method":"수작업",
                        "priority":     "일반",
                        "tw_type":      "Hard",
                        "tw_disp":      "09:00~19:00",
                        "weight":       100,
                        "volume":       1.0,
                        "memo":         bulk_memo.strip(),
                    })
            st.session_state.opt_result = None
            st.rerun()

    if st.session_state.targets:
        st.caption(f"📋 배송지 {len(st.session_state.targets)}개 대기 중")
