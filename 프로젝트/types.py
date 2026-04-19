"""
types.py — LogiTrack 타입 정의

개선 사항:
  - TruckStats: fuel_cost 필드 추가 (app.py 집계 로직 연동)
  - OptimizationResult: wait_time_total 타입 float → float 명시
  - WeatherFactor 타입 추가 (0.7~1.0 float)
  - VehicleInfo: temperature_caps 필드명 오류 수정 (temperature_caps)
  - 모든 TypedDict에 docstring 추가
"""
from typing import TypedDict, List, Dict, Optional, Literal


# ── 웨더·분류 타입 별칭 ───────────────────────────

WeatherType      = Literal["맑음", "비 (감속 20%)", "눈 (감속 30%)"]
WeatherFactor    = float   # 0.7 ~ 1.0
TemperatureType  = Literal["상온", "냉장", "냉동"]
PriorityType     = Literal["VIP", "일반", "여유"]
TimeWindowType   = Literal["Hard", "Soft"]
DifficultyType   = Literal["일반 (+0분)", "보안아파트 (+10분)", "재래시장 (+15분)"]
UnloadMethodType = Literal["수작업", "지게차"]

CacheKey   = str
CacheValue = Dict


# ── 노드/지점 타입 ────────────────────────────────

class LocationDict(TypedDict, total=False):
    """거점 정보 (DB locations 테이블 대응)."""

    name: str    # 거점명 (UNIQUE)
    lat:  float  # 위도
    lon:  float  # 경도
    addr: str    # 정제된 주소


class NodeDict(TypedDict, total=False):
    """배송 노드 정보 (LocationDict 확장)."""

    name:         str             # 거점명
    lat:          float           # 위도
    lon:          float           # 경도
    addr:         str             # 주소
    weight:       float           # 중량 (kg)
    volume:       float           # 부피 (CBM)
    temperature:  TemperatureType # 온도 조건
    unload_method:UnloadMethodType# 하차 방식
    difficulty:   str             # 진입 난이도 (DifficultyType 포함)
    priority:     PriorityType    # 배차 우선순위
    tw_type:      TimeWindowType  # 시간창 제약 유형
    tw_start:     int             # 시간창 시작 (분 단위, 출발 기준 오프셋)
    tw_end:       int             # 시간창 종료 (분 단위)
    tw_disp:      str             # "09:00~18:00" 표시용 문자열
    memo:         str             # 현장 메모
    _node_uid:    str             # 내부 고유 ID (UUID)
    arr_min:      int             # 솔버 계산 도착 시각 (분)


# ── 차량 타입 ─────────────────────────────────────

class VehicleInfo(TypedDict):
    """차량 정보 (솔버 입력 및 결과 집계용)."""

    name:           str        # 차량명 (예: "1톤(T1)")
    max_weight:     float      # 최대 중량 (kg)
    max_volume:     float      # 최대 부피 (CBM)
    cost:           int        # 고정비 (원)
    skill:          float      # 운전자 숙련도 계수 (0.5~2.0)
    temperature_caps:List[str] # 지원 온도 목록 (예: ["상온", "냉장"])


# ── 경로 타입 ─────────────────────────────────────

class RouteSegment(TypedDict):
    """단일 경로 세그먼트 정보."""

    from_name:   str              # 출발 거점명
    to_name:     str              # 도착 거점명
    dist:        float            # 거리 (km)
    time:        float            # 기상 보정 이동 시간 (분)
    raw_time:    float            # 기상 보정 전 이동 시간 (분)
    toll:        int              # 통행료 (원)
    path:        List[List[float]]# [[lat, lon], ...] 경로 좌표
    is_fallback: bool             # True이면 Manhattan 추정 경로


class RouteMatrix(TypedDict):
    """경로 행렬 (build_real_time_matrix 반환값 대응)."""

    combined:     List[List[int]]  # travel_time + service_time 행렬
    travel_time:  List[List[int]]  # 이동 시간 행렬 (분)
    service_time: List[int]        # 노드별 서비스 시간 (분)
    distance:     List[List[float]]# 거리 행렬 (km)
    toll:         List[List[int]]  # 통행료 행렬 (원)


# ── 솔버 결과 타입 ────────────────────────────────

