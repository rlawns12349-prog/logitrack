"""
db.py — DB 매니저
  - PostgreSQL 커넥션 풀 (psycopg2)
  - locations / scenarios / route_cache CRUD
  - contextmanager 기반 _conn() 으로 putconn 누락 방지 (R-5)
"""
import json
import logging
from contextlib import contextmanager
from datetime import datetime

import psycopg2
import psycopg2.extras
from psycopg2 import pool
from psycopg2 import errors as pg_errors
import streamlit as st

logger = logging.getLogger("logitrack")


@st.cache_resource
def get_db_pool(db_url: str):
    """앱 생애주기 동안 단 한 번만 생성되는 커넥션 풀 (B-6: connect_timeout)"""
    return pool.SimpleConnectionPool(
        1, 10, db_url,
        sslmode='require',
        connect_timeout=5,
    )


class DBManager:
    def __init__(self, db_url: str):
        self.pool = get_db_pool(db_url)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS locations
                    (name TEXT UNIQUE, lat REAL, lon REAL, addr TEXT)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS scenarios (
                    s_name TEXT UNIQUE, targets_data TEXT DEFAULT '',
                    result_data TEXT DEFAULT '', created_at TEXT DEFAULT '2024-01-01 00:00',
                    start_node TEXT DEFAULT '', cfg_data TEXT DEFAULT '{}')""")
                cur.execute("""CREATE TABLE IF NOT EXISTS route_cache (
                    cache_key TEXT UNIQUE, result_data TEXT,
                    created_at TIMESTAMP DEFAULT NOW())""")
            conn.commit()

    @contextmanager
    def _conn(self):
        """커넥션을 yield 하고 finally에서 반드시 반환 (R-5)
        D-3: 예외 발생 시 rollback 후 putconn — 더러운 커넥션 방지
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
    def load_locations(self):
        # D-2: 연결 실패 시 앱 다운 방지
        with self._conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM locations ORDER BY name")
                    return [dict(r) for r in cur.fetchall()]
            except Exception as e:
                logger.warning("load_locations: %s", e)
                return []

    def insert_location(self, name, lat, lon, addr):
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO locations VALUES (%s,%s,%s,%s)", (name, lat, lon, addr))
                conn.commit()
                return True, None
            except pg_errors.UniqueViolation:
                conn.rollback()
                return False, "duplicate"
            except Exception as e:
                conn.rollback()
                logger.warning("insert_location: %s", e)
                return False, str(e)

    def update_location(self, name, lat, lon, addr):
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("UPDATE locations SET lat=%s,lon=%s,addr=%s WHERE name=%s",
                                (lat, lon, addr, name))
                conn.commit()
                return True, None
            except Exception as e:
                conn.rollback()
                logger.warning("update_location: %s", e)
                return False, str(e)

    def delete_location(self, name):
        # D-1: 예외 처리 추가 — DB 오류 시 크래시 방지
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM locations WHERE name=%s", (name,))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("delete_location: %s", e)

    # ── route_cache ──────────────────────────────
    def get_route_cache(self, cache_key):
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT result_data FROM route_cache WHERE cache_key=%s", (cache_key,))
                    res = cur.fetchone()
                    return json.loads(res[0]) if res else None
            except Exception as e:
                logger.warning("get_route_cache: %s", e)
                return None

    def save_route_cache(self, cache_key, data_dict):
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO route_cache (cache_key,result_data) VALUES (%s,%s) "
                        "ON CONFLICT (cache_key) DO NOTHING",
                        (cache_key, json.dumps(data_dict)),
                    )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("save_route_cache: %s", e)

    def purge_old_route_cache(self, ttl_hours=12):
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
    def save_scenario(self, s_name, targets, result, start_node, cfg):
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO scenarios
                            (s_name, targets_data, result_data, created_at, start_node, cfg_data)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (s_name) DO UPDATE SET
                            targets_data = EXCLUDED.targets_data,
                            result_data  = EXCLUDED.result_data,
                            created_at   = EXCLUDED.created_at,
                            start_node   = EXCLUDED.start_node,
                            cfg_data     = EXCLUDED.cfg_data
                    """, (
                        s_name,
                        json.dumps(targets, ensure_ascii=False),
                        json.dumps(result,  ensure_ascii=False) if result else '',
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

    def load_scenario(self, s_name):
        with self._conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT * FROM scenarios WHERE s_name=%s", (s_name,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    return {
                        's_name':     row['s_name'],
                        'targets':    json.loads(row['targets_data']) if row['targets_data'] else [],
                        'result':     json.loads(row['result_data'])  if row['result_data']  else None,
                        'start_node': row['start_node'],
                        'cfg':        json.loads(row['cfg_data'])     if row['cfg_data']     else {},
                        'created_at': row['created_at'],
                    }
            except Exception as e:
                logger.warning("load_scenario: %s", e)
                return None

    def list_scenarios(self):
        with self._conn() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT s_name, created_at, start_node FROM scenarios "
                        "ORDER BY created_at DESC LIMIT 30"
                    )
                    return [dict(r) for r in cur.fetchall()]
            except Exception as e:
                logger.warning("list_scenarios: %s", e)
                return []

    def delete_scenario(self, s_name):
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM scenarios WHERE s_name=%s", (s_name,))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("delete_scenario: %s", e)
