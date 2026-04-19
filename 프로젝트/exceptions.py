"""
exceptions.py — LogiTrack 커스텀 예외 클래스

개선 사항:
  - 모든 예외에 error_code 필드 추가 (ERROR_CODES 연동)
  - from_code() 공장 메서드 추가
  - __repr__ 구현으로 디버깅 편의성 향상
  - KakaoAPIError: response_body 필드 추가
"""
from typing import Optional


# ══════════════════════════════════════════════════
# 에러 코드 레지스트리
# ══════════════════════════════════════════════════

ERROR_CODES: dict[str, str] = {
    # 데이터베이스
    "DB001": "데이터베이스 연결 실패",
    "DB002": "쿼리 실행 오류",
    "DB003": "커넥션 풀 고갈",
    # API
    "API001": "API 응답 없음",
    "API002": "API 응답 타임아웃",
    "API003": "API 인증 실패",
    "API004": "API 요청 제한 초과",
    # 경로
    "ROUTE001": "경로를 찾을 수 없음",
    "ROUTE002": "매트릭스 구축 실패",
    # 솔버
    "SOLVER001": "해를 찾을 수 없음",
    "SOLVER002": "시간창 제약 위반",
    "SOLVER003": "용량 제약 위반",
    # 검증
    "VAL001": "잘못된 입력 데이터",
    "VAL002": "필수 필드 누락",
    # 설정
    "CFG001": "필수 환경변수 누락",
    "CFG002": "설정값 범위 오류",
}


def get_error_message(error_code: str) -> str:
    """에러 코드로부터 메시지 반환.

    Args:
        error_code: 에러 코드 문자열 (예: "DB001")

    Returns:
        에러 설명 문자열. 알 수 없는 코드면 "알 수 없는 오류".
    """
    return ERROR_CODES.get(error_code, "알 수 없는 오류")


# ══════════════════════════════════════════════════
# 기본 예외
# ══════════════════════════════════════════════════

class LogiTrackError(Exception):
    """LogiTrack 기본 예외 클래스.

    Attributes:
        message:    사람이 읽을 수 있는 오류 메시지
        details:    추가 컨텍스트 정보
        error_code: ERROR_CODES 키 (예: "DB001")
    """

    def __init__(
        self,
        message: str,
        details:    Optional[dict] = None,
        error_code: Optional[str]  = None,
    ) -> None:
        self.message    = message
        self.details    = details or {}
        self.error_code = error_code
        super().__init__(self.message)

    @classmethod
    def from_code(cls, code: str, details: Optional[dict] = None) -> "LogiTrackError":
        """에러 코드로부터 예외 인스턴스 생성.

        Args:
            code:    ERROR_CODES 키
            details: 추가 컨텍스트

        Returns:
            LogiTrackError 인스턴스
        """
        msg = get_error_message(code)
        return cls(msg, details=details, error_code=code)

    def __str__(self) -> str:
        base = self.message
        if self.error_code:
            base = f"[{self.error_code}] {base}"
        if self.details:
            base += f" | 상세: {self.details}"
        return base

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"error_code={self.error_code!r}, "
            f"details={self.details!r})"
        )


# ══════════════════════════════════════════════════
# 데이터베이스 예외
# ══════════════════════════════════════════════════

class DatabaseError(LogiTrackError):
    """데이터베이스 관련 오류."""


class ConnectionPoolError(DatabaseError):
    """커넥션 풀 고갈 또는 연결 실패."""

    @classmethod
    def from_code(cls, code: str = "DB003", details: Optional[dict] = None) -> "ConnectionPoolError":
        return cls(get_error_message(code), details=details, error_code=code)


# ══════════════════════════════════════════════════
# API 예외
# ══════════════════════════════════════════════════

class APIError(LogiTrackError):
    """외부 API 관련 오류."""


class KakaoAPIError(APIError):
    """Kakao API 오류.

    Attributes:
        status_code:   HTTP 상태 코드
        response_body: 응답 본문 (디버깅용, 일부)
    """

    def __init__(
        self,
        message:       str,
        status_code:   Optional[int] = None,
        details:       Optional[dict] = None,
        response_body: Optional[str]  = None,
        error_code:    Optional[str]  = None,
    ) -> None:
        super().__init__(message, details=details, error_code=error_code)
        self.status_code   = status_code
        self.response_body = response_body

    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code:
            base += f" (HTTP {self.status_code})"
        return base


# ══════════════════════════════════════════════════
# 경로 예외
# ══════════════════════════════════════════════════

class RoutingError(LogiTrackError):
    """경로 계산 관련 오류."""


# ══════════════════════════════════════════════════
# 솔버 예외
# ══════════════════════════════════════════════════

class SolverError(LogiTrackError):
    """OR-Tools 솔버 관련 오류."""


class NoFeasibleSolutionError(SolverError):
    """실행 가능한 해가 없는 경우.

    가능 원인:
        - 시간창이 너무 좁음
        - 차량 용량 부족
        - 온도 조건 불일치
    """

    def __init__(
        self,
        message:    str = "실행 가능한 해를 찾을 수 없습니다.",
        details:    Optional[dict] = None,
        error_code: str = "SOLVER001",
    ) -> None:
        super().__init__(message, details=details, error_code=error_code)


# ══════════════════════════════════════════════════
# 검증 예외
# ══════════════════════════════════════════════════

class ValidationError(LogiTrackError):
    """데이터 검증 오류."""

    @classmethod
    def missing_field(cls, field_name: str) -> "ValidationError":
        """필수 필드 누락 예외 생성 헬퍼.

        Args:
            field_name: 누락된 필드명

        Returns:
            ValidationError 인스턴스
        """
        return cls(
            f"필수 필드 '{field_name}'이 없습니다.",
            details={"field": field_name},
            error_code="VAL002",
        )


class TimeWindowError(ValidationError):
    """시간창 관련 오류 (너무 좁거나, 시작 > 종료)."""


class CapacityError(ValidationError):
    """용량 초과 오류."""


# ══════════════════════════════════════════════════
# 설정 예외
# ══════════════════════════════════════════════════

class ConfigurationError(LogiTrackError):
    """설정 오류 (환경변수 누락·범위 초과 등)."""

    @classmethod
    def missing_env(cls, env_var: str) -> "ConfigurationError":
        """환경변수 누락 예외 생성 헬퍼.

        Args:
            env_var: 누락된 환경변수명

        Returns:
            ConfigurationError 인스턴스
        """
        return cls(
            f"필수 환경변수 '{env_var}'이 설정되지 않았습니다.",
            details={"env_var": env_var},
            error_code="CFG001",
        )
