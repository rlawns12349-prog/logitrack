"""
config.py — LogiTrack 중앙 설정 관리

개선 사항:
  - 환경변수 범위 검증 추가 (음수 방지)
  - 각 dataclass에 __post_init__ 검증 로직 강화
  - get_config_summary 반환 타입 명시
  - 모든 공개 인스턴스에 docstring
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DatabaseConfig:
    """데이터베이스 커넥션 풀 및 캐시 설정."""

    POOL_MIN:         int = field(default_factory=lambda: int(os.getenv("DB_POOL_MIN",        "1")))
    POOL_MAX:         int = field(default_factory=lambda: int(os.getenv("DB_POOL_MAX",       "10")))
    CONNECT_TIMEOUT:  int = field(default_factory=lambda: int(os.getenv("DB_CONNECT_TIMEOUT", "5")))
    CACHE_TTL_HOURS:  int = field(default_factory=lambda: int(os.getenv("CACHE_TTL_HOURS",   "12")))

    def __post_init__(self) -> None:
        if self.POOL_MIN < 1:
            raise ValueError(f"DB_POOL_MIN은 1 이상이어야 합니다. (받은 값: {self.POOL_MIN})")
        if self.POOL_MAX < self.POOL_MIN:
            raise ValueError(
                f"DB_POOL_MAX({self.POOL_MAX})는 DB_POOL_MIN({self.POOL_MIN}) 이상이어야 합니다."
            )
        if self.CONNECT_TIMEOUT < 1:
            raise ValueError(f"DB_CONNECT_TIMEOUT은 1 이상이어야 합니다. (받은 값: {self.CONNECT_TIMEOUT})")
        if self.CACHE_TTL_HOURS < 1:
            raise ValueError(f"CACHE_TTL_HOURS는 1 이상이어야 합니다. (받은 값: {self.CACHE_TTL_HOURS})")


@dataclass
class APIConfig:
    """외부 API 요청 관련 설정."""

    TIMEOUT_SEC:          int   = field(default_factory=lambda: int(os.getenv("API_TIMEOUT_SEC",   "5")))
    RETRY_ATTEMPTS:       int   = field(default_factory=lambda: int(os.getenv("API_RETRY_ATTEMPTS","3")))
    RATE_LIMIT_BATCH_SIZE:int   = field(default_factory=lambda: int(os.getenv("API_BATCH_SIZE",   "15")))
    RATE_LIMIT_DELAY_SEC: float = field(default_factory=lambda: float(os.getenv("API_DELAY_SEC", "0.3")))

    def __post_init__(self) -> None:
        if self.TIMEOUT_SEC < 1:
            raise ValueError(f"API_TIMEOUT_SEC는 1 이상이어야 합니다. (받은 값: {self.TIMEOUT_SEC})")
        if self.RETRY_ATTEMPTS < 0:
            raise ValueError(f"API_RETRY_ATTEMPTS는 0 이상이어야 합니다.")
        if self.RATE_LIMIT_BATCH_SIZE < 1:
            raise ValueError(f"API_BATCH_SIZE는 1 이상이어야 합니다.")
        if self.RATE_LIMIT_DELAY_SEC < 0:
            raise ValueError(f"API_DELAY_SEC는 0 이상이어야 합니다.")


@dataclass
class CacheConfig:
    """메모리 LRU 캐시 설정."""

    LRU_MAX_SIZE: int = field(default_factory=lambda: int(os.getenv("CACHE_MAX_SIZE", "500")))

    def __post_init__(self) -> None:
        if self.LRU_MAX_SIZE < 1:
            raise ValueError(f"CACHE_MAX_SIZE는 1 이상이어야 합니다. (받은 값: {self.LRU_MAX_SIZE})")


@dataclass
class VehicleSpecs:
    """차량 스펙 정의.

    SPECS: {차량타입: (기본연비 km/L, 적재량당 연비저하 계수)}
    DEADHEAD_BONUS: 공차 복귀 시 연비 향상 배율
    MAX_FUEL_DROP_RATIO: 최대 연비 저하율 (0~1)
    """

    SPECS:              dict  = field(default=None)
    DEADHEAD_BONUS:     float = 1.15
    MAX_FUEL_DROP_RATIO:float = 0.30

    def __post_init__(self) -> None:
        if self.SPECS is None:
            self.SPECS = {
                "1톤":   (9.0, 0.05),
                "2.5톤": (6.5, 0.04),
                "5톤":   (4.5, 0.03),
            }
        if not (0 < self.DEADHEAD_BONUS <= 2.0):
            raise ValueError(f"DEADHEAD_BONUS는 (0, 2.0] 범위여야 합니다. (받은 값: {self.DEADHEAD_BONUS})")
        if not (0 < self.MAX_FUEL_DROP_RATIO < 1):
            raise ValueError(f"MAX_FUEL_DROP_RATIO는 (0, 1) 범위여야 합니다. (받은 값: {self.MAX_FUEL_DROP_RATIO})")


@dataclass
class CongestionZones:
    """혼잡 구역 정의.

    ZONES: [(lat_min, lat_max, lon_min, lon_max), ...]
    """

    ZONES: list = field(default=None)

    def __post_init__(self) -> None:
        if self.ZONES is None:
            self.ZONES = [
                (37.40, 37.75, 126.80, 127.20),  # 서울 수도권
            ]
        for zone in self.ZONES:
            if len(zone) != 4:
                raise ValueError(f"혼잡 구역은 (lat_min, lat_max, lon_min, lon_max) 4값이어야 합니다: {zone}")
            lat_min, lat_max, lon_min, lon_max = zone
            if lat_min >= lat_max or lon_min >= lon_max:
                raise ValueError(f"혼잡 구역 좌표 범위가 잘못됐습니다: {zone}")


@dataclass
class SolverConfig:
    """OR-Tools VRPTW 솔버 설정."""

    DEFAULT_TIME_LIMIT_SEC: int = field(default_factory=lambda: int(os.getenv("SOLVER_TIME_LIMIT", "5")))
    VIP_PENALTY:            int = 50_000_000
    NORMAL_PENALTY:         int = 5_000_000
    LEISURE_PENALTY:        int = 100_000

    # 차량별 시간 배율 (대형 차량일수록 이동시간 불리)
    VEHICLE_TIME_MULTIPLIERS: dict = field(default=None)

    def __post_init__(self) -> None:
        if self.DEFAULT_TIME_LIMIT_SEC < 1:
            raise ValueError(f"SOLVER_TIME_LIMIT은 1 이상이어야 합니다. (받은 값: {self.DEFAULT_TIME_LIMIT_SEC})")
        if self.VEHICLE_TIME_MULTIPLIERS is None:
            self.VEHICLE_TIME_MULTIPLIERS = {
                "1톤":   1.0,
                "2.5톤": 1.15,
                "5톤":   1.3,
            }


@dataclass
class ESGConfig:
    """ESG 탄소 배출 계산 상수.

    DIESEL_EMISSION_FACTOR: kg CO₂/L (GLEC Framework 기준)
    TREES_PER_KG_CO2: CO₂ 1kg 흡수에 필요한 소나무 그루 수
    """

    DIESEL_EMISSION_FACTOR: float = 2.62
    TREES_PER_KG_CO2:       float = 6.6

    def __post_init__(self) -> None:
        if self.DIESEL_EMISSION_FACTOR <= 0:
            raise ValueError("DIESEL_EMISSION_FACTOR는 양수여야 합니다.")
        if self.TREES_PER_KG_CO2 <= 0:
            raise ValueError("TREES_PER_KG_CO2는 양수여야 합니다.")


@dataclass
class LogConfig:
    """로깅 설정.

    LEVEL: 로그 레벨 (DEBUG / INFO / WARNING / ERROR / CRITICAL)
    FORMAT: 로그 포맷 문자열
    MASK_API_KEYS: API 키 로그 마스킹 여부
    """

    LEVEL:        str  = field(default_factory=lambda: os.getenv("LOG_LEVEL", "WARNING"))
    FORMAT:       str  = "%(asctime)s %(levelname)s %(message)s"
    MASK_API_KEYS:bool = field(
        default_factory=lambda: os.getenv("MASK_API_KEYS", "true").lower() == "true"
    )

    _VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    def __post_init__(self) -> None:
        if self.LEVEL.upper() not in self._VALID_LEVELS:
            raise ValueError(
                f"LOG_LEVEL은 {self._VALID_LEVELS} 중 하나여야 합니다. (받은 값: {self.LEVEL})"
            )
        self.LEVEL = self.LEVEL.upper()


@dataclass
class DefaultValues:
    """세션 기본값.

    VEHICLE_COUNTS:    차량 초기 대수
    SIMULATION_PARAMS: 운행 시뮬레이션 파라미터
    COST_PARAMS:       비용 파라미터
    """

    VEHICLE_COUNTS:    dict = field(default=None)
    SIMULATION_PARAMS: dict = field(default=None)
    COST_PARAMS:       dict = field(default=None)

    def __post_init__(self) -> None:
        if self.VEHICLE_COUNTS is None:
            self.VEHICLE_COUNTS = {
                "1t": 2,
                "2t": 1,
                "5t": 0,
            }
        if self.SIMULATION_PARAMS is None:
            self.SIMULATION_PARAMS = {
                "speed":              45,
                "service_min":        10,
                "service_sec_per_kg": 2,
                "congestion_penalty": 40,
                "start_time":         "09:00",
                "weather":            "맑음",
                "max_work_hours":     10,
                "balance_workload":   False,
                "vrptw_time_limit":   5,
            }
        if self.COST_PARAMS is None:
            self.COST_PARAMS = {
                "fuel_price":      1500,
                "labor_per_hour": 15000,
            }

        # 파라미터 범위 검증
        sp = self.SIMULATION_PARAMS
        if not (1 <= sp["speed"] <= 200):
            raise ValueError(f"speed는 1~200 범위여야 합니다. (받은 값: {sp['speed']})")
        if sp["service_min"] < 0:
            raise ValueError(f"service_min은 0 이상이어야 합니다.")
        if not (0 <= sp["congestion_penalty"] <= 100):
            raise ValueError(f"congestion_penalty는 0~100 범위여야 합니다.")
        if not (1 <= sp["max_work_hours"] <= 24):
            raise ValueError(f"max_work_hours는 1~24 범위여야 합니다.")


# ── 싱글톤 인스턴스 ───────────────────────────────
db_config       = DatabaseConfig()
api_config      = APIConfig()
cache_config    = CacheConfig()
vehicle_specs   = VehicleSpecs()
congestion_zones = CongestionZones()
solver_config   = SolverConfig()
esg_config      = ESGConfig()
log_config      = LogConfig()
defaults        = DefaultValues()


def get_config_summary() -> dict[str, dict]:
    """현재 설정 요약 반환 (디버깅·운영 모니터링용).

    Returns:
        설정 카테고리별 요약 dict
    """
    return {
        "database": {
            "pool_size": f"{db_config.POOL_MIN}-{db_config.POOL_MAX}",
            "timeout":   f"{db_config.CONNECT_TIMEOUT}s",
            "cache_ttl": f"{db_config.CACHE_TTL_HOURS}h",
        },
        "api": {
            "timeout":    f"{api_config.TIMEOUT_SEC}s",
            "retry":      api_config.RETRY_ATTEMPTS,
            "batch_size": api_config.RATE_LIMIT_BATCH_SIZE,
            "delay":      f"{api_config.RATE_LIMIT_DELAY_SEC}s",
        },
        "solver": {
            "time_limit": f"{solver_config.DEFAULT_TIME_LIMIT_SEC}s",
            "vip_penalty": solver_config.VIP_PENALTY,
        },
        "esg": {
            "emission_factor": f"{esg_config.DIESEL_EMISSION_FACTOR} kgCO2/L",
            "trees_per_kg":    esg_config.TREES_PER_KG_CO2,
        },
        "logging": {
            "level":     log_config.LEVEL,
            "mask_keys": log_config.MASK_API_KEYS,
        },
    }
