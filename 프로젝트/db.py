"""
db.py — DB 매니저

개선 사항:
  - 모든 공개 메서드에 타입 힌팅·docstring 추가
  - config.py 상수 연동 (pool_min/max, timeout, ttl)
  - purge_old_route_cache: TTL 기본값 config에서 로드
  - list_scenarios: LIMIT 상수화
  - UniqueViolation 임포트 경로 명시 (psycopg2.errors)
"""
import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool
from psycopg2 import errors as pg_errors
import streamlit as st

try:
    from config import db_config
    _POOL_MIN       = db_config.POOL_MIN
    _POOL_MAX       = db_config.POOL_MAX
    _CONNECT_TIMEOUT = db_config.CONNECT_TIMEOUT
    _CACHE_TTL_HOURS = db_config.CACHE_TTL_HOURS
except ImportError:
    _POOL_MIN, _POOL_MAX, _CONNECT_TIMEOUT, _CACHE_TTL_HOURS = 1, 10, 5, 12

logger = logging.getLogger("logitrack")

_SCENARIO_LIST_LIMIT = 30


@st.cache_resource
def get_db_pool(db_url: str) -> pool.SimpleConnectionPool:
    """앱 생애주기 동안 단 한 번만 생성되는 커넥션 풀.

    Args:
        db_url: PostgreSQL 연결 URL

    Returns:
        SimpleConnectionPool 인스턴스
    """
    return pool.SimpleConnectionPool(
        _POOL_MIN, _POOL_MAX, db_url,
        sslmode="require",
        connect_timeout=_CONNECT_TIMEOUT,
    )


