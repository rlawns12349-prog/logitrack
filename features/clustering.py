"""
features/clustering.py — D9 배송권역 자동 클러스터링

버그 수정:
  - 이전 코드의 일치율 계산은 같은 클러스터에 속한 노드 수만큼 mismatch를
    중복 카운트하는 문제가 있었음.
  - 수정: 클러스터 단위로 판정 (클러스터 내 실제 차량이 2대 이상 → 해당
    클러스터의 노드 전체를 불일치로 집계)해서 노드별 1회만 카운트.
"""
from collections import defaultdict

import pandas as pd
import streamlit as st


def _kmeans_simple(
    points: list[tuple[float, float]],
    k: int,
    iters: int = 30,
) -> list[int]:
    """외부 라이브러리 없는 k-means (Lloyd's algorithm).

    Args:
        points: [(lat, lon), ...] 좌표 리스트
        k:      클러스터 수
        iters:  최대 반복 횟수

    Returns:
        각 포인트의 클러스터 레이블 리스트
    """
    if not points or k <= 0:
        return []
    k = min(k, len(points))

    step    = max(1, len(points) // k)
    centers = [list(points[i * step]) for i in range(k)]
    labels  = [0] * len(points)

    for _ in range(iters):
        for j, (lat, lon) in enumerate(points):
            best, best_d = 0, float("inf")
            for ci, (clat, clon) in enumerate(centers):
                d = (lat - clat) ** 2 + (lon - clon) ** 2
                if d < best_d:
                    best_d, best = d, ci
            labels[j] = best

        new_centers = [[0.0, 0.0, 0] for _ in range(k)]
        for j, (lat, lon) in enumerate(points):
            c = labels[j]
            new_centers[c][0] += lat
            new_centers[c][1] += lon
            new_centers[c][2] += 1
        for ci in range(k):
            cnt = new_centers[ci][2]
            if cnt > 0:
                centers[ci] = [
                    new_centers[ci][0] / cnt,
                    new_centers[ci][1] / cnt,
                ]
    return labels


def _calc_match_pct(
    all_nodes: list[dict],
    labels: list[int],
    actual_clusters: dict[str, int],
) -> float:
    """제안 권역과 실제 배차 결과의 일치율을 계산한다.

    버그 수정 포인트:
        이전 구현은 각 노드마다 '속한 클러스터 전체'의 차량 분포를 확인하고
        불일치 시 +1 했기 때문에 같은 클러스터의 노드 수 × 중복 집계됐음.

        수정된 로직: 클러스터별로 딱 한 번 판정 후,
        불일치 클러스터에 속한 노드 수를 합산한다.

    Args:
        all_nodes:       배송지 노드 리스트
        labels:          각 노드의 제안 클러스터 레이블
        actual_clusters: {거점명: 실제 차량 인덱스} dict

    Returns:
        일치율 (0~100 float)
    """
    if not all_nodes:
        return 100.0

    # 클러스터별로 실제 차량 집합 구성
    cluster_vehicles: dict[int, set[int]] = defaultdict(set)
    cluster_node_count: dict[int, int]    = defaultdict(int)

    for j, node in enumerate(all_nodes):
        c  = labels[j]
        av = actual_clusters.get(node.get("name", ""), -1)
        cluster_vehicles[c].add(av)
        cluster_node_count[c] += 1

    # 불일치 클러스터에 속한 노드 수 합산 (클러스터당 1회만 판정)
    mismatch_nodes = sum(
        cluster_node_count[c]
        for c, vehicles in cluster_vehicles.items()
        if len(vehicles) > 1
    )

    return max(0.0, round((1 - mismatch_nodes / len(all_nodes)) * 100, 1))


def render_cluster_analysis(res: dict) -> None:
    """[D9] 배송권역 자동 클러스터링 시각화."""
    with st.expander("🗺️ 배송권역 클러스터링 분석", expanded=False):
        st.caption(
            "배송지 좌표를 자동 군집화해 최적 권역 분할을 제안합니다. "
            "실제 배차 결과와 비교해 권역 재설계 여부를 판단하세요."
        )

        routes   = res.get("routes", [])
        hub_name = res.get("hub_name", "")

        if not routes:
            st.info("배차 결과가 없습니다.")
            return

        all_nodes: list[dict] = [
            n
            for plan in routes
            for n in plan
            if n.get("name") != hub_name and "lat" in n and "lon" in n
        ]

        if len(all_nodes) < 2:
            st.info("클러스터링에 필요한 배송지가 부족합니다.")
            return

        n_clusters = st.slider(
            "권역 수 (k)", 2, min(8, len(all_nodes)), len(routes), key="cluster_k"
        )
        points = [(n["lat"], n["lon"]) for n in all_nodes]
        labels = _kmeans_simple(points, n_clusters)

        # 클러스터별 통계 테이블
        cluster_stats: dict[int, dict] = defaultdict(
            lambda: {"nodes": [], "total_w": 0.0, "total_v": 0.0}
        )
        for j, node in enumerate(all_nodes):
            c = labels[j]
            cluster_stats[c]["nodes"].append(node.get("name", "?"))
            cluster_stats[c]["total_w"] += node.get("weight", 0)
            cluster_stats[c]["total_v"] += node.get("volume", 0)

        rows = [
            {
                "권역":      f"권역 {ci + 1}",
                "배송지 수": len(st_data["nodes"]),
                "총 무게":   f"{st_data['total_w']:.1f}kg",
                "총 부피":   f"{st_data['total_v']:.2f}CBM",
                "거점 목록": ", ".join(st_data["nodes"][:5])
                             + ("..." if len(st_data["nodes"]) > 5 else ""),
            }
            for ci, st_data in sorted(cluster_stats.items())
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        # 실제 배차 클러스터 맵 구성
        actual_clusters: dict[str, int] = {
            n.get("name", ""): vi
            for vi, plan in enumerate(routes)
            for n in plan
            if n.get("name") != hub_name
        }

        # ── 수정된 일치율 계산 ────────────────
        match_pct = _calc_match_pct(all_nodes, labels, actual_clusters)

        col1, col2 = st.columns(2)
        col1.metric(
            "권역-배차 일치율", f"{match_pct}%",
            help="제안 권역과 실제 배차 결과가 얼마나 일치하는지"
        )
        col2.metric("분석 배송지 수", len(all_nodes))

        if match_pct < 70:
            st.warning(
                f"⚠️ 배차 일치율 {match_pct}% — 현재 배송지들이 권역 경계를 많이 "
                "넘어 배차되고 있습니다. 허브 위치 재검토 또는 권역 기반 사전 "
                "필터링을 권장합니다."
            )
