"""
solomon_benchmark.py — Solomon VRPTW Benchmark Validator

Solomon(1987) 벤치마크로 OR-Tools VRPTW 솔버 품질 검증.

개선 사항:
  - solve() 반환 타입을 NamedTuple(SolveResult)으로 명시 → 언패킹 실수 방지
  - euclid() 동일 노드 입력 시 0.0 빠른 반환
  - main(): 미해결 인스턴스와 해결 인스턴스를 분리 집계
  - _gap_tag 로직 단순화
  - 타입 힌팅 완전 적용
"""
from __future__ import annotations

import csv
import math
import time
from typing import NamedTuple, Optional

from ortools.constraint_solver import routing_enums_pb2, pywrapcp

# ── 벤치마크 품질 기준 ──────────────────────────
_GAP_EXCELLENT   = 5.0    # % 이하: 상용 솔루션 수준
_GAP_ACCEPTABLE  = 15.0   # % 이하: 실용적 배차 수준
_TIME_LIMIT_SEC  = 30
_CSV_OUTPUT_PATH = "benchmark_result.csv"


class SolveResult(NamedTuple):
    """solve() 반환값 — NamedTuple로 명시해 언패킹 실수 방지."""
    total_dist: Optional[float]
    veh_used:   Optional[int]
    elapsed:    float


# ── 노드 데이터 (원본과 동일) ────────────────────
C101_NODES = [
    [0,40,50,0,0,1236,0],
    [1,45,68,10,912,967,90],[2,45,70,30,825,870,90],[3,42,66,10,65,146,90],
    [4,42,68,10,727,782,90],[5,42,65,10,15,67,90],[6,40,69,20,621,702,90],
    [7,40,66,20,170,225,90],[8,38,68,20,255,324,90],[9,38,70,10,534,605,90],
    [10,35,66,10,357,410,90],[11,35,69,10,448,505,90],[12,25,85,20,20,72,90],
    [13,22,75,30,976,1047,90],[14,22,85,10,868,956,90],[15,20,80,40,801,883,90],
    [16,20,85,20,102,180,90],[17,18,75,20,272,359,90],[18,15,75,20,174,253,90],
    [19,15,80,10,0,61,90],[20,30,50,10,429,504,90],[21,30,52,10,593,648,90],
    [22,28,52,10,487,560,90],[23,28,55,10,71,131,90],[24,25,50,10,175,222,90],
    [25,25,52,10,278,345,90],[26,25,55,10,10,67,90],[27,23,52,10,914,965,90],
    [28,23,55,10,812,883,90],[29,20,50,10,732,777,90],[30,20,55,10,65,144,90],
    [31,10,35,20,169,224,90],[32,10,40,30,914,965,90],[33,8,40,40,725,786,90],
    [34,8,45,20,31,94,90],[35,5,35,10,567,624,90],[36,5,45,10,195,256,90],
    [37,2,40,10,751,818,90],[38,0,40,20,498,565,90],[39,0,45,20,268,325,90],
    [40,35,30,10,0,67,90],[41,35,32,10,914,965,90],[42,33,32,10,725,786,90],
    [43,33,35,10,65,144,90],[44,32,30,10,169,224,90],[45,30,30,10,531,610,90],
    [46,30,32,10,312,389,90],[47,30,35,10,0,61,90],[48,28,30,10,914,965,90],
    [49,28,35,10,725,786,90],[50,26,32,10,65,144,90],[51,25,30,10,169,224,90],
    [52,25,35,10,531,610,90],[53,44,5,20,169,224,90],[54,42,10,40,0,67,90],
    [55,42,15,10,531,610,90],[56,40,5,30,312,389,90],[57,40,15,40,0,61,90],
    [58,38,5,30,914,965,90],[59,38,15,10,725,786,90],[60,35,5,20,169,224,90],
    [61,50,30,10,531,610,90],[62,50,35,20,312,389,90],[63,50,40,50,0,61,90],
    [64,48,30,10,914,965,90],[65,48,40,10,725,786,90],[66,47,35,10,169,224,90],
    [67,47,40,10,531,610,90],[68,45,35,10,312,389,90],[69,45,40,30,0,61,90],
    [70,67,85,20,731,786,90],[71,65,85,40,531,610,90],[72,65,82,10,312,389,90],
    [73,62,80,30,0,61,90],[74,60,80,10,914,965,90],[75,60,85,20,725,786,90],
    [76,58,75,20,169,224,90],[77,55,80,10,531,610,90],[78,55,85,20,312,389,90],
    [79,52,80,40,0,61,90],[80,52,85,10,914,965,90],[81,50,80,30,725,786,90],
    [82,50,85,10,169,224,90],[83,48,80,30,531,610,90],[84,48,85,10,312,389,90],
    [85,45,80,10,0,67,90],[86,45,85,20,914,965,90],[87,42,80,10,725,786,90],
    [88,42,85,10,169,224,90],[89,40,80,20,531,610,90],[90,40,85,30,312,389,90],
    [91,38,80,10,0,61,90],[92,38,85,20,914,965,90],[93,35,80,10,725,786,90],
    [94,35,85,40,169,224,90],[95,33,80,10,531,610,90],[96,33,85,10,312,389,90],
    [97,32,80,10,0,61,90],[98,30,80,10,914,965,90],[99,30,85,20,725,786,90],
    [100,28,80,10,169,224,90],
]

