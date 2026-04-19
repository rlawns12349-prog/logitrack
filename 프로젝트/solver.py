"""
solver.py — OR-Tools VRPTW 솔버 및 재무 계산

개선 사항:
  - 타입 힌팅 전면 적용
  - 솔버 페널티·배율 상수를 config.py에서 로드
  - _safe_tw, diagnose_unassigned, compute_truck_financials 문서화 강화
  - _make_transit_callback: 내부 클로저 late-binding 방지 주석 개선
  - calc_nn_distance_real: 엣지 케이스 명확화
"""
import math
import logging
from typing import Optional

from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from geo import DIESEL_EMISSION_FACTOR  # noqa: F401 (re-export for app.py convenience)

try:
    from config import solver_config
    _VIP_PENALTY     = solver_config.VIP_PENALTY
    _NORMAL_PENALTY  = solver_config.NORMAL_PENALTY
    _LEISURE_PENALTY = solver_config.LEISURE_PENALTY
    _TIME_MULTIPLIERS = solver_config.VEHICLE_TIME_MULTIPLIERS
except ImportError:
    _VIP_PENALTY, _NORMAL_PENALTY, _LEISURE_PENALTY = 50_000_000, 5_000_000, 100_000
    _TIME_MULTIPLIERS = {"1톤": 1.0, "2.5톤": 1.15, "5톤": 1.3}

logger = logging.getLogger("logitrack")


# ── 시간창 유틸 ─────────────────────────────────

def _safe_tw(node: dict) -> tuple[int, int]:
    """노드의 시간창을 안전하게 반환.

    음수 시작값은 0으로 클램핑. 종료값이 시작값 이하이면 시작+1로 보정.

    Args:
        node: 배송 노드 dict (tw_start, tw_end 키 선택적)

    Returns:
        (start_min, end_min) — 양수 보장
    """
    s = max(0, int(node.get("tw_start", 0)))
    e = max(s + 1, int(node.get("tw_end", 630)))
    return s, e


# ── 차량별 시간 배율 ─────────────────────────────

def _vehicle_time_multiplier(v_name: str) -> float:
    """차량명으로 시간 배율 반환.

    Args:
        v_name: 차량명 ("1톤", "2.5톤", "5톤" 포함 문자열)

    Returns:
        시간 배율 (1.0 ~ 1.3)
    """
    for key, mult in _TIME_MULTIPLIERS.items():
        if key in v_name:
            return mult
    return 1.0


# ── VRPTW 솔버 ───────────────────────────────────

