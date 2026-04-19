"""
geo.py — 공간/물리 유틸리티 (개선 버전)
  - 거리 계산 (haversine, manhattan)
  - 동적 속도·연료 계산
  - Thread-safe LRU 캐시

개선 사항:
  - config.py에서 상수 로드
  - 타입 힌팅 완전 적용
  - 에러 처리 강화
  - 문서화 개선
"""
import math
from collections import OrderedDict
import threading
from typing import Optional, Tuple, List

try:
    from config import vehicle_specs, congestion_zones, esg_config
except ImportError:
    # 폴백: config.py 없을 시 기본값 사용
    class _VehicleSpecs:
        SPECS = {"1톤": (9.0, 0.05), "2.5톤": (6.5, 0.04), "5톤": (4.5, 0.03)}
        DEADHEAD_BONUS = 1.15
        MAX_FUEL_DROP_RATIO = 0.30
    
    class _CongestionZones:
        ZONES = [(37.40, 37.75, 126.80, 127.20)]
    
    class _ESGConfig:
        DIESEL_EMISSION_FACTOR = 2.62
    
    vehicle_specs = _VehicleSpecs()
    congestion_zones = _CongestionZones()
    esg_config = _ESGConfig()


# ── 상수 (재export - 하위 호환성) ─────────────────
DIESEL_EMISSION_FACTOR = esg_config.DIESEL_EMISSION_FACTOR


