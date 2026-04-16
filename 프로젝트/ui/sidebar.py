"""
ui/sidebar.py — 사이드바 전체
  render_sidebar(db, kakao_key) 를 app.py에서 호출
"""
import asyncio
import csv as _csv
import hashlib
import logging
from datetime import datetime

import aiohttp
import streamlit as st

from routing import get_kakao_coordinate

logger = logging.getLogger("logitrack")

CFG_KEYS = [
    'cfg_start_time', 'cfg_weather', 'cfg_speed', 'cfg_service',
    'cfg_service_sec_per_kg', 'cfg_congestion', 'cfg_1t_cnt', 'cfg_2t_cnt',
    'cfg_5t_cnt', 'cfg_max_hours', 'cfg_balance', 'cfg_fuel_price',
    'cfg_labor', 'cfg_vrptw_sec',
]


def _cur_cfg():
    return {k: st.session_state[k] for k in CFG_KEYS}


def _is_duplicate_target(name: str) -> bool:
    return any(t['name'] == name for t in st.session_state.get('targets', []))


def render_sidebar(db, kakao_key: str):
    with st.sidebar:
        st.title("🛰️ LogiTrack Pro V18")
        st.caption("Production Grade — B+R Series Fixes")

        _render_settings()
        st.divider()
        _render_scenario_panel(db)
        st.divider()
        _render_location_panel(db, kakao_key)
        _render_csv_upload(db, kakao_key)
        st.divider()
        _render_location_delete(db)
        _render_target_queue(db)


# ── 설정 패널 ─────────────────────────────────────
def _render_settings():
    _speeds  = [20, 30, 40, 45, 50, 60, 70, 80, 90, 100]
    _svcs    = [5, 10, 15, 20, 30, 45, 60]
    _fuels   = [1000, 1200, 1500, 1800, 2000, 2500, 3000]
    _labors  = [10000, 12000, 15000, 18000, 20000, 25000, 30000]
    _vrptws  = [1, 2, 3, 5, 8, 10, 15, 20, 30]
    _weather = ["맑음", "비 (감속 20%)", "눈 (감속 30%)"]

    def _idx(lst, val, d): return lst.index(val) if val in lst else d

    with st.expander("⚙️ 시뮬레이션 환경 설정", expanded=False):
        c_start   = st.time_input("⏰ 출발 시간",
                                   value=datetime.strptime(st.session_state.cfg_start_time, "%H:%M"))
        c_weather = st.selectbox("☁️ 기상", _weather,
                                  index=_idx(_weather, st.session_state.cfg_weather, 0))
        c_speed   = st.selectbox("💨 기준 시속(km/h)", _speeds,
                                  index=_idx(_speeds, st.session_state.cfg_speed, 3))
        c_congest = st.slider("🚦 혼잡 페널티(%)", 0, 80, st.session_state.cfg_congestion)

        st.markdown("**하차 시간**")
        colS1, colS2 = st.columns(2)
        c_svc   = colS1.selectbox("기본(분)", _svcs,
                                   index=_idx(_svcs, st.session_state.cfg_service, 1))
        c_seckg = colS2.number_input("kg당 추가초", min_value=0,
                                      value=st.session_state.cfg_service_sec_per_kg, step=1)

        st.markdown("**🚛 차량 구성**")
        colV1, colV2, colV3 = st.columns(3)
        c_1t = colV1.number_input("1톤(냉탑)", min_value=0, max_value=15,
                                   value=st.session_state.cfg_1t_cnt)
        c_2t = colV2.number_input("2.5톤",     min_value=0, max_value=15,
                                   value=st.session_state.cfg_2t_cnt)
        c_5t = colV3.number_input("5톤",        min_value=0, max_value=15,
                                   value=st.session_state.cfg_5t_cnt)

        with st.expander("🎖️ 기사 스킬 설정"):
            skill_opts = ["베테랑 (×0.8)", "일반 (×1.0)", "초보 (×1.2)"]
            skill_map  = {"베테랑 (×0.8)": 0.8, "일반 (×1.0)": 1.0, "초보 (×1.2)": 1.2}
            v1s, v2s, v5s = [], [], []
            for i in range(c_1t):
                v1s.append(skill_map[st.selectbox(f"1톤 #{i+1}", skill_opts, index=1, key=f"v1_{i}")])
            for i in range(c_2t):
                v2s.append(skill_map[st.selectbox(f"2.5톤 #{i+1}", skill_opts, index=1, key=f"v2_{i}")])
            for i in range(c_5t):
                v5s.append(skill_map[st.selectbox(f"5톤 #{i+1}", skill_opts, index=1, key=f"v5_{i}")])
            st.session_state.cfg_v1_skills = v1s
            st.session_state.cfg_v2_skills = v2s
            st.session_state.cfg_v5_skills = v5s

        st.markdown("**⚖️ 노무·안전**")
        c_hrs   = st.number_input("최대 운행(시간)", min_value=4, max_value=16,
                                   value=st.session_state.cfg_max_hours)
        c_bal   = st.toggle("업무량 균등 분배", value=st.session_state.cfg_balance)
        c_vrptw = st.selectbox("최적화 시간(초)", _vrptws,
                                index=_idx(_vrptws, st.session_state.cfg_vrptw_sec, 3))

        with st.expander("💸 비용 설정"):
            c_fuel  = st.selectbox("경유 단가(원/L)", _fuels,
                                    index=_idx(_fuels,  st.session_state.cfg_fuel_price, 2))
            c_labor = st.selectbox("인건비(원/시)",   _labors,
                                    index=_idx(_labors, st.session_state.cfg_labor, 2))

        st.session_state.update({
            'cfg_start_time':        c_start.strftime("%H:%M"),
            'cfg_weather':           c_weather,
            'cfg_speed':             c_speed,
            'cfg_service':           c_svc,
            'cfg_service_sec_per_kg': c_seckg,
            'cfg_congestion':        c_congest,
            'cfg_1t_cnt':            c_1t,
            'cfg_2t_cnt':            c_2t,
            'cfg_5t_cnt':            c_5t,
            'cfg_max_hours':         c_hrs,
            'cfg_balance':           c_bal,
            'cfg_fuel_price':        c_fuel,
            'cfg_labor':             c_labor,
            'cfg_vrptw_sec':         c_vrptw,
        })