class TruckStats(TypedDict):
    """차량별 운행 통계."""

    dist:         float      # 총 이동 거리 (km)
    time:         float      # 총 운행 시간 (분, 서비스 시간 포함)
    wait_time:    float      # 총 대기 시간 (분)
    stops:        int        # 배송 정차 횟수
    toll_cost:    int        # 통행료 합계 (원)
    fuel_liter:   float      # 연료 소비량 (L)
    fuel_cost:    float      # 연료비 (원) — compute_truck_financials 결과
    co2_kg:       float      # CO₂ 배출량 (kg)
    cost:         int        # 고정비 (원)
    max_wt:       float      # 최대 적재 중량 (kg)
    max_vol:      float      # 최대 적재 부피 (CBM)
    used_wt:      float      # 실제 적재 중량 (kg)
    used_vol:     float      # 실제 적재 부피 (CBM)
    route_names:  List[str]  # 경유 거점명 리스트
    loads_detail: List[Dict] # LIFO 상차 상세 [{name, diff, wt}, ...]


class ReportRow(TypedDict):
    """운행지시서 단일 행."""

    트럭:     str  # 차량명
    거점:     str  # 거점명
    도착:     str  # 예상 도착 시각 ("HH:MM" 또는 "HH:MM ⚠️지연")
    약속시간: str  # 배송 시간창 표시 ("HH:MM~HH:MM")
    거리:     str  # 구간 거리 ("X.Xkm")
    잔여무게: str  # 배송 후 잔여 중량 ("XXXkg")
    잔여부피: str  # 배송 후 잔여 부피 ("X.XXfCBM")
    메모:     str  # 현장 메모


class UnassignedDiagnosis(TypedDict):
    """미배차 노드 진단 결과."""

    name:   str  # 거점명
    reason: str  # 배차 실패 원인 설명


class OptimizationResult(TypedDict):
    """최적화 실행 결과 전체."""

    report:               List[ReportRow]            # 운행지시서 행 리스트
    dist:                 float                      # 총 이동 거리 (km)
    fuel_cost:            float                      # 연료비 합계 (원)
    toll_cost:            int                        # 통행료 합계 (원)
    co2_total:            float                      # 총 CO₂ 배출량 (kg)
    labor:                float                      # 인건비 합계 (원)
    fixed_cost:           int                        # 고정비 합계 (원)
    total_cost:           float                      # 총 비용 (원)
    truck_stats:          Dict[str, TruckStats]      # 차량명 → 통계
    paths:                List[Dict]                 # 지도용 경로 정보
    routes:               List[List[NodeDict]]       # 차량별 노드 경로
    efficiency:           float                      # 거리 효율 (%, NN 대비)
    nn_real_dist:         float                      # NN 기준 거리 (km)
    hub_name:             str                        # 허브 거점명
    hub_loc:              LocationDict               # 허브 좌표
    unassigned:           List[NodeDict]             # 미배차 노드 리스트
    unassigned_diagnosed: List[UnassignedDiagnosis]  # 미배차 진단 결과
    sla:                  float                      # 납기 준수율 (%)
    late_count:           int                        # 지연 건수
    wait_time_total:      float                      # 총 대기 시간 (분)


# ── 시나리오 타입 ─────────────────────────────────

class ScenarioData(TypedDict):
    """DB scenarios 테이블 대응 시나리오 데이터."""

    s_name:     str                        # 시나리오 이름
    targets:    List[NodeDict]             # 배송 노드 리스트
    result:     Optional[OptimizationResult] # 최적화 결과 (없으면 None)
    start_node: str                        # 허브 거점명
    cfg:        Dict                       # 설정 dict
    created_at: str                        # 생성 시각 ("YYYY-MM-DD HH:MM")


# ── 설정 타입 ─────────────────────────────────────

class SimulationConfig(TypedDict):
    """시뮬레이션 파라미터 설정."""

    speed:              int   # 기준 속도 (km/h)
    service_min:        int   # 기본 서비스 시간 (분)
    service_sec_per_kg: int   # kg당 추가 서비스 시간 (초)
    congestion_penalty: int   # 혼잡 페널티 (%)
    start_time:         str   # 출발 시각 ("HH:MM")
    weather:            str   # 기상 상태 (WeatherType)
    max_work_hours:     int   # 최대 근로 시간 (시간)
    balance_workload:   bool  # 업무량 균등 배분 여부
    vrptw_time_limit:   int   # OR-Tools 제한 시간 (초)


class CostConfig(TypedDict):
    """비용 파라미터 설정."""

    fuel_price:      int  # 연료 단가 (원/L)
    labor_per_hour:  int  # 시간당 인건비 (원/h)
