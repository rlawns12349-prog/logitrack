"""
solver.py — OR-Tools VRPTW 솔버 및 재무 계산
  - solve_vrptw: 경로 최적화
  - calc_nn_distance_real: Nearest-Neighbor 기준 거리
  - diagnose_unassigned: 배차 불가 원인 진단
  - compute_truck_financials: 차량별 비용 계산
  - _mask_key / _safe_log_replace: API 키 마스킹 (B-10, R-2)
"""
import math
import logging

from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from geo import DIESEL_EMISSION_FACTOR  # noqa: F401 (re-export for app.py convenience)

logger = logging.getLogger("logitrack")


# ── 시간 창 유틸 ─────────────────────────────────
def _safe_tw(node):
    s = max(0, int(node.get('tw_start', 0)))
    e = max(s + 1, int(node.get('tw_end', 630)))
    return s, e


# ── VRPTW 솔버 ───────────────────────────────────
def solve_vrptw(
    nodes, v_weights, v_volumes, v_costs, v_names, v_skills, v_temp_caps,
    combined_matrix, balance_workload, max_work_minutes, time_limit_sec=5,
):
    size = len(nodes)
    nv   = len(v_weights)
    if nv == 0:
        return None, ["차량이 없습니다."], [], []
    if size <= 1:
        return None, ["배송지가 없습니다."], [], []

    int_wt   = [int(math.ceil(n.get('weight', 0)))       for n in nodes]
    int_vol  = [int(math.ceil(n.get('volume', 0) * 100)) for n in nodes]
    int_vwt  = [int(w)       for w in v_weights]
    int_vvol = [int(v * 100) for v in v_volumes]
    tws      = [_safe_tw(n)  for n in nodes]

    manager = pywrapcp.RoutingIndexManager(size, nv, 0)
    routing = pywrapcp.RoutingModel(manager)

    transit_cbs = []
    for v in range(nv):
        def _make(vi):
            def _cb(fi, ti):
                fn = manager.IndexToNode(fi)
                tn = manager.IndexToNode(ti)
                vm = 1.15 if "2.5톤" in v_names[vi] else (1.3 if "5톤" in v_names[vi] else 1.0)
                return int(combined_matrix[fn][tn] * v_skills[vi] * vm)
            return _cb
        ci = routing.RegisterTransitCallback(_make(v))
        transit_cbs.append(ci)
        routing.SetArcCostEvaluatorOfVehicle(ci, v)

    solver_max = max_work_minutes - 30 if max_work_minutes >= 240 else max_work_minutes
    routing.AddDimensionWithVehicleTransits(transit_cbs, 120, int(solver_max), False, 'Time')
    td = routing.GetDimensionOrDie('Time')

    if balance_workload:
        td.SetGlobalSpanCostCoefficient(100)

    for v in range(nv):
        routing.SetFixedCostOfVehicle(int(v_costs[v]), v)

    for i in range(1, size):
        pri = nodes[i].get('priority', '일반')
        pen = 50_000_000 if pri == 'VIP' else (100_000 if pri == '여유' else 5_000_000)
        routing.AddDisjunction([manager.NodeToIndex(i)], pen)

    for i, (s, e) in enumerate(tws):
        idx = manager.NodeToIndex(i)
        td.CumulVar(idx).SetMin(s)
        if nodes[i].get('tw_type', 'Hard') == 'Soft':
            td.SetCumulVarSoftUpperBound(idx, e, 10_000)
        else:
            td.CumulVar(idx).SetMax(e)
        nt = nodes[i].get('temperature', '상온')
        for v in range(nv):
            if nt not in v_temp_caps[v]:
                routing.VehicleVar(idx).RemoveValue(v)

    # P-4 fix: default 인수로 리스트를 명시적으로 캡처 (late binding 방지)
    wi = routing.RegisterUnaryTransitCallback(
        lambda f, wt=int_wt: wt[manager.IndexToNode(f)])
    vi = routing.RegisterUnaryTransitCallback(
        lambda f, vol=int_vol: vol[manager.IndexToNode(f)])
    routing.AddDimensionWithVehicleCapacity(wi, 0, int_vwt,  True, 'Weight')
    routing.AddDimensionWithVehicleCapacity(vi, 0, int_vvol, True, 'Volume')

    sp = pywrapcp.DefaultRoutingSearchParameters()
    sp.first_solution_strategy    = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    sp.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    sp.time_limit.FromSeconds(int(time_limit_sec))
    solution = routing.SolveWithParameters(sp)

    if not solution:
        return None, ["해를 찾을 수 없습니다. 제약을 완화하세요."], [], []

    unassigned = []
    for i in range(1, size):
        idx = manager.NodeToIndex(i)
        if solution.Value(routing.NextVar(idx)) == idx:
            unassigned.append(nodes[i])

    routes, used_vi = [], []
    for vid in range(nv):
        idx  = routing.Start(vid)
        plan = []
        while not routing.IsEnd(idx):
            ni = manager.IndexToNode(idx)
            plan.append({**nodes[ni], "arr_min": solution.Min(td.CumulVar(idx))})
            idx = solution.Value(routing.NextVar(idx))
        plan.append({**nodes[manager.IndexToNode(idx)], "arr_min": solution.Min(td.CumulVar(idx))})
        if len(plan) > 2:
            routes.append(plan)
            used_vi.append({
                "name":       v_names[vid],
                "max_weight": v_weights[vid],
                "max_volume": v_volumes[vid],
                "cost":       v_costs[vid],
                "skill":      v_skills[vid],
            })

    return routes, [], unassigned, used_vi


