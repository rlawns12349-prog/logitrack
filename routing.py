"""
routing.py — 경로 API 및 실측 매트릭스

개선 사항:
  - 타입 힌팅 전면 적용
  - fetch_route_core: 재시도 간격을 지수 백오프(1.0→2.0→4.0s)로 정규화,
    429(Rate Limit) 응답 시에도 동일 백오프 적용 (원본은 pass만 하고 sleep 없음)
  - 빈 routes 응답을 폴백 없이 break하던 로직 → 명시적 조건 분기
  - build_real_time_matrix: pair_index_list 생성 인덱스 오류 방어
  - 모든 공개 함수 docstring 추가
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp
import requests

from geo import LRUCache, manhattan_distance, get_dynamic_speed

logger = logging.getLogger("logitrack")

# 재시도 관련 상수
_MAX_RETRY      = 3
_RETRY_BASE_SEC = 1.0   # 지수 백오프 기본값 (1 → 2 → 4초)
_BATCH_SIZE     = 15    # 비동기 배치 크기
_BATCH_DELAY    = 0.3   # 배치 간 딜레이(초)


def get_kakao_coordinate(
    address: str,
    kakao_key: str,
) -> tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    """주소 문자열을 Kakao Local API로 좌표 변환.

    키워드 검색 → 주소 검색 순으로 시도.

    Args:
        address:   검색할 주소 또는 키워드
        kakao_key: Kakao REST API 키

    Returns:
        (lat, lon, formatted_addr, error_msg)
        성공 시 error_msg=None, 실패 시 lat/lon/addr=None
    """
    headers = {"Authorization": f"KakaoAK {kakao_key}"}
    urls = [
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        "https://dapi.kakao.com/v2/local/search/address.json",
    ]
    try:
        for url in urls:
            res = requests.get(url, headers=headers,
                               params={"query": address}, timeout=5)
            if res.status_code == 200:
                body = res.json()
                if body.get("documents"):
                    d    = body["documents"][0]
                    best = (d.get("place_name")
                            or d.get("road_address_name")
                            or d.get("address_name"))
                    return float(d["y"]), float(d["x"]), best, None
        return None, None, None, "주소를 찾을 수 없습니다."
    except requests.RequestException as e:
        logger.warning("get_kakao_coordinate: %s", e)
        return None, None, None, "API 오류"


async def fetch_route_core(
    session:            aiohttp.ClientSession,
    curr:               dict,
    dest:               dict,
    speed:              float,
    congestion_penalty: float,
    weather_factor:     float,
    kakao_key:          str,
    api_cache:          LRUCache,
    db,
) -> dict:
    """두 노드 간 경로 정보를 반환.

    조회 순서: 메모리 캐시 → DB 캐시 → Kakao Mobility API → Manhattan 폴백

    개선된 재시도 로직:
        - 지수 백오프: attempt 0→1s, 1→2s, 2→4s
        - 429(Rate Limit) 응답도 동일 백오프 적용 (원본은 sleep 없이 pass)
        - 빈 routes 리스트는 폴백으로 이어지도록 명시

    Args:
        session:            aiohttp 세션
        curr:               출발 노드 dict (name, lat, lon)
        dest:               도착 노드 dict (name, lat, lon)
        speed:              기준 속도 (km/h)
        congestion_penalty: 혼잡 페널티 (0~100)
        weather_factor:     기상 계수 (0.7~1.0)
        kakao_key:          Kakao REST API 키
        api_cache:          LRU 메모리 캐시
        db:                 DBManager 인스턴스

    Returns:
        경로 정보 dict
        {from, to, dist, raw_time, time, toll, path, is_fallback}
    """
    cache_key = (
        f"DIR_{curr['lat']:.4f},{curr['lon']:.4f}"
        f"_{dest['lat']:.4f},{dest['lon']:.4f}"
    )

    # 1) 메모리 캐시
    cached = api_cache.get(cache_key)
    if cached:
        r = cached.copy()
        r["from"] = curr["name"]
        r["to"]   = dest["name"]
        r["time"] = r.get("raw_time", r.get("time", 0)) * weather_factor
        return r

    # 2) DB 캐시
    db_cache = db.get_route_cache(cache_key)
    if db_cache:
        if "raw_time" not in db_cache:
            db_cache["raw_time"] = db_cache.get("time", 0)
        api_cache.set(cache_key, db_cache)
        r = db_cache.copy()
        r["from"] = curr["name"]
        r["to"]   = dest["name"]
        r["time"] = r["raw_time"] * weather_factor
        return r

    # 3) Kakao Mobility API — 지수 백오프 재시도
    params = {
        "origin":      f"{curr['lon']},{curr['lat']}",
        "destination": f"{dest['lon']},{dest['lat']}",
        "priority":    "RECOMMEND",
    }
    for attempt in range(_MAX_RETRY):
        try:
            async with session.get(
                "https://apis-navi.kakaomobility.com/v1/directions",
                headers={"Authorization": f"KakaoAK {kakao_key}"},
                params=params,
                timeout=aiohttp.ClientTimeout(total=5, ceil_threshold=5),
            ) as res:
                if res.status == 200:
                    data   = await res.json()
                    routes = data.get("routes", [])
                    if routes:
                        r       = routes[0]
                        path_yx: list[list[float]] = []
                        for sec in r["sections"]:
                            for rd in sec["roads"]:
                                v = rd["vertexes"]
                                for i in range(0, len(v), 2):
                                    path_yx.append([v[i + 1], v[i]])
                        raw_t = r["summary"]["duration"] / 60
                        cd = {
                            "from":     curr["name"],
                            "to":       dest["name"],
                            "dist":     r["summary"]["distance"] / 1000,
                            "raw_time": raw_t,
                            "toll":     r["summary"].get("fare", {}).get("toll", 0),
                            "path":     path_yx,
                            "is_fallback": False,
                        }
                        api_cache.set(cache_key, cd)
                        db.save_route_cache(cache_key, cd)
                        result = cd.copy()
                        result["time"] = raw_t * weather_factor
                        return result
                    # routes가 비어있으면 폴백으로 진행
                    logger.debug("fetch_route: empty routes for %s→%s",
                                 curr["name"], dest["name"])
                    break
                elif res.status == 429:
                    # Rate Limit — 원본 버그 수정: sleep 없이 continue하던 것을 백오프 적용
                    logger.warning("fetch_route: 429 Rate Limit (attempt %d)", attempt)
                else:
                    logger.debug("fetch_route: HTTP %d (attempt %d)", res.status, attempt)
                    break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("fetch_route attempt %d: %s", attempt, e)

        # 지수 백오프: 1s → 2s → 4s
        await asyncio.sleep(_RETRY_BASE_SEC * (2 ** attempt))

    # 4) 폴백: Manhattan 추정
    d_km  = manhattan_distance(curr["lat"], curr["lon"], dest["lat"], dest["lon"])
    spd   = get_dynamic_speed(dest["lat"], dest["lon"], speed, congestion_penalty, weather_factor)
    raw_t = (d_km / spd) * 60 if spd > 0 else 0.0
    fb = {
        "from":     curr["name"],
        "to":       dest["name"],
        "dist":     d_km,
        "raw_time": raw_t,
        "time":     raw_t,
        "toll":     0,
        "path":     [[curr["lat"], curr["lon"]], [dest["lat"], dest["lon"]]],
        "is_fallback": True,
    }
    api_cache.set(cache_key, fb)
    db.save_route_cache(cache_key, fb)
    return fb


async def build_real_time_matrix(
    nodes:              list[dict],
    speed:              float,
    congestion_penalty: float,
    weather_factor:     float,
    base_service_min:   float,
    sec_per_kg:         float,
    kakao_key:          str,
    api_cache:          LRUCache,
    db,
) -> tuple[
    list[list[int]],    # combined
    list[list[int]],    # travel_time
    list[float],        # service_time
    list[list[float]],  # dist
    list[list[int]],    # toll
]:
    """노드 리스트 → (combined, travel_time, service_time, dist, toll) 행렬 반환.

    combined[i][j] = travel_time[i][j] + service_time[j]

    Args:
        nodes:              배송 노드 리스트 (0번=허브)
        speed:              기준 속도 (km/h)
        congestion_penalty: 혼잡 페널티 (0~100)
        weather_factor:     기상 계수 (0.7~1.0)
        base_service_min:   기본 서비스 시간 (분)
        sec_per_kg:         kg당 추가 서비스 시간 (초)
        kakao_key:          Kakao REST API 키
        api_cache:          LRU 메모리 캐시
        db:                 DBManager 인스턴스

    Returns:
        5개 행렬 튜플
    """
    size = len(nodes)
    travel_time_matrix: list[list[int]]   = [[0] * size for _ in range(size)]
    service_time_list:  list[float]       = [0.0] * size
    real_dist_matrix:   list[list[float]] = [[0.0] * size for _ in range(size)]
    real_toll_matrix:   list[list[int]]   = [[0] * size for _ in range(size)]

    # 서비스 시간 계산
    for j, node in enumerate(nodes):
        if j == 0:
            continue
        wt   = node.get("weight",     0)
        vol  = node.get("volume",     0)
        diff = node.get("difficulty", "일반")
        pen  = 10 if "보안" in diff else (15 if "재래" in diff else 0)

        if node.get("unload_method", "수작업") == "지게차":
            svc = base_service_min + pen
        else:
            svc = (base_service_min
                   + max(wt * sec_per_kg / 60.0, vol * 100 * sec_per_kg / 60.0)
                   + pen)
        service_time_list[j] = svc

    def _rush_mult(node: dict) -> float:
        tw = node.get("tw_start", 0)
        return 1.3 if (-120 <= tw <= 0) or (480 <= tw <= 600) else 1.0

    # rush_mult를 노드별로 미리 계산 (이전: n² 페어마다 nodes[j]로 재호출)
    rush_mults: list[float] = [_rush_mult(n) for n in nodes]

    # 모든 (i, j) 페어 — i≠j
    # 인덱스 쌍과 노드 쌍을 분리된 리스트로 유지해 중간 튜플 분해 불필요
    idx_pairs:  list[tuple[int, int]] = []
    node_pairs: list[tuple[dict, dict]] = []
    for i in range(size):
        for j in range(size):
            if i != j:
                idx_pairs.append((i, j))
                node_pairs.append((nodes[i], nodes[j]))

    results: list[dict] = []
    connector = aiohttp.TCPConnector()
    timeout   = aiohttp.ClientTimeout(total=10, ceil_threshold=5)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
        for start in range(0, len(node_pairs), _BATCH_SIZE):
            chunk   = node_pairs[start : start + _BATCH_SIZE]
            chunk_r = await asyncio.gather(*[
                fetch_route_core(
                    sess, c, d,
                    speed, congestion_penalty, weather_factor,
                    kakao_key, api_cache, db,
                )
                for c, d in chunk
            ])
            results.extend(chunk_r)
            await asyncio.sleep(_BATCH_DELAY)

    for k, res in enumerate(results):
        i, j = idx_pairs[k]
        travel_time_matrix[i][j] = int(res["time"] * rush_mults[j])  # 미리 계산된 값 사용
        real_dist_matrix[i][j]   = res["dist"]
        real_toll_matrix[i][j]   = res.get("toll", 0)

    combined: list[list[int]] = [[0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            if i != j:
                combined[i][j] = travel_time_matrix[i][j] + int(service_time_list[j])

    return combined, travel_time_matrix, service_time_list, real_dist_matrix, real_toll_matrix