def solve_vrptw(
    nodes:           list[dict],
    v_weights:       list[float],
    v_volumes:       list[float],
    v_costs:         list[int],
    v_names:         list[str],
    v_skills:        list[float],
    v_temp_caps:     list[list[str]],
    combined_matrix: list[list[int]],
    balance_workload:bool,
    max_work_minutes:int,
    time_limit_sec:  int = 5,
) -> tuple[Optional[list], list[str], list[dict], list[dict]]:
    """OR-Tools VRPTW 솔버 실행.

    Args:
        nodes:            배송 노드 리스트 (0번이 허브)
        v_weights:        차량별 최대 중량 (kg)
        v_volumes:        차량별 최대 부피 (CBM)
        v_costs:          차량별 고정비 (원)
        v_names:          차량별 이름
        v_skills:         차량별 운전자 숙련도 계수 (0.5~2.0)
        v_temp_caps:      차량별 지원 온도 목록
        combined_matrix:  (travel_time + service_time) 행렬
        balance_workload: 업무량 균등 배분 여부
        max_work_minutes: 최대 근로 시간(분)
        time_limit_sec:   솔버 제한 시간(초)

    Returns:
        (routes, warn_msgs, unassigned, used_vi)
        - routes:     차량별 노드 경로 리스트 (배차된 차량만)
        - warn_msgs:  경고 메시지 리스트
        - unassigned: 배차 실패 노드 리스트
        - used_vi:    배차된 차량 정보 리스트
    """
    size = len(nodes)
    nv   = len(v_weights)

    if nv == 0:
        return None, ["차량이 없습니다."], [], []
    if size <= 1:
        return None, ["배송지가 없습니다."], [], []

    # 정수 변환
    int_wt   = [int(math.ceil(n.get("weight", 0)))       for n in nodes]
    int_vol  = [int(math.ceil(n.get("volume", 0) * 100)) for n in nodes]
    int_vwt  = [int(w)       for w in v_weights]
    int_vvol = [int(v * 100) for v in v_volumes]
    tws      = [_safe_tw(n)  for n in nodes]

    manager = pywrapcp.RoutingIndexManager(size, nv, 0)
    routing = pywrapcp.RoutingModel(manager)

    # ① 차량별 Transit callback (late-binding 방지: default 인수 캡처)
    transit_cbs = []
    for v in range(nv):
        vm = _vehicle_time_multiplier(v_names[v])
        sk = v_skills[v]

        def _make_cb(vi: int, vehicle_mult: float, skill: float):
            def _cb(fi: int, ti: int) -> int:
                fn = manager.IndexToNode(fi)
                tn = manager.IndexToNode(ti)
                return int(combined_matrix[fn][tn] * skill * vehicle_mult)
            return _cb

        ci = routing.RegisterTransitCallback(_make_cb(v, vm, sk))
        transit_cbs.append(ci)
        routing.SetArcCostEvaluatorOfVehicle(ci, v)

    # ② Time dimension
    solver_max = max_work_minutes - 30 if max_work_minutes >= 240 else max_work_minutes
    routing.AddDimensionWithVehicleTransits(transit_cbs, 120, int(solver_max), False, "Time")
    td = routing.GetDimensionOrDie("Time")

    if balance_workload:
        td.SetGlobalSpanCostCoefficient(100)

    # ③ 고정비
    for v in range(nv):
        routing.SetFixedCostOfVehicle(int(v_costs[v]), v)

    # ④ 배송지별 우선순위 패널티 및 시간창
    for i in range(1, size):
        pri = nodes[i].get("priority", "일반")
        pen = (_VIP_PENALTY     if pri == "VIP"
               else _LEISURE_PENALTY if pri == "여유"
               else _NORMAL_PENALTY)
        routing.AddDisjunction([manager.NodeToIndex(i)], pen)

    for i, (s, e) in enumerate(tws):
        idx = manager.NodeToIndex(i)
        td.CumulVar(idx).SetMin(s)
        if nodes[i].get("tw_type", "Hard") == "Soft":
            td.SetCumulVarSoftUpperBound(idx, e, 10_000)
        else:
            td.CumulVar(idx).SetMax(e)

        # 온도 조건 불일치 차량 제외
        nt = nodes[i].get("temperature", "상온")
        for v in range(nv):
            if nt not in v_temp_caps[v]:
                routing.VehicleVar(idx).RemoveValue(v)

    # ⑤ 중량·부피 용량 — default 인수로 late-binding 방지
    wi = routing.RegisterUnaryTransitCallback(
        lambda f, wt=int_wt: wt[manager.IndexToNode(f)]
    )
    vi = routing.RegisterUnaryTransitCallback(
        lambda f, vol=int_vol: vol[manager.IndexToNode(f)]
    )
    routing.AddDimensionWithVehicleCapacity(wi, 0, int_vwt,  True, "Weight")
    routing.AddDimensionWithVehicleCapacity(vi, 0, int_vvol, True, "Volume")

    # ⑥ 탐색 파라미터
    sp = pywrapcp.DefaultRoutingSearchParameters()
    sp.first_solution_strategy    = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    sp.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    sp.time_limit.FromSeconds(int(time_limit_sec))
    solution = routing.SolveWithParameters(sp)

    if not solution:
        return None, ["해를 찾을 수 없습니다. 제약을 완화하세요."], [], []

    # ⑦ 결과 추출
    unassigned: list[dict] = []
    for i in range(1, size):
        idx = manager.NodeToIndex(i)
        if solution.Value(routing.NextVar(idx)) == idx:
            unassigned.append(nodes[i])

    routes:  list[list[dict]] = []
    used_vi: list[dict]       = []

    for vid in range(nv):
        idx  = routing.Start(vid)
        plan: list[dict] = []
        while not routing.IsEnd(idx):
            ni = manager.IndexToNode(idx)
            plan.append({**nodes[ni], "arr_min": solution.Min(td.CumulVar(idx))})
            idx = solution.Value(routing.NextVar(idx))
        # 종점(허브 복귀) 추가
        plan.append({**nodes[manager.IndexToNode(idx)], "arr_min": solution.Min(td.CumulVar(idx))})

        if len(plan) > 2:  # 허브 출발 + 1개 이상 배송지 + 허브 복귀
            routes.append(plan)
            used_vi.append({
                "name":       v_names[vid],
                "max_weight": v_weights[vid],
                "max_volume": v_volumes[vid],
                "cost":       v_costs[vid],
                "skill":      v_skills[vid],
            })

    return routes, [], unassigned, used_vi