# ── 시나리오 패널 ─────────────────────────────────
def _render_scenario_panel(db):
    with st.expander("💾 시나리오 저장/불러오기"):
        sc_name = st.text_input("시나리오 이름", placeholder="예: 2024-04-15 서울권 배차")
        c_sv, c_ld = st.columns(2)

        with c_sv:
            if st.button("💾 저장", use_container_width=True):
                if not sc_name:
                    st.warning("이름을 입력하세요.")
                else:
                    ok, err = db.save_scenario(
                        sc_name, st.session_state.targets,
                        st.session_state.opt_result,
                        st.session_state.start_node, _cur_cfg(),
                    )
                    st.toast("✅ 저장 완료!" if ok else f"❌ {err}")

        with c_ld:
            saved = db.list_scenarios()
            if saved:
                sel = st.selectbox("불러올 시나리오",
                                    ["선택"] + [s['s_name'] for s in saved], key="sc_sel")
                if sel != "선택":
                    meta = next((s for s in saved if s['s_name'] == sel), {})
                    st.caption(f"📅 {meta.get('created_at','')} | 허브: {meta.get('start_node','')}")
                    if st.button("📂 불러오기", use_container_width=True):
                        sc = db.load_scenario(sel)
                        if sc:
                            st.session_state.targets    = sc['targets']
                            st.session_state.opt_result = sc['result']
                            st.session_state.start_node = sc['start_node']
                            for k, v in sc['cfg'].items():
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
def _render_location_panel(db, kakao_key: str):
    st.subheader("🏢 거점 등록")
    n_addr = st.text_input("주소 또는 장소명", placeholder="예: 평택항")
    n_name = st.text_input("지점명 (비워두면 자동)", placeholder="예: 서울허브")

    if st.button("💾 등록", use_container_width=True):
        if n_addr:
            lat, lon, f_addr, err = get_kakao_coordinate(n_addr, kakao_key)
            if lat:
                final = n_name if n_name else f_addr
                ok, reason = db.insert_location(final, lat, lon, f_addr)
                if ok:
                    st.session_state.db_data = db.load_locations()
                    st.toast(f"✅ '{final}' 등록 완료")
                    st.rerun()
                elif reason == "duplicate":
                    st.error("❌ 이미 존재하는 지점명입니다.")
                else:
                    st.error(f"❌ DB 오류: {reason}")
            else:
                st.error(f"❌ {err}")
        else:
            st.warning("⚠️ 주소를 입력하세요.")

    if st.session_state.db_data:
        with st.expander("✏️ 거점 좌표 수정"):
            edit_n = st.selectbox(
                "수정할 거점",
                ["선택 안함"] + [l['name'] for l in st.session_state.db_data],
                key="edit_loc_sel",
            )
            if edit_n != "선택 안함":
                edit_addr = st.text_input("새 주소", key="edit_addr_input")
                if st.button("🔄 갱신", use_container_width=True):
                    if edit_addr:
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
                    else:
                        st.warning("새 주소를 입력하세요.")


