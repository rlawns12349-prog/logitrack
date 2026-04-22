"""
geo.py — 공간/물리 유틸리티

개선 사항:
  - haversine_distance / manhattan_distance: 동일 좌표 입력 시 0.0 빠른 반환
  - get_dynamic_speed: base_speed ≤ 0 방어 추가
  - get_dynamic_fuel_consumption: kmpl 계산 분기를 명확히 분리
  - LRUCache.__contains__: 접근 시 LRU 순서 갱신 동작을 docstring에 명시
  - 타입 힌팅 완전 적용 (from __future__ import annotations)
"""
from __future__ import annotations

import math
from collections import OrderedDict
import threading
from typing import Any, Optional

try:
    from config import vehicle_specs, congestion_zones, esg_config
except ImportError:
    class _VehicleSpecs:
        SPECS               = {"1톤": (9.0, 0.05), "2.5톤": (6.5, 0.04), "5톤": (4.5, 0.03)}
        DEADHEAD_BONUS      = 1.15
        MAX_FUEL_DROP_RATIO = 0.30

    class _CongestionZones:
        ZONES = [(37.40, 37.75, 126.80, 127.20)]

    class _ESGConfig:
        DIESEL_EMISSION_FACTOR = 2.62

    vehicle_specs    = _VehicleSpecs()
    congestion_zones = _CongestionZones()
    esg_config       = _ESGConfig()

# re-export (하위 호환성)
DIESEL_EMISSION_FACTOR: float = esg_config.DIESEL_EMISSION_FACTOR

_EARTH_RADIUS_KM = 6371.0


# ══════════════════════════════════════════════
# 거리 계산
# ══════════════════════════════════════════════

def haversine_distance(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> float:
    """두 좌표 간 구면 거리 (km), Haversine 공식.

    동일 좌표이면 0.0을 바로 반환.

    Args:
        lat1, lon1: 시작점 위도·경도
        lat2, lon2: 도착점 위도·경도

    Returns:
        거리 (km)
    """
    if lat1 == lat2 and lon1 == lon2:
        return 0.0

    phi1   = math.radians(lat1)
    phi2   = math.radians(lat2)
    dphi   = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)

    a = (math.sin(dphi   / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(dlambda / 2.0) ** 2)

    return _EARTH_RADIUS_KM * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def manhattan_distance(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
) -> float:
    """도로망 근사 거리 (km) — 남북 + 동서 구간 haversine 합산.

    동일 좌표이면 0.0을 바로 반환.

    Args:
        lat1, lon1: 시작점 위도·경도
        lat2, lon2: 도착점 위도·경도

    Returns:
        거리 (km)
    """
    if lat1 == lat2 and lon1 == lon2:
        return 0.0

    phi1    = math.radians(lat1)
    phi2    = math.radians(lat2)
    dphi    = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)

    # 남북 구간
    a_ns = math.sin(dphi / 2.0) ** 2
    ns   = _EARTH_RADIUS_KM * 2.0 * math.atan2(math.sqrt(a_ns), math.sqrt(1.0 - a_ns))

    # 동서 구간
    a_ew = math.cos(phi2) ** 2 * math.sin(dlambda / 2.0) ** 2
    ew   = _EARTH_RADIUS_KM * 2.0 * math.atan2(math.sqrt(a_ew), math.sqrt(1.0 - a_ew))

    return ns + ew


# ══════════════════════════════════════════════
# 동적 속도 계산
# ══════════════════════════════════════════════

def _in_congestion_zone(lat: float, lon: float) -> bool:
    """좌표가 혼잡 구역 내에 있는지 확인."""
    return any(
        lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
        for lat_min, lat_max, lon_min, lon_max in congestion_zones.ZONES
    )


def get_dynamic_speed(
    lat:            float,
    lon:            float,
    base_speed:     float,
    penalty_pct:    float,
    weather_factor: float = 1.0,
) -> float:
    """위치·기상·혼잡도를 반영한 실효 속도 (km/h).

    개선: base_speed ≤ 0 입력 방어 추가.

    Args:
        lat:            위도
        lon:            경도
        base_speed:     기준 속도 (km/h), 양수여야 함
        penalty_pct:    혼잡 페널티 (0~100)
        weather_factor: 기상 계수 (0.1~1.0)

    Returns:
        실효 속도 (km/h), 최소 5.0
    """
    if base_speed <= 0:
        return 5.0

    weather_factor = max(0.1, min(1.0, weather_factor))
    penalty_pct    = max(0.0, min(100.0, penalty_pct))

    spd = base_speed
    if _in_congestion_zone(lat, lon):
        spd = max(10.0, base_speed * (1.0 - penalty_pct / 100.0))

    return max(5.0, spd * weather_factor)