# ── Nearest-Neighbor 기준 거리 ────────────────────

def calc_nn_distance_real(
    real_dist_matrix: list[list[float]],
    nodes_data:       list[dict],
) -> float:
    """Nearest-Neighbor 휴리스틱으로 기준 거리 계산 (효율성 지표용).

    단일 노드(허브만) 또는 빈 입력이면 0.0 반환.
    unvisited를 set으로 관리해 remove를 O(1)로 처리.

    Args:
        real_dist_matrix: n×n 실측 거리 행렬 (km)
        nodes_data:       배송 노드 리스트

    Returns:
        NN 휴리스틱 총 거리 (km)
    """
    size = len(nodes_data)
    if size <= 1:
        return 0.0

    unvisited = set(range(1, size))
    curr      = 0
    total     = 0.0

    while unvisited:
        nxt    = min(unvisited, key=lambda j: real_dist_matrix[curr][j])
        total += real_dist_matrix[curr][nxt]
        curr   = nxt
        unvisited.discard(nxt)

    # 마지막 노드 → 허브 복귀
    total += real_dist_matrix[curr][0]
    return total


# ── 미배차 진단 ───────────────────────────────────

def diagnose_unassigned(
    node:       dict,
    v_temp_caps:list[list[str]],
    v_weights:  list[float],
    v_volumes:  list[float],
) -> str:
    """배차 실패 원인을 진단해 사람이 읽을 수 있는 문자열로 반환.

    Args:
        node:        배차 실패한 노드 dict
        v_temp_caps: 차량별 지원 온도 목록
        v_weights:   차량별 최대 중량 (kg)
        v_volumes:   차량별 최대 부피 (CBM)

    Returns:
        원인 설명 문자열 (여러 원인 시 " / "로 구분)
    """
    reasons: list[str] = []
    nt = node.get("temperature", "상온")
    nw = node.get("weight",      0)
    nv = node.get("volume",      0)
    s, e = _safe_tw(node)

    if v_temp_caps and not any(nt in c for c in v_temp_caps):
        reasons.append(f"온도({nt}) 미지원")

    if v_weights and not any(nw <= w for w in v_weights):
        reasons.append(f"중량 초과({nw}kg > 최대{max(v_weights):.0f}kg)")

    if v_volumes and not any(nv <= v for v in v_volumes):
        reasons.append(f"부피 초과({nv}CBM > 최대{max(v_volumes):.2f}CBM)")

    if e - s < 30:
        reasons.append(f"시간창 협소({node.get('tw_disp', '?')}, {e - s}분)")

    if not reasons:
        reasons.append("근로시간/복합 제약 초과")

    return " / ".join(reasons)


# ── 재무 계산 ─────────────────────────────────────

def compute_truck_financials(s: dict, fuel_price: int, labor_per_hour: int) -> dict:
    """차량별 비용 계산.

    Args:
        s:              truck_stats 단일 항목 dict
                        (fuel_liter, time, toll_cost, cost 키 필요)
        fuel_price:     연료 단가 (원/L)
        labor_per_hour: 시간당 인건비 (원/h)

    Returns:
        {fuel_cost, labor_cost, total_variable, grand_total}
    """
    tf = s["fuel_liter"] * fuel_price
    tl = (s["time"] / 60) * labor_per_hour
    return {
        "fuel_cost":      tf,
        "labor_cost":     tl,
        "total_variable": tf + tl + s["toll_cost"],
        "grand_total":    tf + tl + s["toll_cost"] + s["cost"],
    }


# ── API 키 마스킹 ─────────────────────────────────

def _mask_key(key: Optional[str]) -> str:
    """API 키를 마스킹해 로그 유출 방지.

    Args:
        key: API 키 문자열 (None 허용)

    Returns:
        마스킹된 키. 너무 짧거나 None이면 "***".

    Examples:
        >>> _mask_key("abcd1234efgh5678")
        'abcd****5678'
    """
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "****" + key[-4:]


def _safe_log_replace(text: str, key: Optional[str]) -> str:
    """로그 출력 전 API 키를 마스킹한 문자열 반환.

    Args:
        text: 원본 로그 문자열
        key:  마스킹할 API 키 (None이면 원본 반환)

    Returns:
        마스킹된 로그 문자열
    """
    if key:
        return text.replace(key, _mask_key(key))
    return text