# ── CSV 업로드 패널 ───────────────────────────────
def _render_csv_upload(db, kakao_key: str):
    template = (
        "[설정]\n출발시간,09:00\n기상상황,맑음\n1톤트럭수,2\n2.5톤트럭수,1\n5톤트럭수,0\n"
        "평균시속kmh,45\n교통혼잡도%,40\n기본하차시간분,10\nkg당추가초,2\n최대근로시간,10\n"
        "업무균등화,0\n경유단가원per L,1500\n인건비원per시,15000\n최적화시간,5\n[거점]\n"
        "지점명,주소,허브(O/X),무게kg,부피CBM,온도,하차방식,난이도,우선순위,제약유형,시간약속,메모\n"
        "서울허브,서울특별시 중구 세종대로 110,O,0,0,상온,지게차,일반 (+0분),일반,Hard,00:00~23:59,출발기지\n"
        "강남센터,서울특별시 강남구 테헤란로 152,X,450,2.1,냉장,수작업,보안아파트 (+10분),VIP,Hard,09:00~13:00,오전배송"
    )
    with st.expander("📥 CSV 일괄 업로드"):
        st.download_button(
            "📄 템플릿 다운로드", data=template.encode('utf-8-sig'),
            file_name="배차계획_V18_템플릿.csv", mime="text/csv", use_container_width=True,
        )
        uploaded = st.file_uploader("CSV 업로드", type=["csv"], label_visibility="collapsed")
        if uploaded is None:
            return

        raw     = uploaded.read()
        file_id = hashlib.md5(raw).hexdigest()
        if st.session_state.get('_last_upload_id') == file_id:
            st.info("✅ 이미 처리된 파일입니다.")
            return

        try:
            _process_csv(raw, file_id, db, kakao_key)
        except Exception as e:
            logger.exception("CSV upload error")
            st.error(f"❌ {e}")


