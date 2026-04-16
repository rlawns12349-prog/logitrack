"""
geo.py — 공간/물리 유틸리티
  - 거리 계산 (haversine, manhattan)
  - 동적 속도·연료 계산
  - Thread-safe LRU 캐시

고도화 이력:
  v2  차량 스펙 상수 테이블 분리 (VEHICLE_SPECS)
      혼잡 구역 상수 분리 (CONGESTION_ZONES)
      penalty_pct 0~100 클리핑
      LRUCache: clear(), keys() 추가
      LRUCache: __contains__ LRU 순서 갱신
      manhattan_distance: 라디안 변환 중복 제거
"""
import math
from collections import OrderedDict
import threading
from typing import Optional

# ── 상수 ────────────────────────────────────────
DIESEL_EMISSION_FACTOR = 2.62  # kg CO2/L (GLEC Framework)

# 차량 스펙 테이블 — (기본연비 km/L, 적재량당 연비저하 계수)
# 새 차종 추가 시 이 딕셔너리만 수정
VEHICLE_SPECS: dict[str, tuple[float, float]] = {
    "1톤":   (9.0, 0.05),
    "2.5톤": (6.5, 0.04),
    "5톤":   (4.5, 0.03),
}
_VEHICLE_DEADHEAD_BONUS = 1.15  # 공차 복귀 연비 향상 배율
_MAX_FUEL_DROP_RATIO    = 0.30  # 연비 저하 최대 비율 (30%)

# 혼잡 구역 정의 — (lat_min, lat_max, lon_min, lon_max)
# 구역 추가 시 이 리스트에 append
CONGESTION_ZONES: list[tuple[float, float, float, float]] = [
    (37.40, 37.75, 126.80, 127.20),  # 서울 수도권
]


# ── 거리 계산 ────────────────────────────────────
def haversine_distance(lat1: float, lon1: float,
                       lat2: float, lon2: float) -> float:
    """두 좌표 간 구면 거리 (km), Haversine 공식"""
    R    = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi    = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def manhattan_distance(lat1: float, lon1: float,
                       lat2: float, lon2: float) -> float:
    """도로망 근사 거리 (km) — 남북 + 동서 구간 haversine 합산
    라디안 변환을 한 번만 수행해 중복 연산 제거
    """
    phi1    = math.radians(lat1)
    phi2    = math.radians(lat2)
    dphi    = phi2 - phi1
    dlon1   = 0.0                        # 남북 구간: 경도 차이 없음
    dlambda = math.radians(lon2 - lon1)  # 동서 구간: 경도 차이

    R = 6371.0
    # 남북 구간: (lat1,lon1) → (lat2,lon1)
    a_ns = math.sin(dphi / 2.0) ** 2
    ns   = R * 2.0 * math.atan2(math.sqrt(a_ns), math.sqrt(1.0 - a_ns))

    # 동서 구간: (lat2,lon1) → (lat2,lon2)
    a_ew = math.cos(phi2) ** 2 * math.sin(dlambda / 2.0) ** 2
    ew   = R * 2.0 * math.atan2(math.sqrt(a_ew), math.sqrt(1.0 - a_ew))

    return ns + ew


# ── 동적 속도 계산 ───────────────────────────────
def _in_congestion_zone(lat: float, lon: float) -> bool:
    """좌표가 혼잡 구역 안에 있는지 확인"""
    return any(
        lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
        for lat_min, lat_max, lon_min, lon_max in CONGESTION_ZONES
    )


def get_dynamic_speed(lat: float, lon: float,
                      base_speed: float,
                      penalty_pct: float,
                      weather_factor: float = 1.0) -> float:
    """위치·기상·혼잡도를 반영한 실효 속도 (km/h)"""
    weather_factor = max(0.1, weather_factor)               # 음수/0 방어
    penalty_pct    = max(0.0, min(100.0, penalty_pct))      # 0~100 클리핑

    spd = base_speed
    if _in_congestion_zone(lat, lon):
        spd = max(10.0, base_speed * (1.0 - penalty_pct / 100.0))

    return max(5.0, spd * weather_factor)


# ── 동적 연료 소비 계산 ──────────────────────────
def _get_vehicle_spec(vehicle_type: str) -> tuple[float, float]:
    """차량 타입 문자열로 스펙 반환. 매칭 실패 시 가장 큰 차종(5톤) 기본값"""
    for key, spec in VEHICLE_SPECS.items():
        if key in vehicle_type:
            return spec
    return VEHICLE_SPECS["5톤"]


def get_dynamic_fuel_consumption(vehicle_type: str,
                                 payload_kg: float,
                                 dist_km: float,
                                 is_deadhead: bool = False) -> float:
    """실측 연료 소비량 (L)
    - payload_kg, dist_km 음수 입력 방어
    - 차량 스펙은 VEHICLE_SPECS 테이블에서 조회
    """
    payload_kg = max(0.0, payload_kg)
    dist_km    = max(0.0, dist_km)

    base_kmpl, drop_coeff = _get_vehicle_spec(vehicle_type)
    drop = (payload_kg / 1000.0) * drop_coeff

    if is_deadhead:
        kmpl = base_kmpl * _VEHICLE_DEADHEAD_BONUS
    else:
        kmpl = base_kmpl * (1.0 - min(_MAX_FUEL_DROP_RATIO, drop))

    return dist_km / kmpl if kmpl > 0.0 else 0.0


# ── Thread-safe LRU 캐시 ─────────────────────────
class LRUCache:
    """Thread-safe LRU 캐시

    개선:
    - __contains__: 존재 확인 시 LRU 순서 갱신 (get과 동일한 효과)
    - clear(): 캐시 전체 초기화
    - keys(): 현재 캐시된 키 목록 반환 (최신 → 오래된 순)
    """

    def __init__(self, maxsize: int = 500):
        if maxsize < 1:
            raise ValueError(f"maxsize는 1 이상이어야 합니다. (받은 값: {maxsize})")
        self._cache:   OrderedDict = OrderedDict()
        self._maxsize: int         = maxsize
        self._lock:    threading.Lock = threading.Lock()

    def get(self, key: str, default=None):
        with self._lock:
            if key not in self._cache:
                return default
            self._cache.move_to_end(key)
            return self._cache[key]

    def set(self, key: str, value) -> None:
        with self._lock:
            if key in self._cache:
                # 기존 키 업데이트: 크기 변화 없으므로 eviction 불필요
                self._cache[key] = value
                self._cache.move_to_end(key)
            else:
                self._cache[key] = value
                if len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)

    def clear(self) -> None:
        """캐시 전체 초기화"""
        with self._lock:
            self._cache.clear()

    def keys(self) -> list[str]:
        """현재 캐시된 키 목록 (최신 → 오래된 순)"""
        with self._lock:
            return list(reversed(self._cache.keys()))

    def __contains__(self, key: str) -> bool:
        # LRU 순서도 갱신 — 확인만 해도 최근 사용으로 처리
        with self._lock:
            if key not in self._cache:
                return False
            self._cache.move_to_end(key)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __getitem__(self, key: str):
        return self.get(key)

    def __setitem__(self, key: str, value) -> None:
        self.set(key, value)

    def __repr__(self) -> str:
        return f"LRUCache(size={len(self)}/{self._maxsize})"