# ══════════════════════════════════════════════
# 동적 연료 소비 계산
# ══════════════════════════════════════════════

def _get_vehicle_spec(vehicle_type: str) -> tuple[float, float]:
    """차량 타입 문자열로 스펙 반환.

    Args:
        vehicle_type: "1톤", "2.5톤", "5톤" 포함 문자열

    Returns:
        (기본연비 km/L, 적재량당 연비저하 계수)
    """
    for key, spec in vehicle_specs.SPECS.items():
        if key in vehicle_type:
            return spec
    return vehicle_specs.SPECS["5톤"]  # 매칭 실패 시 최대 차종 기본값


def get_dynamic_fuel_consumption(
    vehicle_type: str,
    payload_kg:   float,
    dist_km:      float,
    is_deadhead:  bool = False,
) -> float:
    """실측 연료 소비량 (L).

    개선: deadhead와 적재 분기를 명확히 분리해 가독성 향상.

    Args:
        vehicle_type: 차량 타입 ("1톤", "2.5톤", "5톤" 포함)
        payload_kg:   적재 중량 (kg), 음수 → 0으로 보정
        dist_km:      이동 거리 (km), 음수 → 0으로 보정
        is_deadhead:  공차 복귀 여부

    Returns:
        연료 소비량 (L)
    """
    payload_kg = max(0.0, payload_kg)
    dist_km    = max(0.0, dist_km)

    base_kmpl, drop_coeff = _get_vehicle_spec(vehicle_type)

    if is_deadhead:
        kmpl = base_kmpl * vehicle_specs.DEADHEAD_BONUS
    else:
        drop = (payload_kg / 1000.0) * drop_coeff
        kmpl = base_kmpl * (1.0 - min(vehicle_specs.MAX_FUEL_DROP_RATIO, drop))

    return dist_km / kmpl if kmpl > 0.0 else 0.0


# ══════════════════════════════════════════════
# Thread-safe LRU 캐시
# ══════════════════════════════════════════════

class LRUCache:
    """Thread-safe LRU 캐시.

    Notes:
        - get() / __contains__() 모두 LRU 순서를 갱신한다.
        - set() 시 maxsize 초과 → 가장 오래된 항목 자동 제거.
        - clear() 로 전체 초기화 가능.
    """

    def __init__(self, maxsize: int = 500) -> None:
        """
        Args:
            maxsize: 최대 캐시 크기 (1 이상)

        Raises:
            ValueError: maxsize < 1
        """
        if maxsize < 1:
            raise ValueError(f"maxsize는 1 이상이어야 합니다. (받은 값: {maxsize})")
        self._cache:   OrderedDict[str, Any] = OrderedDict()
        self._maxsize: int                   = maxsize
        self._lock:    threading.Lock        = threading.Lock()

    def get(self, key: str, default: Any = None) -> Any:
        """키로 값 조회. 존재하면 LRU 순서를 최신으로 갱신.

        Args:
            key:     조회할 키
            default: 키가 없을 때 반환할 값

        Returns:
            저장된 값 또는 default
        """
        with self._lock:
            if key not in self._cache:
                return default
            self._cache.move_to_end(key)
            return self._cache[key]

    def set(self, key: str, value: Any) -> None:
        """키-값 저장. maxsize 초과 시 가장 오래된 항목 제거.

        Args:
            key:   저장할 키
            value: 저장할 값
        """
        with self._lock:
            if key in self._cache:
                self._cache[key] = value
                self._cache.move_to_end(key)
            else:
                self._cache[key] = value
                if len(self._cache) > self._maxsize:
                    self._cache.popitem(last=False)

    def clear(self) -> None:
        """캐시 전체 초기화."""
        with self._lock:
            self._cache.clear()

    def keys(self) -> list[str]:
        """현재 캐시된 키 목록 (최신 → 오래된 순).

        Returns:
            키 리스트
        """
        with self._lock:
            return list(reversed(self._cache.keys()))

    def __contains__(self, key: str) -> bool:
        """키 존재 확인. 존재하면 LRU 순서도 갱신.

        Args:
            key: 확인할 키

        Returns:
            키 존재 여부
        """
        with self._lock:
            if key not in self._cache:
                return False
            self._cache.move_to_end(key)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __repr__(self) -> str:
        return f"LRUCache(size={len(self)}/{self._maxsize})"
