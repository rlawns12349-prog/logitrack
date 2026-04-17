"""
types.py — LogiTrack 타입 정의
코드 가독성과 타입 안전성 향상
"""
from typing import TypedDict, List, Dict, Optional, Literal


# ── 노드/지점 관련 타입 ────────────────────────────
class LocationDict(TypedDict, total=False):
    """지점 정보"""
    name: str
    lat: float
    lon: float
    addr: str


class NodeDict(TypedDict, total=False):
    """배송 노드 정보 (LocationDict 확장)"""
    name: str
    lat: float
    lon: float
    addr: str
    weight: float          # kg
    volume: float          # CBM
    temperature: Literal["상온", "냉장", "냉동"]
    unload_method: Literal["수작업", "지게차"]
    difficulty: str        # "일반 (+0분)", "보안아파트 (+10분)", etc.
    priority: Literal["VIP", "일반", "여유"]
    tw_type: Literal["Hard", "Soft"]
    tw_start: int          # 분 단위
    tw_end: int            # 분 단위
    tw_disp: str           # "09:00~18:00" 형식
    memo: str
    _node_uid: str         # 내부 고유 ID


# ── 차량 관련 타입 ────────────────────────────────
class VehicleInfo(TypedDict):
    """차량 정보"""
    name: str
    max_weight: float      # kg
    max_volume: float      # CBM
    cost: int              # 고정비 (원)
    skill: float           # 운전자 숙련도 (0.8~1.2)
    temperature_caps: List[str]  # ["상온", "냉장", "냉동"]


# ── 경로 관련 타입 ────────────────────────────────
class RouteSegment(TypedDict):
    """경로 세그먼트 정보"""
    from_name: str
    to_name: str
    dist: float            # km
    time: float            # 분
    raw_time: float        # 기상 보정 전 시간
    toll: int              # 통행료 (원)
    path: List[List[float]]  # [[lat, lon], ...]
    is_fallback: bool      # Manhattan 추정 여부


class RouteMatrix(TypedDict):
    """경로 매트릭스"""
    combined: List[List[int]]        # travel_time + service_time
    travel_time: List[List[int]]     # 이동 시간 행렬
    service_time: List[int]          # 서비스 시간 리스트
    distance: List[List[float]]      # 거리 행렬 (km)
    toll: List[List[int]]            # 통행료 행렬 (원)


# ── 솔버 결과 타입 ────────────────────────────────
class TruckStats(TypedDict):
    """차량별 통계"""
    dist: float            # 총 거리 (km)
    time: float            # 총 시간 (분)
    wait_time: float       # 대기 시간 (분)
    stops: int             # 정차 횟수
    toll_cost: int         # 통행료 (원)
    fuel_liter: float      # 연료 소비 (L)
    co2_kg: float          # CO2 배출 (kg)
    cost: int              # 고정비 (원)
    max_wt: float          # 최대 중량 (kg)
    max_vol: float         # 최대 부피 (CBM)
    used_wt: float         # 사용 중량 (kg)
    used_vol: float        # 사용 부피 (CBM)
    route_names: List[str] # 경로 지점 이름들
    loads_detail: List[Dict]  # LIFO 상차 상세


class ReportRow(TypedDict):
    """운행 지시서 행"""
    트럭: str
    거점: str
    도착: str
    약속시간: str
    거리: str
    잔여무게: str
    잔여부피: str
    메모: str


class OptimizationResult(TypedDict):
    """최적화 결과"""
    report: List[ReportRow]
    dist: float                    # 총 거리
    fuel_cost: float               # 연료비
    toll_cost: int                 # 통행료
    co2_total: float               # 총 CO2 배출량
    labor: float                   # 인건비
    fixed_cost: int                # 고정비 합계
    total_cost: float              # 총 비용
    truck_stats: Dict[str, TruckStats]
    paths: List[Dict]              # 지도용 경로 정보
    routes: List[List[NodeDict]]   # 차량별 노드 리스트
    efficiency: float              # 거리 효율성 (%)
    nn_real_dist: float            # NN 기준 거리
    hub_name: str
    hub_loc: LocationDict
    unassigned: List[NodeDict]     # 배차 불가 노드
    unassigned_diagnosed: List[Dict]  # 배차 불가 사유
    sla: float                     # SLA 정시율 (%)
    late_count: int                # 지연 건수
    wait_time_total: float         # 총 대기 시간


# ── 시나리오 관련 타입 ────────────────────────────
class ScenarioData(TypedDict):
    """시나리오 데이터"""
    s_name: str
    targets: List[NodeDict]
    result: Optional[OptimizationResult]
    start_node: str
    cfg: Dict
    created_at: str


# ── 설정 관련 타입 ────────────────────────────────
class SimulationConfig(TypedDict):
    """시뮬레이션 설정"""
    speed: int                     # 기준 시속 (km/h)
    service_min: int               # 기본 서비스 시간 (분)
    service_sec_per_kg: int        # kg당 추가 시간 (초)
    congestion_penalty: int        # 혼잡 페널티 (%)
    start_time: str                # 출발 시간 "HH:MM"
    weather: str                   # 기상 상태
    max_work_hours: int            # 최대 근로 시간
    balance_workload: bool         # 업무량 균등 배분
    vrptw_time_limit: int          # 솔버 제한 시간 (초)


# ── 캐시 관련 타입 ────────────────────────────────
CacheKey = str
CacheValue = Dict


# ── 웨더 타입 ────────────────────────────────────
WeatherType = Literal["맑음", "비 (감속 20%)", "눈 (감속 30%)"]
TemperatureType = Literal["상온", "냉장", "냉동"]
PriorityType = Literal["VIP", "일반", "여유"]
TimeWindowType = Literal["Hard", "Soft"]
DifficultyType = Literal["일반 (+0분)", "보안아파트 (+10분)", "재래시장 (+15분)"]
UnloadMethodType = Literal["수작업", "지게차"]
