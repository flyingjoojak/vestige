"""벡터 인덱스 = 재생성 가능한 파생물. numpy 브루트포스(정확·현 규모 최적).

vectors.npy(2D float32, L2정규화됨) + vector_ids.json(행 순서 chunk_key).
정규화 벡터라 코사인 = 내적 → 검색은 matrix @ query 한 번.
수십만 넘어가면 hnswlib로 승격(아카이브에서 재색인, 무손실).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from .config import VECTOR_IDS_PATH, VECTORS_PATH


class VectorIndex:
    def __init__(self, vectors_path: str | Path = VECTORS_PATH, ids_path: str | Path = VECTOR_IDS_PATH):
        self.vectors_path = Path(vectors_path)
        self.ids_path = Path(ids_path)
        self.ids: list[str] = []
        self.matrix: np.ndarray | None = None
        self._pos: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if self.vectors_path.exists() and self.ids_path.exists():
            self.matrix = np.load(self.vectors_path)
            self.ids = json.loads(self.ids_path.read_text(encoding="utf-8"))
            self._pos = {k: i for i, k in enumerate(self.ids)}

    def __len__(self) -> int:
        return len(self.ids)

    def add(self, keys: list[str], matrix: np.ndarray) -> None:
        """키-벡터 추가. 이미 있는 키는 벡터를 교체(멱등 재임베딩)."""
        if len(keys) == 0:
            return
        matrix = np.asarray(matrix, dtype=np.float32)
        if self.matrix is None:
            self.matrix = np.zeros((0, matrix.shape[1]), dtype=np.float32)

        new_keys, new_rows = [], []
        for key, vec in zip(keys, matrix):
            if key in self._pos:
                self.matrix[self._pos[key]] = vec
            else:
                new_keys.append(key)
                new_rows.append(vec)
        if new_rows:
            start = self.matrix.shape[0]
            self.matrix = np.vstack([self.matrix, np.asarray(new_rows, dtype=np.float32)])
            for off, key in enumerate(new_keys):
                self._pos[key] = start + off
                self.ids.append(key)

    def remove(self, keys: list[str]) -> int:
        """주어진 키의 벡터를 제거(행렬·ids·pos 재구축). 제거된 개수 반환."""
        drop = {k for k in keys if k in self._pos}
        if not drop or self.matrix is None:
            return 0
        keep = [i for i, k in enumerate(self.ids) if k not in drop]
        self.matrix = self.matrix[keep] if keep else np.zeros((0, self.matrix.shape[1]), dtype=np.float32)
        self.ids = [self.ids[i] for i in keep]
        self._pos = {k: i for i, k in enumerate(self.ids)}
        return len(drop)

    def search(self, query_vec: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        if self.matrix is None or self.matrix.shape[0] == 0:
            return []
        scores = self.matrix @ np.asarray(query_vec, dtype=np.float32)
        k = min(k, scores.shape[0])
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self.ids[i], float(scores[i])) for i in top]

    def keys(self) -> list[str]:
        """전체 chunk_key 목록(벡터 로드 없이) — reconcile용."""
        return list(self.ids)

    def all_vectors(self):
        """(keys, matrix) 전체 반환 — 의미 지도 투영용."""
        if self.matrix is None or len(self.ids) == 0:
            return [], np.zeros((0, 0), dtype=np.float32)
        return list(self.ids), self.matrix

    def reset(self) -> None:
        """전체 비우기(모델 교체 재색인용). 메모리·파일 모두 초기화."""
        self.ids = []
        self.matrix = None
        self._pos = {}
        for p in (self.vectors_path, self.ids_path):
            Path(p).unlink(missing_ok=True)

    def save(self) -> None:
        """원자적 저장: temp에 쓰고 rename → kill 중에도 파일 손상 없음."""
        self.vectors_path.parent.mkdir(parents=True, exist_ok=True)
        mat = self.matrix if self.matrix is not None else np.zeros((0, 0), dtype=np.float32)

        tmp_v = self.vectors_path.with_name(self.vectors_path.name + ".tmp")
        with open(tmp_v, "wb") as f:
            np.save(f, mat)
        os.replace(tmp_v, self.vectors_path)

        tmp_i = self.ids_path.with_name(self.ids_path.name + ".tmp")
        tmp_i.write_text(json.dumps(self.ids, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_i, self.ids_path)


class SqliteVecIndex:
    """sqlite-vec 백엔드: 벡터를 디스크(sqlite)에 int8로 저장 → RAM 극소·용량 1/4.

    인터페이스는 VectorIndex와 동일(add/search/remove/reset/save/__len__).
    정규화 벡터를 [-127,127] int8로 양자화, cosine 거리로 KNN. score=1-거리(높을수록 유사).
    """

    def __init__(self, db_path=None):
        import sqlite3

        import sqlite_vec

        from .config import VECTORS_DB_PATH
        self.db_path = Path(db_path or VECTORS_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS vmeta(k TEXT PRIMARY KEY, v TEXT)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS vkeys(id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE)")
        self._dim = self._get_dim()
        if self._dim:
            self._ensure_vec(self._dim)

    def _get_dim(self) -> int | None:
        row = self.conn.execute("SELECT v FROM vmeta WHERE k='dim'").fetchone()
        return int(row[0]) if row else None

    def _ensure_vec(self, dim: int) -> None:
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec USING vec0("
            f"embedding int8[{dim}] distance_metric=cosine)")
        if self._dim is None:
            self.conn.execute("INSERT OR REPLACE INTO vmeta(k,v) VALUES('dim',?)", (str(dim),))
            self._dim = dim

    @staticmethod
    def _q8(vec) -> bytes:
        return np.clip(np.round(np.asarray(vec, dtype=np.float32) * 127), -127, 127).astype(np.int8).tobytes()

    def __len__(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM vkeys").fetchone()[0]

    def add(self, keys: list[str], matrix) -> None:
        if len(keys) == 0:
            return
        matrix = np.asarray(matrix, dtype=np.float32)
        self._ensure_vec(matrix.shape[1])
        cur = self.conn.cursor()
        for key, vec in zip(keys, matrix):
            row = cur.execute("SELECT id FROM vkeys WHERE key=?", (key,)).fetchone()
            if row:  # 멱등 재임베딩: 기존 rowid 벡터 교체
                rid = row[0]
                cur.execute("DELETE FROM vec WHERE rowid=?", (rid,))
            else:
                cur.execute("INSERT INTO vkeys(key) VALUES(?)", (key,))
                rid = cur.lastrowid
            cur.execute("INSERT INTO vec(rowid, embedding) VALUES(?, vec_int8(?))", (rid, self._q8(vec)))
        self.conn.commit()

    def search(self, query_vec, k: int = 5) -> list[tuple[str, float]]:
        if not self._dim or len(self) == 0:
            return []
        rows = self.conn.execute(
            "SELECT v.rowid, v.distance, k.key FROM vec v JOIN vkeys k ON k.id=v.rowid "
            "WHERE v.embedding MATCH vec_int8(?) AND k=? ORDER BY v.distance",
            (self._q8(query_vec), k)).fetchall()
        return [(r[2], 1.0 - float(r[1])) for r in rows]  # cosine 유사도 = 1 - 거리

    def remove(self, keys: list[str]) -> int:
        n = 0
        cur = self.conn.cursor()
        for key in keys:
            row = cur.execute("SELECT id FROM vkeys WHERE key=?", (key,)).fetchone()
            if row:
                cur.execute("DELETE FROM vec WHERE rowid=?", (row[0],))
                cur.execute("DELETE FROM vkeys WHERE id=?", (row[0],))
                n += 1
        self.conn.commit()
        return n

    def keys(self) -> list[str]:
        """전체 chunk_key 목록 — reconcile용."""
        return [r[0] for r in self.conn.execute("SELECT key FROM vkeys").fetchall()]

    def all_vectors(self):
        """(keys, matrix) 전체 반환(int8→float 역양자화) — 의미 지도 투영용."""
        if not self._dim:
            return [], np.zeros((0, 0), dtype=np.float32)
        rows = self.conn.execute(
            "SELECT k.key, v.embedding FROM vec v JOIN vkeys k ON k.id=v.rowid").fetchall()
        if not rows:
            return [], np.zeros((0, 0), dtype=np.float32)
        keys = [r[0] for r in rows]
        mat = np.stack([np.frombuffer(r[1], dtype=np.int8).astype(np.float32) / 127.0 for r in rows])
        return keys, mat

    def reset(self) -> None:
        """전체 비우기(모델 교체=차원 변경 대응): 테이블 드롭 후 재생성."""
        self.conn.execute("DROP TABLE IF EXISTS vec")
        self.conn.execute("DELETE FROM vkeys")
        self.conn.execute("DELETE FROM vmeta")
        self.conn.commit()
        self._dim = None

    def save(self) -> None:
        self.conn.commit()  # sqlite는 커밋이 곧 영속화


def make_index(backend: str | None = None):
    """설정에 따라 벡터 인덱스 백엔드 선택. npy(기본) / sqlite-vec(배포).

    sqlite-vec 는 sqlite3 확장 로드가 필요한데, 일부 macOS 파이썬은 보안상
    `enable_load_extension` 없이 빌드된다 → 그 경우 npy 로 자동 폴백(RAM 더 쓰지만 동작).
    """
    import logging
    from .config import VECTOR_BACKEND
    b = (backend or VECTOR_BACKEND or "npy").lower()
    if b in ("sqlite-vec", "sqlite_vec", "sqlitevec"):
        try:
            return SqliteVecIndex()
        except Exception as e:  # noqa: BLE001 — 확장 로드 불가 등 어떤 이유든 안전 폴백
            logging.getLogger(__name__).warning(
                "sqlite-vec 벡터 백엔드 사용 불가(%s) → npy 백엔드로 폴백", e)
            return VectorIndex()
    return VectorIndex()


def vector_count(backend: str | None = None) -> int:
    """벡터 개수만. npy 백엔드에서 make_index()를 쓰면 수백 MB 행렬을 통째로 올리는데,
    개수 하나 보여주려고 그럴 이유가 없다(상태바가 1초마다 물어본다)."""
    from .config import VECTOR_BACKEND
    b = (backend or VECTOR_BACKEND or "npy").lower()
    if b in ("sqlite-vec", "sqlite_vec", "sqlitevec"):
        return len(make_index(b))   # DB 조회라 행렬 적재 없음
    p = Path(VECTOR_IDS_PATH)
    return len(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else 0
