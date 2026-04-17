"""
exceptions.py — LogiTrack 커스텀 예외 클래스
명확한 에러 처리를 위한 예외 계층 구조
"""


class LogiTrackError(Exception):
    """LogiTrack 기본 예외 클래스"""
    def __init__(self, message: str, details: dict = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)
    
    def __str__(self):
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class DatabaseError(LogiTrackError):
    """데이터베이스 관련 오류"""
    pass


class ConnectionPoolError(DatabaseError):
    """커넥션 풀 관련 오류"""
    pass


class APIError(LogiTrackError):
    """외부 API 관련 오류"""
    pass


class KakaoAPIError(APIError):
    """Kakao API 오류"""
    def __init__(self, message: str, status_code: int = None, details: dict = None):
        super().__init__(message, details)
        self.status_code = status_code


class RoutingError(LogiTrackError):
    """경로 계산 관련 오류"""
    pass


class SolverError(LogiTrackError):
    """OR-Tools 솔버 관련 오류"""
    pass


class NoFeasibleSolutionError(SolverError):
    """실행 가능한 해가 없는 경우"""
    pass


class ValidationError(LogiTrackError):
    """데이터 검증 오류"""
    pass


class ConfigurationError(LogiTrackError):
    """설정 오류"""
    pass


class TimeWindowError(ValidationError):
    """시간창 관련 오류"""
    pass


class CapacityError(ValidationError):
    """용량 초과 오류"""
    pass


# 에러 코드 매핑
ERROR_CODES = {
    "DB001": "데이터베이스 연결 실패",
    "DB002": "쿼리 실행 오류",
    "DB003": "커넥션 풀 고갈",
    
    "API001": "API 응답 없음",
    "API002": "API 응답 타임아웃",
    "API003": "API 인증 실패",
    "API004": "API 요청 제한 초과",
    
    "ROUTE001": "경로를 찾을 수 없음",
    "ROUTE002": "매트릭스 구축 실패",
    
    "SOLVER001": "해를 찾을 수 없음",
    "SOLVER002": "시간창 제약 위반",
    "SOLVER003": "용량 제약 위반",
    
    "VAL001": "잘못된 입력 데이터",
    "VAL002": "필수 필드 누락",
}


def get_error_message(error_code: str) -> str:
    """에러 코드로부터 메시지 가져오기"""
    return ERROR_CODES.get(error_code, "알 수 없는 오류")
