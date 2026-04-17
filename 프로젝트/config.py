"""
config.py — LogiTrack 중앙 설정 관리
환경 변수와 상수를 한 곳에서 관리하여 유지보수성 향상
"""
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class DatabaseConfig:
    """데이터베이스 설정"""
    POOL_MIN: int = int(os.getenv("DB_POOL_MIN", "1"))
    POOL_MAX: int = int(os.getenv("DB_POOL_MAX", "10"))
    CONNECT_TIMEOUT: int = int(os.getenv("DB_CONNECT_TIMEOUT", "5"))
    CACHE_TTL_HOURS: int = int(os.getenv("CACHE_TTL_HOURS", "12"))


@dataclass
class APIConfig:
    """API 관련 설정"""
    TIMEOUT_SEC: int = int(os.getenv("API_TIMEOUT_SEC", "5"))
    RETRY_ATTEMPTS: int = int(os.getenv("API_RETRY_ATTEMPTS", "3"))
    RATE_LIMIT_BATCH_SIZE: int = int(os.getenv("API_BATCH_SIZE", "15"))
    RATE_LIMIT_DELAY_SEC: float = float(os.getenv("API_DELAY_SEC", "0.3"))


@dataclass
class CacheConfig:
    """캐시 설정"""
    LRU_MAX_SIZE: int = int(os.getenv("CACHE_MAX_SIZE", "500"))


@dataclass
class VehicleSpecs:
    """차량 스펙 정의 - geo.py에서 사용"""
    SPECS: dict = None
    DEADHEAD_BONUS: float = 1.15
    MAX_FUEL_DROP_RATIO: float = 0.30
    
    def __post_init__(self):
        if self.SPECS is None:
            self.SPECS = {
                "1톤":   (9.0, 0.05),   # (기본연비 km/L, 적재량당 연비저하 계수)
                "2.5톤": (6.5, 0.04),
                "5톤":   (4.5, 0.03),
            }


@dataclass
class CongestionZones:
    """혼잡 구역 정의 - geo.py에서 사용"""
    ZONES: list = None
    
    def __post_init__(self):
        if self.ZONES is None:
            # (lat_min, lat_max, lon_min, lon_max)
            self.ZONES = [
                (37.40, 37.75, 126.80, 127.20),  # 서울 수도권
                # 추가 구역은 여기에 append
            ]


@dataclass
class SolverConfig:
    """OR-Tools 솔버 설정"""
    DEFAULT_TIME_LIMIT_SEC: int = int(os.getenv("SOLVER_TIME_LIMIT", "5"))
    VIP_PENALTY: int = 50_000_000
    NORMAL_PENALTY: int = 5_000_000
    LEISURE_PENALTY: int = 100_000
    
    # 차량별 시간 배율
    VEHICLE_TIME_MULTIPLIERS: dict = None
    
    def __post_init__(self):
        if self.VEHICLE_TIME_MULTIPLIERS is None:
            self.VEHICLE_TIME_MULTIPLIERS = {
                "1톤": 1.0,
                "2.5톤": 1.15,
                "5톤": 1.3,
            }


@dataclass
class ESGConfig:
    """ESG 관련 설정"""
    DIESEL_EMISSION_FACTOR: float = 2.62  # kg CO2/L (GLEC Framework)
    TREES_PER_KG_CO2: float = 6.6  # CO2 1kg 흡수에 필요한 소나무 그루 수


@dataclass
class LogConfig:
    """로깅 설정"""
    LEVEL: str = os.getenv("LOG_LEVEL", "WARNING")
    FORMAT: str = "%(asctime)s %(levelname)s %(message)s"
    MASK_API_KEYS: bool = os.getenv("MASK_API_KEYS", "true").lower() == "true"


@dataclass
class DefaultValues:
    """세션 기본값"""
    VEHICLE_COUNTS: dict = None
    SIMULATION_PARAMS: dict = None
    COST_PARAMS: dict = None
    
    def __post_init__(self):
        if self.VEHICLE_COUNTS is None:
            self.VEHICLE_COUNTS = {
                '1t': 2,
                '2t': 1,
                '5t': 0,
            }
        
        if self.SIMULATION_PARAMS is None:
            self.SIMULATION_PARAMS = {
                'speed': 45,              # km/h
                'service_min': 10,        # 분
                'service_sec_per_kg': 2,  # 초
                'congestion_penalty': 40, # %
                'start_time': "09:00",
                'weather': "맑음",
                'max_work_hours': 10,
                'balance_workload': False,
                'vrptw_time_limit': 5,    # 초
            }
        
        if self.COST_PARAMS is None:
            self.COST_PARAMS = {
                'fuel_price': 1500,        # 원/L
                'labor_per_hour': 15000,   # 원/시간
            }


# 싱글톤 인스턴스 생성
db_config = DatabaseConfig()
api_config = APIConfig()
cache_config = CacheConfig()
vehicle_specs = VehicleSpecs()
congestion_zones = CongestionZones()
solver_config = SolverConfig()
esg_config = ESGConfig()
log_config = LogConfig()
defaults = DefaultValues()


def get_config_summary() -> dict:
    """현재 설정 요약 반환 (디버깅용)"""
    return {
        "database": {
            "pool_size": f"{db_config.POOL_MIN}-{db_config.POOL_MAX}",
            "timeout": f"{db_config.CONNECT_TIMEOUT}s",
            "cache_ttl": f"{db_config.CACHE_TTL_HOURS}h",
        },
        "api": {
            "timeout": f"{api_config.TIMEOUT_SEC}s",
            "retry": api_config.RETRY_ATTEMPTS,
            "batch_size": api_config.RATE_LIMIT_BATCH_SIZE,
        },
        "solver": {
            "time_limit": f"{solver_config.DEFAULT_TIME_LIMIT_SEC}s",
        },
        "logging": {
            "level": log_config.LEVEL,
            "mask_keys": log_config.MASK_API_KEYS,
        }
    }