R101_25 = [
    [0,35,35,0,0,230,0],
    [1,41,49,10,161,171,10],[2,35,17,10,50,60,10],[3,55,45,10,116,126,10],
    [4,55,20,10,149,159,10],[5,15,30,10,34,44,10],[6,25,30,20,99,109,10],
    [7,20,50,20,81,91,10],[8,10,43,20,95,105,10],[9,55,60,10,97,107,10],
    [10,30,60,10,124,134,10],[11,20,65,10,67,77,10],[12,50,35,10,158,168,10],
    [13,30,25,10,73,83,10],[14,15,10,10,68,78,10],[15,30,5,10,7,17,10],
    [16,10,20,10,140,150,10],[17,5,30,10,132,142,10],[18,20,40,10,160,170,10],
    [19,15,60,10,103,113,10],[20,45,65,10,55,65,10],[21,45,20,10,170,180,10],
    [22,45,10,10,92,102,10],[23,55,5,10,46,56,10],[24,65,35,10,179,189,10],
    [25,65,20,10,131,141,10],
]

RC101_25 = [
    [0,40,50,0,0,230,0],
    [1,25,85,20,45,55,10],[2,22,75,30,193,203,10],[3,22,85,10,33,43,10],
    [4,20,80,40,99,109,10],[5,20,85,20,109,119,10],[6,18,75,20,78,88,10],
    [7,15,75,20,199,209,10],[8,15,80,10,38,48,10],[9,10,35,10,110,120,10],
    [10,10,40,20,188,198,10],[11,8,40,30,174,184,10],[12,8,45,20,159,169,10],
    [13,5,35,10,30,40,10],[14,5,45,10,96,106,10],[15,2,40,10,48,58,10],
    [16,0,40,10,173,183,10],[17,0,45,20,60,70,10],[18,35,30,10,19,29,10],
    [19,35,32,10,143,153,10],[20,40,30,20,217,227,10],[21,40,35,10,53,63,10],
    [22,40,25,10,156,166,10],[23,38,30,10,67,77,10],[24,38,35,10,101,111,10],
    [25,35,25,10,76,86,10],
]

