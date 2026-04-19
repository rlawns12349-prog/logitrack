"""
routing.py — 경로 API 및 실측 매트릭스
  - Kakao 좌표 변환 / 경로 조회
  - build_real_time_matrix: 비동기 배치 요청 → 시간/거리/통행료 행렬 반환
"""
import asyncio
import logging

import aiohttp
import requests

from geo import manhattan_distance, get_dynamic_speed

logger = logging.getLogger("logitrack")


def get_kakao_coordinate(address: str, kakao_key: str):
    """주소 → (lat, lon, formatted_addr, error)"""
    headers = {"Authorization": f"KakaoAK {kakao_key}"}
    try:
        for url in [
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            "https://dapi.kakao.com/v2/local/search/address.json",
        ]:
            res = requests.get(url, headers=headers, params={"query": address}, timeout=5)
            if res.status_code == 200:
                body = res.json()
                if body.get('documents'):
                    d = body['documents'][0]
                    best = d.get('place_name') or d.get('road_address_name') or d.get('address_name')
                    return float(d['y']), float(d['x']), best, None
        return None, None, None, "주소를 찾을 수 없습니다."
    except requests.RequestException as e:
        logger.warning("get_kakao_coordinate: %s", e)
        return None, None, None, "API 오류"


async def fetch_route_core(
    session, curr, dest, speed, congestion_penalty, weather_factor,
    kakao_key: str, api_cache, db,
):
    cache_key = f"DIR_{curr['lat']:.4f},{curr['lon']:.4f}_{dest['lat']:.4f},{dest['lon']:.4f}"

    # 1) 메모리 캐시
    cached = api_cache.get(cache_key)
    if cached:
        r = cached.copy()
        r['from'] = curr['name']; r['to'] = dest['name']
        r['time'] = r['raw_time'] * weather_factor
        return r

    # 2) DB 캐시
    db_cache = db.get_route_cache(cache_key)
    if db_cache:
        if 'raw_time' not in db_cache:
            db_cache['raw_time'] = db_cache.get('time', 0)
        api_cache.set(cache_key, db_cache)
        r = db_cache.copy()
        r['from'] = curr['name']; r['to'] = dest['name']
        r['time'] = r['raw_time'] * weather_factor
        return r

    # 3) Kakao Mobility API
    params = {
        "origin":      f"{curr['lon']},{curr['lat']}",
        "destination": f"{dest['lon']},{dest['lat']}",
        "priority":    "RECOMMEND",
    }
    # 타임아웃을 태스크 외부에서 생성하지 않고 숫자로 직접 전달 (Timeout context manager 에러 수정)
    for attempt in range(3):
        try:
            async with session.get(
                "https://apis-navi.kakaomobility.com/v1/directions",
                headers={"Authorization": f"KakaoAK {kakao_key}"},
                params=params,
                timeout=aiohttp.ClientTimeout(total=5, ceil_threshold=5),
            ) as res:
                if res.status == 200:
                    data = await res.json()
                    routes = data.get('routes', [])
                    if routes:
                        r       = routes[0]
                        path_yx = []
                        for sec in r['sections']:
                            for rd in sec['roads']:
                                v = rd['vertexes']
                                for i in range(0, len(v), 2):
                                    path_yx.append([v[i + 1], v[i]])
                        raw_t = r['summary']['duration'] / 60
                        cd = {
                            "from": curr['name'], "to": dest['name'],
                            "dist": r['summary']['distance'] / 1000,
                            "raw_time": raw_t,
                            "toll": r['summary'].get('fare', {}).get('toll', 0),
                            "path": path_yx, "is_fallback": False,
                        }
                        api_cache.set(cache_key, cd)
                        db.save_route_cache(cache_key, cd)
                        result = cd.copy()
                        result['time'] = raw_t * weather_factor
                        return result
                    break
                elif res.status == 429:
                    pass
                else:
                    break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("fetch_route attempt %d: %s", attempt, e)
        await asyncio.sleep(1.5 ** attempt)

    # 4) 폴백: Manhattan 추정
    d_km  = manhattan_distance(curr['lat'], curr['lon'], dest['lat'], dest['lon'])
    spd   = get_dynamic_speed(dest['lat'], dest['lon'], speed, congestion_penalty, weather_factor)
    raw_t = (d_km / spd) * 60
    fb = {
        "from": curr['name'], "to": dest['name'],
        "dist": d_km, "raw_time": raw_t, "time": raw_t,
        "toll": 0,
        "path": [[curr['lat'], curr['lon']], [dest['lat'], dest['lon']]],
        "is_fallback": True,
    }
    api_cache.set(cache_key, fb)
    db.save_route_cache(cache_key, fb)
    return fb


async def build_real_time_matrix(
    nodes, speed, congestion_penalty, weather_factor,
    base_service_min, sec_per_kg,
    kakao_key: str, api_cache, db,
):
    """
    노드 리스트 → (combined, travel_time, service_time, dist, toll) 행렬 반환
    combined[i][j] = travel_time[i][j] + service_time[j]
    """
    size = len(nodes)
    travel_time_matrix = [[0]   * size for _ in range(size)]
    service_time_list  = [0]   * size
    real_dist_matrix   = [[0.0] * size for _ in range(size)]
    real_toll_matrix   = [[0]   * size for _ in range(size)]

    for j, node in enumerate(nodes):
        if j == 0:
            continue
        wt   = node.get('weight', 0)
        vol  = node.get('volume', 0)
        diff = node.get('difficulty', '일반')
        pen  = 10 if '보안' in diff else (15 if '재래' in diff else 0)
        if node.get('unload_method', '수작업') == '지게차':
            svc = base_service_min + pen
        else:
            svc = base_service_min + max(wt * sec_per_kg / 60.0, vol * 100 * sec_per_kg / 60.0) + pen
        service_time_list[j] = svc

    def rush_mult(node):
        tw = node.get('tw_start', 0)
        return 1.3 if ((-120 <= tw <= 0) or (480 <= tw <= 600)) else 1.0

    pair_index_list = [
        ((i, j), (nodes[i], nodes[j]))
        for i in range(size)
        for j in range(size)
        if i != j
    ]

    results = []
    # 커넥터 타임아웃도 ceil_threshold 적용
    connector = aiohttp.TCPConnector()
    timeout   = aiohttp.ClientTimeout(total=10, ceil_threshold=5)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
        all_pairs = [p for _, p in pair_index_list]
        for i in range(0, len(all_pairs), 15):
            chunk   = all_pairs[i:i + 15]
            chunk_r = await asyncio.gather(*[
                fetch_route_core(
                    sess, c, d, speed, congestion_penalty, weather_factor,
                    kakao_key, api_cache, db,
                )
                for c, d in chunk
            ])
            results.extend(chunk_r)
            await asyncio.sleep(0.3)

    for k, res in enumerate(results):
        (i, j), _ = pair_index_list[k]
        travel_time_matrix[i][j] = int(res['time'] * rush_mult(nodes[j]))
        real_dist_matrix[i][j]   = res['dist']
        real_toll_matrix[i][j]   = res.get('toll', 0)

    combined = [[0] * size for _ in range(size)]
    for i in range(size):
        for j in range(size):
            if i != j:
                combined[i][j] = travel_time_matrix[i][j] + int(service_time_list[j])

    return combined, travel_time_matrix, service_time_list, real_dist_matrix, real_toll_matrix
