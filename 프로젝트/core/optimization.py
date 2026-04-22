"""
core/optimization.py — 배차 최적화 메인 로직

최적화 사항:
  - build_report_rows: 경로 좌표(path_entries)를 함수 내부에서 직접 수집해
    _assemble_result의 이중 캐시 루프를 제거.
    (이전: rows 반환 후 _assemble_result에서 plan을 다시 순회하며 캐시 재조회)
  - 반환 튜플 → RouteOutput NamedTuple로 명시적 구조화
  - _assemble_result: 경로 좌표 수집 루프 완전 삭제 (build_report_rows로 이관)
  - _run_async: 모듈 수준으로 분리
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta
from typing import Any, NamedTuple

import streamlit as st

from geo import DIESEL_EMISSION_FACTOR, get_dynamic_fuel_consumption
from routing import build_real_time_matrix
from solver import solve_vrptw, calc_nn_distance_real, diagnose_unassigned

logger = logging.getLogger("logitrack")

_TRUCK_COLORS = [
    "#1E3A8A", "#DC2626", "#059669", "#D97706",
    "#7C3AED", "#0891B2", "#BE185D", "#92400E", "#065F46", "#1D4ED8",
]


# ══════════════════════════════════════════════
# asyncio 헬퍼
# ══════════════════════════════════════════════

def _run_async(coro) -> Any:
    """현재 이벤트 루프에서 코루틴 실행. 닫혀 있으면 새 루프 생성."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ══════════════════════════════════════════════
# build_report_rows 반환 타입
# ══════════════════════════════════════════════

class RouteOutput(NamedTuple):
    """build_report_rows 반환값 — 위치 기반 언패킹 실수 방지."""
    rows:         list[dict]
    tstats:       dict[str, Any]
    path_entries: list[dict]   # ← 경로 좌표 (이전에는 _assemble_result에서 재수집)
    dist:         float
    time:         float
    fuel:         float
    co2:          float
    toll:         float
    wait:         float
    late_cnt:     int
    stop_cnt:     int


# ══════════════════════════════════════════════
# 단계 1 — 노드 리스트 구성
# ══════════════════════════════════════════════

def build_nodes(
    hub: dict,
    targets: list[dict],
) -> tuple[list[dict], list[str]]:
    """허브 + 배송지 노드 리스트를 구성하고 좌표 누락 노드를 걸러낸다.

    Args:
        hub:     허브 거점 dict (lat, lon 필수)
        targets: 배송지 dict 리스트

    Returns:
        (nodes_data, missing_names)
        - nodes_data:    솔버에 넘길 노드 리스트 (0번 = 허브)
        - missing_names: 좌표 누락으로 제외된 거점명 리스트
    """
    nodes_data: list[dict] = [
        {
            **hub,
            "weight": 0, "volume": 0, "temperature": "상온",
            "unload_method": "지게차", "difficulty": "일반 (+0분)",
            "priority": "일반", "tw_type": "Hard",
            "tw_start": 0, "tw_end": 1000,
            "_node_uid": "hub_0",
        }
    ] + [
        {**t, "_node_uid": f"target_{i}"}
        for i, t in enumerate(targets)
    ]

    missing = [n["name"] for n in nodes_data if "lat" not in n or "lon" not in n]
    if missing:
        nodes_data = [n for n in nodes_data if "lat" in n and "lon" in n]

    return nodes_data, missing


# ══════════════════════════════════════════════
# 단계 2 — 차량 리스트 구성
# ══════════════════════════════════════════════

def build_vehicles(
    n_1t: int, n_2t: int, n_5t: int,
    skills_1t: list[float],
    skills_2t: list[float],
    skills_5t: list[float],
) -> tuple[list, list, list, list, list, list]:
    """차량 파라미터 6개 리스트를 반환한다.

    Returns:
        (v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps)
    """
    v_wts:   list[float]      = []
    v_vols:  list[float]      = []
    v_costs: list[int]        = []
    v_names: list[str]        = []
    v_skills: list[float]     = []
    v_tcaps:  list[list[str]] = []

    _specs = [
        (n_1t,  1000,  5.0,  100_000, ["상온", "냉장", "냉동"], "1톤(냉탑)",  skills_1t),
        (n_2t,  2500, 12.0,  180_000, ["상온"],                  "2.5톤(일반)", skills_2t),
        (n_5t,  5000, 25.0,  280_000, ["상온"],                  "5톤(일반)",   skills_5t),
    ]
    for n, wt, vol, cost, temps, label, skill_list in _specs:
        for i in range(n):
            v_wts.append(wt)
            v_vols.append(vol)
            v_costs.append(cost)
            v_tcaps.append(temps)
            v_names.append(f"{label}/#{i + 1}")
            sk = skill_list[i] if i < len(skill_list) else 1.0
            v_skills.append(sk if sk > 0 else 1.0)

    return v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps


# ══════════════════════════════════════════════
# 단계 3 — 운행 보고서 행 생성
# ══════════════════════════════════════════════

def build_report_rows(
    plan:         list[dict],
    vi:           int,
    hub:          dict,
    node_idx_map: dict[str, int],
    dist_m:       list[list[float]],
    toll_m:       list[list[int]],
    travel_tm:    list[list[int]],
    svc_list:     list[float],
    api_cache,
    vinfo:        dict,
    base_t:       datetime,
    cfg:          dict,
) -> RouteOutput:
    """단일 차량 경로(plan)에서 보고서 행, 통계, 경로 좌표를 한 번에 생성.

    path_entries를 내부에서 직접 수집 → _assemble_result의 이중 캐시 루프 제거.
    """
    col    = _TRUCK_COLORS[vi % len(_TRUCK_COLORS)]
    vtype  = vinfo["name"]
    vsk    = vinfo["skill"]
    vtm    = 1.15 if "2.5톤" in vtype else (1.3 if "5톤" in vtype else 1.0)
    vlabel = f"T{vi + 1}({vtype})"

    delivery_nodes = [n for n in plan if n["name"] != hub["name"]]
    load_w = sum(n.get("weight", 0) for n in delivery_nodes)
    load_v = sum(n.get("volume", 0) for n in delivery_nodes)

    tstats_entry: dict[str, Any] = {
        "dist": 0.0, "time": 0.0, "fuel_liter": 0.0, "fuel_cost": 0.0,
        "co2_kg": 0.0, "toll_cost": 0, "wait_time": 0, "stops": 0,
        "route_names": [], "loads_detail": [],
        "cost": vinfo["cost"],
        "used_wt": load_w, "max_wt": vinfo["max_weight"],
        "used_vol": load_v, "max_vol": vinfo["max_volume"],
    }

    rows:         list[dict] = [{
        "트럭": vlabel, "거점": f"🚩 {hub['name']} (출발)",
        "도착": base_t.strftime("%H:%M"), "약속시간": "-",
        "거리": "-", "잔여무게": f"{load_w:.1f}kg",
        "잔여부피": f"{load_v:.1f}CBM", "메모": "허브 출발",
    }]
    path_entries: list[dict] = []

    cont_min = 0.0
    curr_eta = base_t
    dist_tot = time_tot = fuel_tot = co2_tot = toll_tot = wait_tot = 0.0
    late_cnt = stop_cnt = 0

    for i in range(len(plan) - 1):
        fi   = node_idx_map.get(plan[i].get("_node_uid", ""), 0)
        ti   = node_idx_map.get(plan[i + 1].get("_node_uid", ""), 0)
        dseg = dist_m[fi][ti]
        tseg = toll_m[fi][ti]
        trseg = travel_tm[fi][ti] * vsk * vtm
        is_ret = plan[i + 1]["name"] == hub["name"]

        p_lat, p_lon = plan[i].get("lat", 0.0),     plan[i].get("lon", 0.0)
        n_lat, n_lon = plan[i + 1].get("lat", 0.0), plan[i + 1].get("lon", 0.0)
        ck = f"DIR_{p_lat:.4f},{p_lon:.4f}_{n_lat:.4f},{n_lon:.4f}"
        cd = api_cache.get(ck)

        # 경로 좌표를 여기서 한 번만 수집 (이전: _assemble_result에서 재조회)
        path_entries.append({
            "path":        cd["path"] if cd else [[p_lat, p_lon], [n_lat, n_lon]],
            "color":       col,
            "is_fallback": cd.get("is_fallback", True) if cd else True,
        })
        is_fb = path_entries[-1]["is_fallback"]

        liter   = get_dynamic_fuel_consumption(vtype, load_w, dseg, is_ret)
        seg_co2 = liter * DIESEL_EMISSION_FACTOR

        dist_tot  += dseg;  toll_tot  += tseg
        fuel_tot  += liter; co2_tot   += seg_co2
        tstats_entry["dist"]       += dseg
        tstats_entry["fuel_liter"] += liter
        tstats_entry["co2_kg"]     += seg_co2
        tstats_entry["toll_cost"]  += tseg

        cont_min += trseg
        curr_eta += timedelta(minutes=trseg)
        time_tot += trseg
        tstats_entry["time"] += trseg

        # 시간창 대기
        elapsed   = (curr_eta - base_t).total_seconds() / 60.0
        tw_s_min  = plan[i + 1].get("tw_start", 0)
        if elapsed < tw_s_min and not is_ret:
            wait      = tw_s_min - elapsed
            curr_eta += timedelta(minutes=wait)
            time_tot += wait
            wait_tot += wait
            tstats_entry["time"]      += wait
            tstats_entry["wait_time"] += wait

        # 법정 휴게 (4시간 연속)
        if cont_min >= 240 and not is_ret:
            curr_eta += timedelta(minutes=30)
            cont_min  = 0.0
            time_tot += 30
            tstats_entry["time"] += 30
            rows.append({
                "트럭": vlabel, "거점": "☕ 법정 휴게시간",
                "도착": curr_eta.strftime("%H:%M"), "약속시간": "-",
                "거리": "-", "잔여무게": f"{load_w:.1f}kg",
                "잔여부피": f"{load_v:.1f}CBM",
                "메모": "⚠️ 4시간 연속 — 30분 의무 휴식",
            })

        if not is_ret:
            svc = svc_list[ti]
            curr_eta += timedelta(minutes=svc)
            time_tot += svc
            tstats_entry["time"] += svc

        elapsed2 = (curr_eta - base_t).total_seconds() / 60.0
        is_late  = elapsed2 > plan[i + 1].get("tw_end", 1000) and not is_ret

        if is_ret:
            rows.append({
                "트럭": vlabel, "거점": f"🏁 {hub['name']} (복귀)",
                "도착": curr_eta.strftime("%H:%M"), "약속시간": "-",
                "거리": f"{dseg:.1f}km" + (" ⚠️" if is_fb else ""),
                "잔여무게": "0.0kg", "잔여부피": "0.0CBM",
                "메모": f"허브 복귀 (통행료 ₩{int(tseg):,})",
            })
        else:
            load_w = max(0.0, load_w - plan[i + 1].get("weight", 0))
            load_v = max(0.0, load_v - plan[i + 1].get("volume", 0))
            tstats_entry["stops"]        += 1
            tstats_entry["route_names"].append(plan[i + 1]["name"])
            tstats_entry["loads_detail"].append({
                "name":   plan[i + 1]["name"],
                "weight": plan[i + 1].get("weight", 0),
                "volume": plan[i + 1].get("volume", 0),
                "diff":   plan[i + 1].get("difficulty", "일반").split(" ")[0],
            })
            stop_cnt += 1
            if is_late:
                late_cnt += 1

            dl  = plan[i + 1].get("difficulty", "일반").split(" ")[0]
            tl  = plan[i + 1].get("temperature", "상온")
            ul  = plan[i + 1].get("unload_method", "수작업")
            sm  = html.escape(str(plan[i + 1].get("memo", "") or ""))
            memo_parts = [f"{tl} | {dl} | {ul}"]
            if tseg > 0:
                memo_parts.append(f"통행료 ₩{int(tseg):,}")
            if sm:
                memo_parts.append(sm)

            rows.append({
                "트럭":   vlabel,
                "거점":   plan[i + 1]["name"],
                "도착":   (f"{curr_eta.strftime('%H:%M')} ⚠️지연"
                           if is_late else curr_eta.strftime("%H:%M")),
                "약속시간": plan[i + 1].get("tw_disp", "종일"),
                "거리":   f"{dseg:.1f}km" + (" ⚠️" if is_fb else ""),
                "잔여무게": f"{load_w:.1f}kg",
                "잔여부피": f"{load_v:.1f}CBM",
                "메모":   " | ".join(memo_parts),
            })

    return RouteOutput(
        rows=rows, tstats=tstats_entry, path_entries=path_entries,
        dist=dist_tot,  time=time_tot, fuel=fuel_tot,
        co2=co2_tot,    toll=toll_tot, wait=wait_tot,
        late_cnt=late_cnt, stop_cnt=stop_cnt,
    )