# ── 진단 및 재무 ─────────────────────────────────
def calc_nn_distance_real(real_dist_matrix, nodes_data):
    """Nearest-Neighbor 휴리스틱으로 기준 거리 계산 (효율성 지표용)
    S-1: unvisited를 set으로 관리해 remove를 O(n)→O(1)로 개선
    """
    size = len(nodes_data)
    if size <= 1:
        return 0.0
    unvisited = set(range(1, size))
    curr  = 0
    total = 0.0
    while unvisited:
        nxt = min(unvisited, key=lambda j: real_dist_matrix[curr][j])
        total += real_dist_matrix[curr][nxt]
        curr   = nxt
        unvisited.discard(nxt)  # set.discard: O(1)
    total += real_dist_matrix[curr][0]
    return total


def diagnose_unassigned(node, v_temp_caps, v_weights, v_volumes):
    # S-2: v_names는 실제로 사용되지 않으므로 서명에서 제거
    reasons = []
    nt = node.get('temperature', '상온')
    nw = node.get('weight', 0)
    nv = node.get('volume', 0)
    s, e = _safe_tw(node)
    if not any(nt in c for c in v_temp_caps):
        reasons.append(f"온도({nt}) 미지원")
    if not any(nw <= w for w in v_weights):
        reasons.append(f"중량 초과({nw}kg > 최대{max(v_weights)}kg)")
    if not any(nv <= v for v in v_volumes):
        reasons.append(f"부피 초과({nv}CBM > 최대{max(v_volumes)}CBM)")
    if e - s < 30:
        reasons.append(f"시간창 협소({node.get('tw_disp', '?')})")
    if not reasons:
        reasons.append("근로시간/복합 제약 초과")
    return ", ".join(reasons)


def compute_truck_financials(s: dict, fuel_price: int, labor_per_hour: int) -> dict:
    tf = s['fuel_liter'] * fuel_price
    tl = (s['time'] / 60) * labor_per_hour
    return {
        "fuel_cost":      tf,
        "labor_cost":     tl,
        "total_variable": tf + tl + s['toll_cost'],
        "grand_total":    tf + tl + s['toll_cost'] + s['cost'],
    }


# ── API 키 마스킹 (B-10, R-2) ────────────────────
def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "****" + key[-4:]


def _safe_log_replace(text: str, key: str) -> str:
    """로그 출력 전 키를 마스킹한 문자열 반환"""
    if key:
        return text.replace(key, _mask_key(key))
    return text