# ── 거리 계산 ────────────────────────────────────
def haversine_distance(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float
) -> float:
    """두 좌표 간 구면 거리 (km), Haversine 공식
    
    Args:
        lat1: 시작점 위도
        lon1: 시작점 경도
        lat2: 도착점 위도
        lon2: 도착점 경도
    
    Returns:
        거리 (km)
    
    Examples:
        >>> haversine_distance(37.5665, 126.9780, 37.5511, 126.9882)
        2.0  # 약 2km
    """
    R = 6371.0  # 지구 반지름 (km)
    
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    
    a = (math.sin(dphi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2)
    
    return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def manhattan_distance(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float
) -> float:
    """도로망 근사 거리 (km) — 남북 + 동서 구간 haversine 합산
    
    실제 도로망을 따라가는 경로를 Manhattan 거리로 근사.
    라디안 변환을 한 번만 수행해 중복 연산 제거.
    
    Args:
        lat1: 시작점 위도
        lon1: 시작점 경도
        lat2: 도착점 위도
        lon2: 도착점 경도
    
    Returns:
        거리 (km)
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    
    R = 6371.0
    
    # 남북 구간: (lat1,lon1) → (lat2,lon1)
    a_ns = math.sin(dphi / 2.0) ** 2
    ns = R * 2.0 * math.atan2(math.sqrt(a_ns), math.sqrt(1.0 - a_ns))
    
    # 동서 구간: (lat2,lon1) → (lat2,lon2)
    a_ew = math.cos(phi2) ** 2 * math.sin(dlambda / 2.0) ** 2
    ew = R * 2.0 * math.atan2(math.sqrt(a_ew), math.sqrt(1.0 - a_ew))
    
    return ns + ew


# ── 동적 속도 계산 ───────────────────────────────
def _in_congestion_zone(lat: float, lon: float) -> bool:
    """좌표가 혼잡 구역 안에 있는지 확인
    
    Args:
        lat: 위도
        lon: 경도
    
    Returns:
        혼잡 구역 내 위치 여부
    """
    return any(
        lat_min <= lat <= lat_max and lon_min <= lon <= lon_max
        for lat_min, lat_max, lon_min, lon_max in congestion_zones.ZONES
    )


def get_dynamic_speed(
    lat: float,
    lon: float,
    base_speed: float,
    penalty_pct: float,
    weather_factor: float = 1.0
) -> float:
    """위치·기상·혼잡도를 반영한 실효 속도 (km/h)
    
    Args:
        lat: 위도
        lon: 경도
        base_speed: 기준 속도 (km/h)
        penalty_pct: 혼잡 페널티 (0~100)
        weather_factor: 기상 계수 (0.7~1.0)
    
    Returns:
        실효 속도 (km/h), 최소 5km/h
    
    Examples:
        >>> get_dynamic_speed(37.5, 127.0, 50, 40, 0.8)
        24.0  # 혼잡+기상 감속
    """
    # 입력값 검증
    weather_factor = max(0.1, min(1.0, weather_factor))
    penalty_pct = max(0.0, min(100.0, penalty_pct))
    
    spd = base_speed
    if _in_congestion_zone(lat, lon):
        spd = max(10.0, base_speed * (1.0 - penalty_pct / 100.0))
    
    return max(5.0, spd * weather_factor)


# ── 동적 연료 소비 계산 ──────────────────────────
def _get_vehicle_spec(vehicle_type: str) -> Tuple[float, float]:
    """차량 타입 문자열로 스펙 반환
    
    Args:
        vehicle_type: "1톤", "2.5톤", "5톤" 포함 문자열
    
    Returns:
        (기본연비 km/L, 적재량당 연비저하 계수)
    
    Examples:
        >>> _get_vehicle_spec("1톤(냉탑)")
        (9.0, 0.05)
    """
    for key, spec in vehicle_specs.SPECS.items():
        if key in vehicle_type:
            return spec
    # 매칭 실패 시 가장 큰 차종(5톤) 기본값
    return vehicle_specs.SPECS["5톤"]


def get_dynamic_fuel_consumption(
    vehicle_type: str,
    payload_kg: float,
    dist_km: float,
    is_deadhead: bool = False
) -> float:
    """실측 연료 소비량 (L)
    
    Args:
        vehicle_type: 차량 타입 ("1톤", "2.5톤", "5톤" 포함)
        payload_kg: 적재 중량 (kg)
        dist_km: 이동 거리 (km)
        is_deadhead: 공차 복귀 여부
    
    Returns:
        연료 소비량 (L)
    
    Examples:
        >>> get_dynamic_fuel_consumption("1톤(냉탑)", 500, 100)
        12.2  # 약 12L
    """
    # 음수 입력 방어
    payload_kg = max(0.0, payload_kg)
    dist_km = max(0.0, dist_km)
    
    base_kmpl, drop_coeff = _get_vehicle_spec(vehicle_type)
    drop = (payload_kg / 1000.0) * drop_coeff
    
    if is_deadhead:
        # 공차 복귀 시 연비 향상
        kmpl = base_kmpl * vehicle_specs.DEADHEAD_BONUS
    else:
        # 적재 시 연비 저하 (최대 30%까지)
        kmpl = base_kmpl * (1.0 - min(vehicle_specs.MAX_FUEL_DROP_RATIO, drop))
    
    return dist_km / kmpl if kmpl > 0.0 else 0.0


# ── Thread-safe LRU 캐시 ─────────────────────────
class LRUCache:
    """Thread-safe LRU 캐시
    
    특징:
    - __contains__: 존재 확인 시 LRU 순서 갱신
    - clear(): 캐시 전체 초기화
    - keys(): 현재 캐시된 키 목록 반환
    
    Examples:
        >>> cache = LRUCache(maxsize=100)
        >>> cache.set("key1", {"value": 123})
        >>> cache.get("key1")
        {'value': 123}
        >>> "key1" in cache
        True
    """
    
    def __init__(self, maxsize: int = 500):
        """
        Args:
            maxsize: 최대 캐시 크기
        
        Raises:
            ValueError: maxsize < 1
        """
        if maxsize < 1:
            raise ValueError(f"maxsize는 1 이상이어야 합니다. (받은 값: {maxsize})")
        
        self._cache: OrderedDict = OrderedDict()
        self._maxsize: int = maxsize
        self._lock: threading.Lock = threading.Lock()
    
    def get(self, key: str, default=None):
        """키로 값 조회. LRU 순서 갱신.
        
        Args:
            key: 조회할 키
            default: 키가 없을 때 반환할 기본값
        
        Returns:
            저장된 값 또는 default
        """
        with self._lock:
            if key not in self._cache:
                return default
            self._cache.move_to_end(key)
            return self._cache[key]
    
    def set(self, key: str, value) -> None:
        """키-값 저장. 용량 초과 시 가장 오래된 항목 제거.
        
        Args:
            key: 저장할 키
            value: 저장할 값
        """
        with self._lock:
            if key in self._cache:
                # 기존 키 업데이트
                self._cache[key] = value
                self._cache.move_to_end(key)
            else:
                # 새 키 추가
                self._cache[key] = value
                if len(self._cache) > self._maxsize:
                    # FIFO 방식으로 가장 오래된 항목 제거
                    self._cache.popitem(last=False)
    
    def clear(self) -> None:
        """캐시 전체 초기화"""
        with self._lock:
            self._cache.clear()
    
    def keys(self) -> List[str]:
        """현재 캐시된 키 목록 (최신 → 오래된 순)
        
        Returns:
            키 리스트
        """
        with self._lock:
            return list(reversed(self._cache.keys()))
    
    def __contains__(self, key: str) -> bool:
        """키 존재 확인. LRU 순서도 갱신.
        
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
        """캐시 크기 반환"""
        with self._lock:
            return len(self._cache)
    
    def __getitem__(self, key: str):
        """딕셔너리 스타일 조회"""
        return self.get(key)
    
    def __setitem__(self, key: str, value) -> None:
        """딕셔너리 스타일 저장"""
        self.set(key, value)
    
    def __repr__(self) -> str:
        return f"LRUCache(size={len(self)}/{self._maxsize})"
