"""의미 지도: 청크 임베딩 벡터 전체를 2D로 투영(UMAP)한 밀도 점 구름 + 군집 + 태그 라벨.

t-SNE/UMAP 논문 그림 같은 임베딩 시각화. 청크 단위(수천 점)로 밀도를 살리고,
KMeans 군집 + 정제 태그 최빈값으로 라벨링. UMAP/sklearn 없으면 PCA·무군집 폴백.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict

import numpy as np


def _project(mat: np.ndarray, dims: int = 2) -> tuple[np.ndarray, str]:
    n = mat.shape[0]
    if n >= 5:
        try:
            import umap
            return (umap.UMAP(n_components=dims, random_state=42, metric="cosine",
                              n_neighbors=min(15, n - 1), min_dist=0.15)
                    .fit_transform(mat).astype(np.float32)), "umap"
        except Exception:
            pass
    xc = mat - mat.mean(axis=0)
    _u, _s, vt = np.linalg.svd(xc, full_matrices=False)
    comp = vt[:dims].T if vt.shape[0] >= dims else np.eye(mat.shape[1], dims, dtype=np.float32)
    return (xc @ comp).astype(np.float32), "pca"


def _cluster(mat: np.ndarray) -> np.ndarray:
    """주제 군집화. 표시용 3D가 아니라 '고차원 임베딩'에서 묶어 주제 분리를 살린다.

    UMAP(cosine)로 군집용 저차원(≈10D)으로 줄인 뒤 HDBSCAN(밀도 기반) → 실제로 뭉친
    덩어리만 군집, 애매한 점은 -1(노이즈)로 남긴다(억지 배정 안 함). HDBSCAN이 안 되거나
    군집이 2개 미만이면 KMeans 폴백. UMAP 없으면 정규화 임베딩 그대로(유클리드≈코사인).
    """
    n = mat.shape[0]
    if n < 6:
        return np.zeros(n, dtype=int)
    feat = mat
    try:
        import umap
        d = min(10, mat.shape[1])
        feat = umap.UMAP(n_components=d, random_state=42, metric="cosine",
                         n_neighbors=min(15, n - 1), min_dist=0.0).fit_transform(mat)
    except Exception:
        feat = mat
    try:
        from sklearn.cluster import HDBSCAN
        mcs = max(15, n // 150)   # 최소 군집 크기 — 이보다 작은 덩어리는 노이즈
        # leaf: 큰 안정 군집(eom 기본)을 하위 주제로 잘게 쪼갬 → '한 군집=한 주제'에 근접.
        labels = HDBSCAN(min_cluster_size=mcs, min_samples=3,
                         cluster_selection_method="leaf").fit_predict(feat)
        cids = sorted({int(x) for x in labels if x >= 0})
        if len(cids) >= 2:
            # 노이즈(-1)를 가장 가까운 군집 중심에 흡수(회색 점 최소화). 순도는 조금 양보.
            noise = np.where(labels < 0)[0]
            if len(noise):
                cents = np.stack([feat[labels == c].mean(axis=0) for c in cids])
                for idx in noise:
                    labels[idx] = cids[int(np.argmin(np.sum((cents - feat[idx]) ** 2, axis=1)))]
            return labels.astype(int)
    except Exception:
        pass
    try:
        from sklearn.cluster import KMeans
        k = max(3, min(16, n // 300))
        return KMeans(n_clusters=k, n_init=4, random_state=42).fit_predict(feat).astype(int)
    except Exception:
        return np.zeros(n, dtype=int)


def _keyword_ranked(tag_counts: Counter, cluster_df: Counter, cluster_count: int, cluster_size: int) -> list[str]:
    """군집의 구별 태그를 점수순으로(상위 4개). 점수 = tf·log(K/df), df=그 태그가 나온 '군집 수'.

    최빈 태그를 그냥 쓰면 Zipf 분포라 모든 군집이 같은 1등이 됨(라벨 겹치던 원인).
    log(K/df)는 '모든 군집에 있는 말'을 0으로 죽이고, 한 군집에만 몰린 말을 올린다.
    라벨은 보통 1개만 쓰되, 이름이 겹치는 군집에서만 다음 태그를 붙여 구별한다.
    """
    if not tag_counts or cluster_count <= 0:
        return []
    min_tf = 2 if cluster_size >= 10 else 1   # 큰 군집에서 1번 나온 말은 주제가 아님
    scored: list[tuple[float, int, str]] = []
    for tag, tf in tag_counts.items():
        if tf < min_tf:
            continue
        df = cluster_df.get(tag, 1)
        w = math.log(cluster_count / df) if cluster_count > 1 else 1.0
        if w <= 0:                              # 모든 군집에 있는 말 → 제외
            continue
        scored.append((tf * w, tf, tag))
    scored.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return [t for _, _, t in scored[:4]]


def _succeed_cluster_ids(new_keys: dict[int, set], prev_members: list | None) -> dict[int, int]:
    """재계산 시 새 군집 ↔ 이전 군집을 구성원 겹침(Jaccard≥0.2)으로 이어 같은 id/색 유지.

    prev_members: [{"id": int, "keys": [청크키...]}]. 없으면 그대로(로컬 id 유지).
    """
    remap: dict[int, int] = {}
    prev = [(int(m["id"]), set(m.get("keys") or [])) for m in (prev_members or [])]
    if prev:
        cands = []
        for cid, ks in new_keys.items():
            if not ks:
                continue
            for pid, pks in prev:
                inter = len(ks & pks)
                if inter and inter / len(ks | pks) >= 0.2:
                    cands.append((inter / len(ks | pks), cid, pid))
        cands.sort(reverse=True)
        used = set()
        for _, cid, pid in cands:               # 겹침 큰 쌍부터 greedy 배정
            if cid not in remap and pid not in used:
                remap[cid] = pid
                used.add(pid)
    next_id = (max((pid for pid, _ in prev), default=-1)) + 1
    taken = set(remap.values())
    for cid in new_keys:                        # 못 이은 군집 = 새 id
        if cid not in remap:
            while next_id in taken:
                next_id += 1
            remap[cid] = next_id
            taken.add(next_id)
            next_id += 1
    return remap


def build_graph(vi, db, dims: int = 2, prev_members: list | None = None) -> dict:
    hidden = db.hidden_turn_ids()   # 숨김(#128) - 투영·군집 계산에 들어가기 전에 빼야 다른 점에도 영향이 없다
    keys, mat = vi.all_vectors()
    if hidden and len(keys):
        keep = [i for i, k in enumerate(keys) if k.rsplit("#", 1)[0] not in hidden]
        if len(keep) != len(keys):
            keys = [keys[i] for i in keep]
            mat = mat[keep]
    if len(keys) == 0:
        return {"points": [], "clusters": [], "paths": [], "method": None, "dims": dims, "_members": []}

    coords, method = _project(mat, dims)    # 표시용 3D 투영
    labels = _cluster(mat)                  # 군집화는 고차원 임베딩에서(표시와 분리)

    meta, tags = {}, {}
    for r in db.conn.execute("SELECT id, session_id, summary, question, tags, timestamp FROM turns").fetchall():
        if r["id"] in hidden:
            continue
        meta[r["id"]] = (r["session_id"], (r["summary"] or r["question"] or ""), r["timestamp"] or "")
        tags[r["id"]] = json.loads(r["tags"]) if r["tags"] else []

    pts = []
    clu_tag: dict[int, Counter] = defaultdict(Counter)
    clu_pos: dict[int, list[np.ndarray]] = defaultdict(list)
    clu_ptidx: dict[int, list[int]] = defaultdict(list)   # 군집→점index(메도이드용)
    clu_keys: dict[int, set] = defaultdict(set)           # 군집→청크키(승계용)
    clu_turns: dict[int, set] = defaultdict(set)          # 군집→고유 turn id(표시 개수용, 청크 아님)
    sess_pts: dict[str, list[tuple[int, str, int]]] = defaultdict(list)  # 세션→[(점index, 시각, 청크idx)]
    for i, k in enumerate(keys):
        parts = k.rsplit("#", 1)
        tid = parts[0]
        cidx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        m = meta.get(tid)
        if not m:
            continue
        sess, head, ts = m
        c = int(labels[i])
        pt = {"x": round(float(coords[i, 0]), 2), "y": round(float(coords[i, 1]), 2),
              "c": c, "s": sess, "h": head[:80], "t": tid}   # t=turn id(점 클릭→그 턴 열기)
        if dims == 3:
            pt["z"] = round(float(coords[i, 2]), 2)
        ptidx = len(pts)
        pts.append(pt)
        sess_pts[sess].append((ptidx, ts, cidx))
        if c >= 0:   # -1(노이즈)은 점으로는 그리되 라벨 군집엔 넣지 않음
            for t in tags.get(tid, []):
                clu_tag[c][t] += 1
            clu_pos[c].append(coords[i])
            clu_ptidx[c].append(ptidx)
            clu_keys[c].add(k)
            clu_turns[c].add(tid)

    # 같은 세션 점을 시간순으로 잇는 경로(성좌) — 2개 이상인 세션만.
    paths = []
    for lst in sess_pts.values():
        if len(lst) < 2:
            continue
        lst.sort(key=lambda x: (x[1], x[2]))
        paths.append([p[0] for p in lst])

    # 군집 id 승계(재계산해도 색 유지) + 점 c 재매핑.
    remap = _succeed_cluster_ids(clu_keys, prev_members)
    for pt in pts:
        pt["c"] = remap.get(pt["c"], pt["c"])   # 노이즈(-1)는 그대로 -1

    # cluster_df: 태그가 등장한 '군집 수'(전체 빈도 아님).
    cluster_df: Counter = Counter()
    for cnt in clu_tag.values():
        for tag in cnt:
            cluster_df[tag] += 1
    K = len(clu_pos)

    # 1차: 군집별 중심·크기·태그 랭킹(구별=idf / 대표=최빈)·폴백(메도이드).
    info: dict[int, dict] = {}
    for c, pos in clu_pos.items():
        cen = np.mean(pos, axis=0)
        size = len(pos)
        min_tf = 2 if size >= 10 else 1
        ranked = _keyword_ranked(clu_tag[c], cluster_df, K, size)          # 구별력(idf)
        common = [t for t, tf in clu_tag[c].most_common(6) if tf >= min_tf][:4]  # 대표(최빈)
        if ranked or common:
            med = ""
        else:                                   # 태그 없으면 중심 최근접 점(메도이드) 헤드라인
            j = min(range(size), key=lambda j: float(np.sum((pos[j] - cen) ** 2)))
            med = (pts[clu_ptidx[c][j]].get("h") or "").strip()[:40]
        info[c] = {"cen": cen, "size": size, "ranked": ranked, "common": common, "med": med}

    # 큰 군집은 니치 태그(idf)가 튀어 오히려 오해를 부름 → '대표(최빈) 태그'로. 작은 군집은 idf 유지.
    sizes = sorted(info[c]["size"] for c in info)
    large_thresh = max(80, int((sizes[len(sizes) // 2] if sizes else 0) * 1.8))

    # 2차: 라벨 배정 — 큰 군집이 단어 1개를 우선 차지, 이름이 겹치면 다음 태그를 붙여 구별.
    used: set[str] = set()
    label_of: dict[int, str] = {}
    for c in sorted(info, key=lambda c: -info[c]["size"]):
        aid = remap[c]
        d = info[c]
        primary = (d["common"] if d["size"] >= large_thresh and d["common"] else d["ranked"]) or d["common"]
        if primary:
            label = primary[0]
            j = 1
            while label in used and j < len(primary):
                label = f"{primary[0]} · {primary[j]}"; j += 1
        else:
            label = d["med"] or f"군집 {aid + 1}"
        if label in used:                       # 그래도 겹치면 번호로 확정
            label = f"{label} ({aid})"
        used.add(label)
        label_of[c] = label

    clusters, members = [], []
    for c in info:
        aid = remap[c]
        cen = info[c]["cen"]
        cl = {"id": aid, "label": label_of[c],
              "x": round(float(cen[0]), 2), "y": round(float(cen[1]), 2), "n": len(clu_turns[c])}
        if dims == 3:
            cl["z"] = round(float(cen[2]), 2)
        clusters.append(cl)
        members.append({"id": aid, "keys": list(clu_keys[c])})
    clusters.sort(key=lambda c: -c["n"])
    return {"points": pts, "clusters": clusters, "paths": paths,
            "method": method, "dims": dims, "_members": members}