class DBManager:
    """PostgreSQL 기반 LogiTrack DB 매니저.

    테이블:
        - locations:   거점 정보 (name, lat, lon, addr)
        - scenarios:   시나리오 저장 (s_name, targets_data, result_data, ...)
        - route_cache: 경로 API 응답 캐시 (cache_key, result_data, created_at)
    """

    def __init__(self, db_url: str) -> None:
        self.pool = get_db_pool(db_url)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS locations
                    (name TEXT UNIQUE, lat REAL, lon REAL, addr TEXT)
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS scenarios (
                        s_name       TEXT UNIQUE,
                        targets_data TEXT    DEFAULT '',
                        result_data  TEXT    DEFAULT '',
                        created_at   TEXT    DEFAULT '2024-01-01 00:00',
                        start_node   TEXT    DEFAULT '',
                        cfg_data     TEXT    DEFAULT '{}'
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS route_cache (
                        cache_key   TEXT UNIQUE,
                        result_data TEXT,
                        created_at  TIMESTAMP DEFAULT NOW()
                    )
                """)
            conn.commit()

    @contextmanager
    def _conn(self) -> Generator:
        """커넥션을 yield하고 finally에서 반드시 반환.

        예외 발생 시 rollback 후 putconn — 더러운 커넥션 방지.

        Yields:
            psycopg2 connection
        """
        conn = self.pool.getconn()
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self.pool.putconn(conn)

    # ── locations ────────────────────────────────

    def load_locations(self) -> list[dict]:
        """모든 거점 목록을 name 오름차순으로 반환.

        Returns:
            거점 dict 리스트. DB 오류 시 빈 리스트.
        """
        with self._conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM locations ORDER BY name")
                    return [dict(r) for r in cur.fetchall()]
            except Exception as e:
                logger.warning("load_locations: %s", e)
                return []

    def insert_location(
        self,
        name: str,
        lat: float,
        lon: float,
        addr: str,
    ) -> tuple[bool, Optional[str]]:
        """새 거점을 삽입.

        Args:
            name: 거점명 (UNIQUE)
            lat:  위도
            lon:  경도
            addr: 정제된 주소

        Returns:
            (성공 여부, 오류 사유) — 성공 시 (True, None), 중복 시 (False, "duplicate")
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO locations VALUES (%s, %s, %s, %s)",
                        (name, lat, lon, addr),
                    )
                conn.commit()
                return True, None
            except pg_errors.UniqueViolation:
                conn.rollback()
                return False, "duplicate"
            except Exception as e:
                conn.rollback()
                logger.warning("insert_location: %s", e)
                return False, str(e)

    def update_location(
        self,
        name: str,
        lat: float,
        lon: float,
        addr: str,
    ) -> tuple[bool, Optional[str]]:
        """기존 거점의 좌표·주소를 갱신.

        Args:
            name: 수정할 거점명
            lat:  새 위도
            lon:  새 경도
            addr: 새 정제 주소

        Returns:
            (성공 여부, 오류 메시지)
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE locations SET lat=%s, lon=%s, addr=%s WHERE name=%s",
                        (lat, lon, addr, name),
                    )
                conn.commit()
                return True, None
            except Exception as e:
                conn.rollback()
                logger.warning("update_location: %s", e)
                return False, str(e)

    def delete_location(self, name: str) -> None:
        """거점을 삭제.

        Args:
            name: 삭제할 거점명
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM locations WHERE name=%s", (name,))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("delete_location: %s", e)

    # ── route_cache ──────────────────────────────

    def get_route_cache(self, cache_key: str) -> Optional[dict]:
        """경로 캐시 조회.

        Args:
            cache_key: 캐시 식별 키

        Returns:
            캐시 dict 또는 None (미존재·오류)
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT result_data FROM route_cache WHERE cache_key=%s",
                        (cache_key,),
                    )
                    res = cur.fetchone()
                    return json.loads(res[0]) if res else None
            except Exception as e:
                logger.warning("get_route_cache: %s", e)
                return None

    def save_route_cache(self, cache_key: str, data_dict: dict) -> None:
        """경로 캐시 저장 (이미 존재하면 무시).

        Args:
            cache_key:  캐시 식별 키
            data_dict:  저장할 경로 데이터
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO route_cache (cache_key, result_data) VALUES (%s, %s) "
                        "ON CONFLICT (cache_key) DO NOTHING",
                        (cache_key, json.dumps(data_dict)),
                    )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("save_route_cache: %s", e)

    def purge_old_route_cache(self, ttl_hours: int = _CACHE_TTL_HOURS) -> None:
        """TTL 초과 경로 캐시 삭제.

        Args:
            ttl_hours: 보존 시간 (기본값: config.CACHE_TTL_HOURS)
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM route_cache "
                        f"WHERE created_at < NOW() - INTERVAL '{int(ttl_hours)} hours'"
                    )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("purge_old_route_cache: %s", e)

    # ── scenarios ────────────────────────────────

    def save_scenario(
        self,
        s_name:     str,
        targets:    list,
        result:     Optional[dict],
        start_node: str,
        cfg:        dict,
    ) -> tuple[bool, Optional[str]]:
        """시나리오를 저장 (이미 존재하면 UPSERT).

        Args:
            s_name:     시나리오 이름 (UNIQUE)
            targets:    배송 노드 리스트
            result:     최적화 결과 dict (없으면 None)
            start_node: 허브 거점명
            cfg:        설정 dict

        Returns:
            (성공 여부, 오류 메시지)
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO scenarios
                            (s_name, targets_data, result_data, created_at, start_node, cfg_data)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (s_name) DO UPDATE SET
                            targets_data = EXCLUDED.targets_data,
                            result_data  = EXCLUDED.result_data,
                            created_at   = EXCLUDED.created_at,
                            start_node   = EXCLUDED.start_node,
                            cfg_data     = EXCLUDED.cfg_data
                    """, (
                        s_name,
                        json.dumps(targets, ensure_ascii=False),
                        json.dumps(result,  ensure_ascii=False) if result else "",
                        datetime.now().strftime("%Y-%m-%d %H:%M"),
                        start_node,
                        json.dumps(cfg, ensure_ascii=False),
                    ))
                conn.commit()
                return True, None
            except Exception as e:
                conn.rollback()
                logger.warning("save_scenario: %s", e)
                return False, str(e)

    def load_scenario(self, s_name: str) -> Optional[dict]:
        """시나리오 로드.

        Args:
            s_name: 시나리오 이름

        Returns:
            시나리오 dict 또는 None (미존재·오류)
        """
        with self._conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM scenarios WHERE s_name=%s", (s_name,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    return {
                        "s_name":     row["s_name"],
                        "targets":    json.loads(row["targets_data"]) if row["targets_data"] else [],
                        "result":     json.loads(row["result_data"])  if row["result_data"]  else None,
                        "start_node": row["start_node"],
                        "cfg":        json.loads(row["cfg_data"])     if row["cfg_data"]     else {},
                        "created_at": row["created_at"],
                    }
            except Exception as e:
                logger.warning("load_scenario: %s", e)
                return None

    def list_scenarios(self) -> list[dict]:
        """최근 시나리오 목록 반환 (생성일 내림차순, 최대 30개).

        Returns:
            시나리오 요약 dict 리스트. DB 오류 시 빈 리스트.
        """
        with self._conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT s_name, created_at, start_node FROM scenarios "
                        "ORDER BY created_at DESC LIMIT %s",
                        (_SCENARIO_LIST_LIMIT,),
                    )
                    return [dict(r) for r in cur.fetchall()]
            except Exception as e:
                logger.warning("list_scenarios: %s", e)
                return []

    def delete_scenario(self, s_name: str) -> None:
        """시나리오 삭제.

        Args:
            s_name: 삭제할 시나리오 이름
        """
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM scenarios WHERE s_name=%s", (s_name,))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("delete_scenario: %s", e)