# ══════════════════════════════════════════════
# 메인 엔트리
# ══════════════════════════════════════════════

def run_optimization(hub_name: str, db, api_cache, kakao_key: str) -> None:
    """배차 최적화 전체 파이프라인 실행."""
    total_v = (st.session_state.cfg_1t_cnt
               + st.session_state.cfg_2t_cnt
               + st.session_state.cfg_5t_cnt)
    if total_v == 0:
        st.error("❌ 차량이 0대입니다.")
        st.session_state._opt_in_progress = False
        return
    if not st.session_state.targets:
        st.error("❌ 배송지가 없습니다.")
        st.session_state._opt_in_progress = False
        return
    try:
        datetime.strptime(st.session_state.cfg_start_time, "%H:%M")
    except ValueError:
        st.session_state.cfg_start_time = "09:00"
        st.session_state._opt_in_progress = False
        return

    cfg = {k: st.session_state[k] for k in [
        "cfg_speed", "cfg_congestion", "cfg_service", "cfg_service_sec_per_kg",
        "cfg_start_time", "cfg_weather", "cfg_max_hours", "cfg_balance",
        "cfg_vrptw_sec", "cfg_fuel_price", "cfg_labor",
    ]}
    targets = st.session_state.targets
    db_data = st.session_state.db_data

    try:
        with st.status("🗺️ 실측 경로 매트릭스 구축 중...", expanded=True) as status:

            # ── Step 1: 허브 확인 ──────────────
            hub_candidates = [l for l in db_data if l["name"].strip() == hub_name.strip()]
            if not hub_candidates:
                st.session_state._opt_in_progress = False
                status.update(label="❌ 허브 없음", state="error")
                st.error(f"❌ 허브 '{hub_name}'이 DB에 없습니다.")
                return

            hub = hub_candidates[0]

            # ── Step 2: 노드 구성 ─────────────
            nodes_data, missing = build_nodes(hub, targets)
            if missing:
                st.warning(f"⚠️ 좌표 누락 제외: {', '.join(missing)}")

            node_idx_map = {n["_node_uid"]: i for i, n in enumerate(nodes_data)}
            weather_f = (0.7 if "눈" in cfg["cfg_weather"]
                         else 0.8 if "비" in cfg["cfg_weather"] else 1.0)

            # ── Step 3: 경로 매트릭스 ─────────
            combined, travel_tm, svc_list, dist_m, toll_m = _run_async(
                build_real_time_matrix(
                    nodes_data,
                    cfg["cfg_speed"], cfg["cfg_congestion"], weather_f,
                    cfg["cfg_service"], cfg["cfg_service_sec_per_kg"],
                    kakao_key=kakao_key, api_cache=api_cache, db=db,
                )
            )

            # ── Step 4: 차량 구성 ─────────────
            v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps = build_vehicles(
                st.session_state.cfg_1t_cnt,
                st.session_state.cfg_2t_cnt,
                st.session_state.cfg_5t_cnt,
                st.session_state.get("cfg_v1_skills", []),
                st.session_state.get("cfg_v2_skills", []),
                st.session_state.get("cfg_v5_skills", []),
            )

            # ── Step 5: VRPTW 솔버 ────────────
            st.write("⚙️ 최적 배차 경로 계산 중...")
            mwm = cfg["cfg_max_hours"] * 60
            plans, diag, unassigned, used_vi = solve_vrptw(
                nodes_data, v_wts, v_vols, v_costs, v_names, v_skills, v_tcaps,
                combined, cfg["cfg_balance"], mwm, cfg["cfg_vrptw_sec"],
            )

            if not plans and not unassigned:
                st.session_state._opt_in_progress = False
                status.update(label="❌ 최적화 실패", state="error")
                st.error("❌ " + "\n".join(diag))
                return

            # ── Step 6: 보고서 생성 ───────────
            result = _assemble_result(
                plans, used_vi, hub, node_idx_map,
                dist_m, toll_m, travel_tm, svc_list,
                api_cache, unassigned, v_tcaps, v_wts, v_vols,
                cfg, nodes_data,
            )
            st.session_state.opt_result = result
            st.session_state._prev_sla  = result["sla"]
            st.session_state._prev_eff  = result["efficiency"]

            status.update(label="✅ 배차 최적화 완료!", state="complete")
            logger.info("최적화 완료 | %d경로 | %.1fkm | SLA %.1f%%",
                        len(plans), result["dist"], result["sla"])

    except Exception as exc:
        logger.exception("run_optimization 예외: %s", exc)
        st.error(f"❌ 최적화 중 오류: {exc}")
    finally:
        st.session_state._opt_in_progress = False

    st.rerun()