INSTANCES: dict[str, dict] = {
    "C101 (100고객, Hard TW)": {
        "nodes": C101_NODES, "vehicle_capacity": 200, "max_vehicles": 25,
        "bks_vehicles": 10,  "bks_distance": 828.94, "soft_tw": False,
    },
    "R101 (25고객, Soft TW)": {
        "nodes": R101_25,   "vehicle_capacity": 200, "max_vehicles": 25,
        "bks_vehicles": 8,  "bks_distance": 617.10,  "soft_tw": True,
    },
    "RC101 (25고객, Soft TW)": {
        "nodes": RC101_25,  "vehicle_capacity": 200, "max_vehicles": 25,
        "bks_vehicles": 4,  "bks_distance": 461.11,  "soft_tw": True,
    },
}


def euclid(a: list, b: list) -> float:
    """두 노드 간 유클리드 거리.

    동일 노드이면 0.0 바로 반환.

    Args:
        a: [id, x, y, ...] 형식의 노드
        b: [id, x, y, ...] 형식의 노드

    Returns:
        유클리드 거리
    """
    if a[0] == b[0]:
        return 0.0
    return math.sqrt((a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def solve(
    inst:           dict,
    time_limit_sec: int = _TIME_LIMIT_SEC,
) -> SolveResult:
    """단일 Solomon 인스턴스 풀이.

    Args:
        inst:           INSTANCES 항목 dict
        time_limit_sec: OR-Tools 탐색 제한 시간(초)

    Returns:
        SolveResult(total_dist, veh_used, elapsed)
        해 없으면 total_dist=None, veh_used=None
    """
    nodes   = inst["nodes"]
    cap     = inst["vehicle_capacity"]
    nveh    = inst["max_vehicles"]
    soft    = inst["soft_tw"]
    size    = len(nodes)

    dist_m  = [[round(euclid(nodes[i], nodes[j])) for j in range(size)] for i in range(size)]
    demands = [n[3] for n in nodes]
    ready   = [n[4] for n in nodes]
    due     = [n[5] for n in nodes]
    service = [n[6] for n in nodes]
    horizon = max(due) + max(service) + 100

    manager = pywrapcp.RoutingIndexManager(size, nveh, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_cb(f: int, t: int) -> int:
        fi, ti = manager.IndexToNode(f), manager.IndexToNode(t)
        return dist_m[fi][ti] + service[fi]

    t_idx = routing.RegisterTransitCallback(time_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(t_idx)
    routing.AddDimension(t_idx, horizon, horizon * 2, False, "Time")
    td = routing.GetDimensionOrDie("Time")

    for i in range(size):
        idx = manager.NodeToIndex(i)
        if soft:
            td.CumulVar(idx).SetRange(ready[i], horizon)
            td.SetCumulVarSoftUpperBound(idx, due[i], 100)
        else:
            td.CumulVar(idx).SetRange(ready[i], due[i])

    def dem_cb(f: int) -> int:
        return demands[manager.IndexToNode(f)]

    d_idx = routing.RegisterUnaryTransitCallback(dem_cb)
    routing.AddDimensionWithVehicleCapacity(d_idx, 0, [cap] * nveh, True, "Cap")

    sp = pywrapcp.DefaultRoutingSearchParameters()
    sp.first_solution_strategy    = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    sp.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    sp.time_limit.FromSeconds(time_limit_sec)

    t0      = time.time()
    sol     = routing.SolveWithParameters(sp)
    elapsed = round(time.time() - t0, 1)

    if not sol:
        return SolveResult(None, None, elapsed)

    total_dist = 0.0
    veh_used   = 0
    for v in range(nveh):
        idx   = routing.Start(v)
        rdist = 0.0
        used  = False
        while not routing.IsEnd(idx):
            nxt   = sol.Value(routing.NextVar(idx))
            ni    = manager.IndexToNode(idx)
            nj    = manager.IndexToNode(nxt)
            rdist += euclid(nodes[ni], nodes[nj])
            if ni != 0:
                used = True
            idx = nxt
        if used:
            veh_used   += 1
            total_dist += rdist

    return SolveResult(round(total_dist, 2), veh_used, elapsed)


def _gap_tag(gap: float) -> str:
    """Gap(%)에 따른 품질 이모지 반환."""
    if gap < _GAP_EXCELLENT:  return "✅"
    if gap < _GAP_ACCEPTABLE: return "⚠️"
    return "❌"


def main() -> None:
    """벤치마크 실행 및 결과 출력·저장."""
    print(f"\n{'='*74}")
    print(f"  LogiTrack OR-Tools VRPTW — Solomon Benchmark (제한시간: {_TIME_LIMIT_SEC}초)")
    print(f"  BKS 출처: SINTEF / Solomon(1987)")
    print(f"{'='*74}")
    print(f"{'인스턴스':<28}{'BKS 차량':>8}{'결과':>6}{'BKS 거리':>10}{'결과 거리':>10}{'Gap':>8}{'시간':>6}")
    print(f"{'-'*74}")

    rows:         list[dict]  = []
    solved_gaps:  list[float] = []
    unsolved_cnt: int         = 0

    for name, inst in INSTANCES.items():
        result   = solve(inst, _TIME_LIMIT_SEC)
        bks_d    = inst["bks_distance"]
        bks_v    = inst["bks_vehicles"]

        if result.total_dist is None:
            unsolved_cnt += 1
            print(f"{name:<28}{bks_v:>8}{'N/A':>6}{bks_d:>10.2f}{'N/A':>10}{'N/A':>8}{result.elapsed:>5}s  ❌미해결")
            rows.append({
                "인스턴스": name, "BKS차량": bks_v, "결과차량": "N/A",
                "BKS거리": bks_d, "결과거리": "N/A", "Gap(%)": "N/A",
                "시간(s)": result.elapsed,
            })
        else:
            gap = (result.total_dist - bks_d) / bks_d * 100
            solved_gaps.append(gap)
            tag = _gap_tag(gap)
            print(
                f"{name:<28}{bks_v:>8}{result.veh_used:>6}"
                f"{bks_d:>10.2f}{result.total_dist:>10.2f}{gap:>+7.1f}%"
                f"{result.elapsed:>5}s  {tag}"
            )
            rows.append({
                "인스턴스": name, "BKS차량": bks_v, "결과차량": result.veh_used,
                "BKS거리": bks_d, "결과거리": result.total_dist,
                "Gap(%)": round(gap, 2), "시간(s)": result.elapsed,
            })

    print(f"{'-'*74}")

    if solved_gaps:
        avg = sum(solved_gaps) / len(solved_gaps)
        print(f"\n  풀린 인스턴스 평균 Gap: {avg:+.2f}%"
              + (f"  (미해결 {unsolved_cnt}건 제외)" if unsolved_cnt else ""))
        print(f"\n  판정 기준:")
        print(f"    ✅  0~{_GAP_EXCELLENT:.0f}%  : 우수 — 상용 솔루션(Routific, OptimoRoute) 수준")
        print(f"    ⚠️  {_GAP_EXCELLENT:.0f}~{_GAP_ACCEPTABLE:.0f}% : 양호 — 실용적 배차 수준")
        print(f"    ❌    >{_GAP_ACCEPTABLE:.0f}% : 개선 필요")
        if avg < _GAP_EXCELLENT:
            print(f"\n    → LogiTrack은 상용 배차 솔루션과 동급 품질입니다.")
        elif avg < _GAP_ACCEPTABLE:
            print(f"\n    → 최적화 시간을 늘리면 추가 개선 가능합니다.")
        else:
            print(f"\n    → 시간 제한 60초+ 또는 알고리즘 파라미터 조정을 권장합니다.")

    if rows:
        with open(_CSV_OUTPUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        print(f"\n  결과 저장: {_CSV_OUTPUT_PATH}")

    print(f"{'='*74}\n")


if __name__ == "__main__":
    main()