def _process_csv(raw: bytes, file_id: str, db, kakao_key: str):
    text = None
    for enc in ('utf-8-sig', 'utf-8', 'cp949', 'euc-kr'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise ValueError("인코딩 오류.")

    settings, loc_lines, headers_row, mode = {}, [], None, None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line == '[설정]':
            mode = 'cfg'; continue
        if line == '[거점]':
            mode = 'loc'; continue
        if mode == 'cfg':
            parts = [p.strip() for p in line.split(',')]
            if len(parts) >= 2:
                settings[parts[0]] = parts[1]
        elif mode == 'loc':
            if headers_row is None:
                headers_row = [h.strip() for h in line.split(',')]
                continue
            loc_lines.append(line)

    def _s(k_, d_):
        for k, v in settings.items():
            if k_ in k:
                try:
                    return float(v)
                except ValueError:
                    pass
        return d_

    def _ss(k_, d_):
        for k, v in settings.items():
            if k_ in k:
                return v.strip()
        return d_

    st.session_state.update({
        'cfg_start_time':         _ss('출발',  "09:00"),
        'cfg_weather':            _ss('기상',  "맑음"),
        'cfg_1t_cnt':             int(_s('1톤',      2)),
        'cfg_2t_cnt':             int(_s('2.5톤',    1)),
        'cfg_5t_cnt':             int(_s('5톤',      0)),
        'cfg_speed':              int(_s('시속',     45)),
        'cfg_congestion':         int(_s('혼잡',     40)),
        'cfg_service':            int(_s('기본하차', 10)),
        'cfg_service_sec_per_kg': int(_s('kg당',     2)),
        'cfg_max_hours':          int(_s('최대근로', 10)),
        'cfg_balance':            bool(_s('균등화',   0)),
        'cfg_fuel_price':         int(_s('단가',   1500)),
        'cfg_labor':              int(_s('인건',  15000)),
        'cfg_vrptw_sec':          int(_s('최적화',   5)),
    })

    if not headers_row:
        raise ValueError("[거점] 섹션 없음")

    def fc(kws):
        return next((h for h in headers_row if any(k in h for k in kws)), None)

    name_col    = fc(['지점', 'name'])    or headers_row[0]
    addr_col    = fc(['주소', 'addr'])    or headers_row[1]
    hub_col     = fc(['허브', 'hub'])
    wt_col      = fc(['무게', 'kg', 'weight'])
    vol_col     = fc(['부피', 'CBM', 'volume'])
    temp_col    = fc(['온도', 'temp', '냉장'])
    method_col  = fc(['하차방식', '지게차', '수작업'])
    diff_col    = fc(['난이도', 'difficulty', '유형'])
    pri_col     = fc(['우선순위', 'priority'])
    twtype_col  = fc(['제약유형', 'Hard', 'Soft'])
    tw_col      = fc(['시간', 'tw', 'time'])
    memo_col    = fc(['메모', 'memo', 'note'])

    name_i  = headers_row.index(name_col)
    addr_i  = headers_row.index(addr_col)
    rows_p  = list(_csv.reader(loc_lines))
    pairs   = [
        (r[name_i].strip(), r[addr_i].strip())
        for r in rows_p
        if len(r) > max(name_i, addr_i) and r[name_i].strip()
    ]

    async def _fetch_all(pairs_):
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
                            url_, headers=hdr,
                            params={"query": addr_},
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as r_:
                            if r_.status == 200:
                                d_ = await r_.json()
                                if d_.get('documents'):
                                    doc = d_['documents'][0]
                                    b_  = (doc.get('place_name')
                                           or doc.get('road_address_name')
                                           or doc.get('address_name'))
                                    return name_, float(doc['y']), float(doc['x']), b_
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        continue
                return name_, None, None, None

        async with aiohttp.ClientSession() as sess_:
            return await asyncio.gather(*[_one(sess_, n, a) for n, a in pairs_])

    with st.spinner(f"🌐 {len(pairs)}개 주소 조회 중..."):
        loop = asyncio.get_event_loop()
        geo  = loop.run_until_complete(_fetch_all(pairs))

    coord_map = {n: (la, lo, fa) for n, la, lo, fa in geo if la}
    loc_map   = {l['name']: l for l in db.load_locations()}
    success = dup = fail = 0
    new_tgts = []
    hub_name = ""
    sh, sm = map(int, st.session_state.cfg_start_time.split(':'))
    so = sh * 60 + sm

    for r in rows_p:
        if len(r) <= max(name_i, addr_i):
            continue
        name = r[name_i].strip()
        if not name:
            continue
        if name in coord_map:
            la, lo, fa = coord_map[name]
            ok, rsn = db.insert_location(name, la, lo, fa)
            if ok:
                success += 1
                loc_map[name] = {'name': name, 'lat': la, 'lon': lo, 'addr': fa}
            elif rsn == "duplicate":
                dup += 1
            else:
                fail += 1
                continue
        elif name not in loc_map:
            fail += 1
            continue

        def _gc(col_):
            if col_ is None:
                return ""
            try:
                return r[headers_row.index(col_)].strip()
            except (ValueError, IndexError):
                return ""

        is_hub = _gc(hub_col).upper() in ('O', 'Y', '1', '허브')
        if is_hub:
            hub_name = name
        else:
            try:
                wt = float(_gc(wt_col) or 50)
            except ValueError:
                wt = 50
            try:
                vol = float(_gc(vol_col) or 0.5)
            except ValueError:
                vol = 0.5
            tmp = _gc(temp_col)   or "상온"
            mth = _gc(method_col) or "수작업"
            dif = _gc(diff_col)   or "일반 (+0분)"
            pri = _gc(pri_col)    or "일반"
            twt = _gc(twtype_col) or "Hard"
            twr = _gc(tw_col)
            twd = twr if '~' in twr else "09:00~18:00"
            mem = _gc(memo_col)
            mem = "" if str(mem).lower() == 'nan' else str(mem)  # SB-2: 비문자열 타입 방어
            try:
                ts, te = twd.split('~')
                tw0 = max(0, int(ts[:2]) * 60 + int(ts[3:]) - so)
                tw1 = max(tw0 + 1, int(te[:2]) * 60 + int(te[3:]) - so)
            except (ValueError, IndexError):
                tw0, tw1 = 0, 1000
            item = loc_map.get(name)
            if item and not _is_duplicate_target(name):
                new_tgts.append({
                    **item,
                    "tw_start": tw0, "tw_end": tw1,
                    "difficulty": dif, "temperature": tmp,
                    "unload_method": mth, "priority": pri,
                    "tw_type": twt, "tw_disp": twd,
                    "weight": wt, "volume": vol, "memo": mem,
                })

    st.session_state.db_data      = db.load_locations()
    st.session_state.opt_result   = None
    st.session_state.delivery_done = {}
    st.session_state['_last_upload_id'] = file_id
    if hub_name:  st.session_state.start_node = hub_name
    if new_tgts:  st.session_state.targets    = new_tgts

    msg = f"✅ 등록 {success}개"
    if dup:      msg += f" / 중복 {dup}개"
    if fail:     msg += f" / 실패 {fail}개"
    if hub_name: msg += f" / 허브:{hub_name}"
    if new_tgts: msg += f" / 배송지 {len(new_tgts)}개"
    st.success(msg)
    st.rerun()


# ── 거점 삭제 / 배송지 추가 ─────────────────────
def _render_location_delete(db):
    if not st.session_state.db_data:
        return
    with st.expander("🗑️ 거점 삭제"):
        del_n = st.selectbox(
            "삭제할 거점",
            ["선택 안함"] + [l['name'] for l in st.session_state.db_data],
        )
        if del_n != "선택 안함" and st.button("삭제하기", use_container_width=True, type="primary"):
            db.delete_location(del_n)
            st.session_state.db_data = db.load_locations()
            st.session_state.targets = [
                t for t in st.session_state.targets if t['name'] != del_n
            ]
            if st.session_state.start_node == del_n:
                rem = [l['name'] for l in st.session_state.db_data]
                st.session_state.start_node = rem[0] if rem else ""
                st.toast("⚠️ 허브가 초기화되었습니다.")
            st.session_state.opt_result = None
            st.rerun()


def _render_target_queue(db):
    st.subheader("🚚 배송지 추가")
    if not st.session_state.db_data:
        return
    all_names = [l['name'] for l in st.session_state.db_data]
    # SB-1: 현재 start_node가 목록에 있으면 그 인덱스 유지, 없으면 0
    cur_hub = st.session_state.get('start_node', '')
    hub_idx = all_names.index(cur_hub) if cur_hub in all_names else 0
    st.session_state.start_node = st.selectbox("🚩 허브(출발점)", all_names, index=hub_idx)
    avail = [
        n for n in all_names
        if not _is_duplicate_target(n) and n != st.session_state.start_node
    ]
    if avail:
        sel_list  = st.multiselect("배송지 선택", avail, placeholder="클릭해서 선택")
        bulk_memo = st.text_input("일괄 메모(선택)", placeholder="예: 냉장 보관")
        if st.button("➕ 큐에 추가", use_container_width=True) and sel_list:
            for sel in sel_list:
                item = next(l for l in st.session_state.db_data if l['name'] == sel)
                if not _is_duplicate_target(sel):
                    st.session_state.targets.append({
                        **item,
                        "tw_start": 0, "tw_end": 600,
                        "difficulty": "일반 (+0분)", "temperature": "상온",
                        "unload_method": "수작업", "priority": "일반",
                        "tw_type": "Hard", "tw_disp": "09:00~19:00",
                        "weight": 100, "volume": 1.0, "memo": bulk_memo.strip(),
                    })
            st.session_state.opt_result = None
            st.rerun()
    if st.session_state.targets:
        st.caption(f"📋 대기열 {len(st.session_state.targets)}개")