def _assemble_result(
    plans, used_vi, hub, node_idx_map,
    dist_m, toll_m, travel_tm, svc_list,
    api_cache, unassigned, v_tcaps, v_wts, v_vols,
    cfg, nodes_data,
) -> dict:
    """솔버 결과를 OptimizationResult dict로 조립한다."""
    base_t = datetime.strptime(cfg["cfg_start_time"], "%H:%M")

    report:    list[dict]     = []
    all_paths: list[dict]     = []
    tstats:    dict[str, Any] = {}

    dist_tot = time_tot = fuel_liter_tot = co2_tot = toll_tot = wait_tot = 0.0
    late_cnt = total_stops = 0

    for vi, plan in enumerate(plans):
        out = build_report_rows(
            plan, vi, hub, node_idx_map,
            dist_m, toll_m, travel_tm, svc_list,
            api_cache, used_vi[vi], base_t, cfg,
        )
        vlabel = f"T{vi + 1}({used_vi[vi]['name']})"

        report.extend(out.rows)
        all_paths.extend(out.path_entries)  # 캐시 재조회 없이 바로 추가
        tstats[vlabel]  = out.tstats

        dist_tot       += out.dist;  time_tot       += out.time
        fuel_liter_tot += out.fuel;  co2_tot        += out.co2
        toll_tot       += out.toll;  wait_tot       += out.wait
        late_cnt       += out.late_cnt
        total_stops    += out.stop_cnt

    for stat in tstats.values():
        stat["fuel_cost"] = stat["fuel_liter"] * cfg["cfg_fuel_price"]

    fuel_cost = fuel_liter_tot * cfg["cfg_fuel_price"]
    nn_d  = calc_nn_distance_real(dist_m, nodes_data)
    eff   = round((1 - dist_tot / nn_d) * 100, 1) if nn_d > 0 else 0.0
    sla   = (round(((total_stops - late_cnt) / total_stops) * 100, 1)
             if total_stops > 0 else 100.0)
    fixed = sum(vi_["cost"] for vi_ in used_vi)

    unassigned_diag = [
        {"name": n["name"],
         "reason": diagnose_unassigned(n, v_tcaps, v_wts, v_vols)}
        for n in unassigned
    ]

    return {
        "report":               report,
        "dist":                 dist_tot,
        "fuel_cost":            fuel_cost,
        "toll_cost":            toll_tot,
        "co2_total":            co2_tot,
        "labor":                (time_tot / 60) * cfg["cfg_labor"],
        "fixed_cost":           fixed,
        "total_cost":           fuel_cost + toll_tot + (time_tot / 60) * cfg["cfg_labor"] + fixed,
        "truck_stats":          tstats,
        "paths":                all_paths,
        "routes":               plans,
        "efficiency":           eff,
        "nn_real_dist":         nn_d,
        "hub_name":             hub["name"],
        "hub_loc":              hub,
        "unassigned":           unassigned,
        "unassigned_diagnosed": unassigned_diag,
        "sla":                  sla,
        "late_count":           late_cnt,
        "wait_time_total":      wait_tot,
        "_prev_sla":            st.session_state.get("_prev_sla", -1.0),
        "_prev_eff":            st.session_state.get("_prev_eff", -1.0),
    }
