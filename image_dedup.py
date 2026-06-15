"""
图片去重模块 v1.0
=================
基于 phash 的全量/增量图片去重。

设计决策（参见需求分析）：
1. 全局去重 — 整个 ~/Desktop/转发图片/ 共享一个哈希库
2. 首次全量扫描建库 → 后续增量
3. phash 阈值 ≤ 5（抗 QQ 压缩 + compress_image 二次压缩）
4. 先压缩后比对 — 格式归一化为 JPG，减少资源浪费
5. DB 建在 ~/Desktop/转发图片/gallery_hashes.db（跟着图片走）
6. 完整 prune — 定期清理已删文件的残留记录
"""

import os
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Optional

import imagehash
from PIL import Image

from image_utils import compress_image

log = logging.getLogger("gw")

# 支持的图片扩展名（全量扫描用）
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}

# 默认 DB 路径
DEFAULT_FWD_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "转发图片")
DEFAULT_DB_PATH = os.path.join(DEFAULT_FWD_DIR, "gallery_hashes.db")

# phash 汉明距离阈值
DEFAULT_THRESHOLD = 5


class ImageDeduplicator:
    """
    图片去重器。
    线程安全（每个连接使用独立 cursor，通过锁保护共享状态）。
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._lock = threading.Lock()

        # 内存镜像：{hex_hash: [path1, path2, ...]}
        # 用 dict[hex_hash] 而非 ImageHash 对象，避免反复转换
        self.hash_to_paths: dict[str, list[str]] = {}
        # 反向索引：path -> hex_hash（快速判断文件是否已记录）
        self.path_to_hash: dict[str, str] = {}

        self._init_db()

    # ── 数据库初始化 ────────────────────────────────────────

    def _init_db(self):
        """创建数据库和表结构，加载已有记录到内存。"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT    NOT NULL,
                hash_value  TEXT    NOT NULL,
                file_size   INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now', 'localtime')),
                updated_at  TEXT    DEFAULT (datetime('now', 'localtime'))
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON images(hash_value)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_path ON images(file_path)")
        self.conn.commit()
        self._load_memory()

    def _load_memory(self):
        """从 SQLite 加载所有记录到内存镜像。"""
        self.hash_to_paths.clear()
        self.path_to_hash.clear()
        try:
            cursor = self.conn.execute("SELECT file_path, hash_value FROM images")
            for fpath, hval in cursor:
                self.path_to_hash[fpath] = hval
                self.hash_to_paths.setdefault(hval, []).append(fpath)
            log.info("DEDUP_INIT: loaded %d records from %s", len(self.path_to_hash), self.db_path)
        except Exception as e:
            log.warning("DEDUP_INIT_ERR: %s", str(e)[:80])

    # ── 核心操作 ────────────────────────────────────────────

    def compute_hash(self, image_path: str) -> Optional[imagehash.ImageHash]:
        """
        读取图片 → 计算 phash。
        注意：不压缩（调用方自行决定是否提前 compress_image）。
        """
        try:
            img = Image.open(image_path)
            h = imagehash.phash(img)
            img.close()
            return h
        except Exception as e:
            log.warning("DEDUP_HASH_ERR: %s %s", os.path.basename(image_path), str(e)[:60])
            return None

    def is_duplicate_by_hash(
        self, img_hash: imagehash.ImageHash, threshold: int = DEFAULT_THRESHOLD
    ) -> tuple[bool, Optional[str]]:
        """
        检查 hash 是否已在库中（汉明距离 ≤ threshold）。
        返回 (is_duplicate, matched_path)。
        """
        hex_str = str(img_hash)
        with self._lock:
            # 精确匹配（快速路径）
            if hex_str in self.hash_to_paths:
                # 验证路径是否存在
                for p in self.hash_to_paths[hex_str]:
                    if os.path.isfile(p):
                        return True, p
                return True, None  # hash 匹配但文件已被删除

            # 模糊匹配（汉明距离 ≤ threshold）
            for stored_hex, paths in self.hash_to_paths.items():
                try:
                    dist = imagehash.hex_to_hash(stored_hex) - img_hash
                    if dist <= threshold:
                        existing = next((p for p in paths if os.path.isfile(p)), None)
                        return True, existing
                except Exception:
                    continue

        return False, None

    def record(self, file_path: str, img_hash: imagehash.ImageHash) -> bool:
        """
        记录一张图片到数据库和内存镜像。
        如果 file_path 已存在则更新（文件可能被覆盖）。
        """
        hex_str = str(img_hash)
        size = 0
        try:
            size = os.path.getsize(file_path)
        except OSError:
            pass

        with self._lock:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO images (file_path, hash_value, file_size, updated_at) "
                    "VALUES (?, ?, ?, datetime('now', 'localtime'))",
                    (file_path, hex_str, size),
                )
                self.conn.commit()

                # 更新内存镜像
                old_hash = self.path_to_hash.get(file_path)
                if old_hash and old_hash != hex_str:
                    # 路径的 hash 变了（文件被覆盖）
                    paths = self.hash_to_paths.get(old_hash, [])
                    if file_path in paths:
                        paths.remove(file_path)

                self.path_to_hash[file_path] = hex_str
                self.hash_to_paths.setdefault(hex_str, [])
                if file_path not in self.hash_to_paths[hex_str]:
                    self.hash_to_paths[hex_str].append(file_path)

                return True
            except Exception as e:
                log.warning("DEDUP_RECORD_ERR: %s %s", os.path.basename(file_path), str(e)[:80])
                return False

    def remove(self, file_path: str) -> bool:
        """从库中删除某条记录（文件已被删除时调用）。"""
        with self._lock:
            old_hash = self.path_to_hash.pop(file_path, None)
            if old_hash:
                paths = self.hash_to_paths.get(old_hash, [])
                if file_path in paths:
                    paths.remove(file_path)
                    if not paths:
                        del self.hash_to_paths[old_hash]
            try:
                self.conn.execute("DELETE FROM images WHERE file_path = ?", (file_path,))
                self.conn.commit()
                return True
            except Exception as e:
                log.warning("DEDUP_REMOVE_ERR: %s", str(e)[:80])
                return False

    # ── 全量扫描 ────────────────────────────────────────────

    def scan_full(self, base_dir: str, progress_callback=None) -> dict:
        """
        全量扫描目录，为所有图片建立去重记录。
        返回统计 dict：{scanned, compressed, new, duplicate, skipped, errors}
        """
        stats = {
            "scanned": 0,
            "compressed": 0,
            "new": 0,
            "duplicate": 0,
            "skipped": 0,
            "errors": 0,
            "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        if not os.path.isdir(base_dir):
            stats["errors"] += 1
            return stats

        # 收集所有支持的文件
        all_files = []
        for root, _dirs, fnames in os.walk(base_dir):
            # 跳过数据库文件自身
            if root == os.path.dirname(self.db_path):
                fnames = [f for f in fnames if os.path.join(root, f) != self.db_path]
            for fname in fnames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in SUPPORTED_EXT:
                    all_files.append(os.path.join(root, fname))

        stats["total_found"] = len(all_files)
        log.info("DEDUP_SCAN: found %d files in %s", len(all_files), base_dir)

        for idx, fpath in enumerate(all_files):
            if progress_callback:
                progress_callback(idx + 1, len(all_files), fpath)

            stats["scanned"] += 1

            # 已记录过的文件跳过
            with self._lock:
                already_recorded = fpath in self.path_to_hash
            if already_recorded:
                stats["skipped"] += 1
                continue

            try:
                ext = os.path.splitext(fpath)[1].lower()
                img_hash = None

                if ext == ".gif":
                    # GIF：直接读第一帧，不压缩
                    h = self.compute_hash(fpath)
                    if h is not None:
                        img_hash = h
                elif ext == ".mp4":
                    # MP4：跳过（后续可能增加视频指纹）
                    stats["skipped"] += 1
                    continue
                else:
                    # 图片：先压缩归一化，再算 hash
                    compressed = compress_image(fpath)
                    if compressed != fpath:
                        stats["compressed"] += 1
                    h = self.compute_hash(compressed)
                    if h is not None:
                        img_hash = h

                if img_hash is None:
                    stats["errors"] += 1
                    continue

                # 查重
                is_dup, _ = self.is_duplicate_by_hash(img_hash)
                if is_dup:
                    stats["duplicate"] += 1
                    continue

                # 新图片
                self.record(fpath, img_hash)
                stats["new"] += 1

            except Exception as e:
                stats["errors"] += 1
                log.warning("DEDUP_SCAN_FILE_ERR: %s %s", os.path.basename(fpath), str(e)[:80])

        stats["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(
            "DEDUP_SCAN_DONE: scanned=%d new=%d dup=%d skipped=%d errors=%d",
            stats["scanned"], stats["new"], stats["duplicate"], stats["skipped"], stats["errors"],
        )
        return stats

    # ── 查找重复（审计模式，不写 DB） ─────────────────────

    def find_duplicates(
        self, base_dir: str, threshold: int = DEFAULT_THRESHOLD, progress_callback=None,
        max_workers: int = None,
    ) -> dict:
        """
        全量扫描目录，压缩 + phash，用 union-find 聚类重复图片。
        使用 ThreadPoolExecutor 并发计算 hash（默认 workers = cpu_count × 2）。

        max_workers: 并发数，默认 min(32, cpu_count×2)。设为 1 可降级为单线程。

        返回 dict，含 stats 和 groups（每组第一张为最旧文件，默认保留）。
        """
        result = {
            "stats": {
                "scanned": 0,
                "compressed": 0,
                "errors": 0,
                "groups": 0,
                "duplicate_files": 0,
                "total_found": 0,
                "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "groups": [],
        }

        if not os.path.isdir(base_dir):
            return result

        # 1. 收集所有支持的文件 ──────────────────────────
        all_files = []
        for root, _dirs, fnames in os.walk(base_dir):
            if root == os.path.dirname(self.db_path):
                fnames = [f for f in fnames if os.path.join(root, f) != self.db_path]
            for fname in fnames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in SUPPORTED_EXT:
                    continue
                all_files.append(os.path.join(root, fname))

        result["stats"]["total_found"] = len(all_files)
        if not all_files:
            return result

        log.info("DEDUP_FIND: found %d files, computing hashes (%d workers)...",
                 len(all_files), max_workers or "auto")

        # 2. 并发计算 hash ─────────────────────────────────
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        if max_workers is None:
            max_workers = min(32, (os.cpu_count() or 4) * 2)

        hash_ints: list[tuple[str, int]] = []  # [(path, hash_int), ...]
        hash_lock = threading.Lock()
        progress_lock = threading.Lock()
        scanned = [0]
        compressed_count = [0]
        errors = [0]

        def _process_one(fpath: str):
            """处理单个文件：压缩 → hash → 返回 (path, hash_int) 或 None"""
            ext = os.path.splitext(fpath)[1].lower()
            try:
                if ext == ".gif":
                    h = self.compute_hash(fpath)
                    return (fpath, int(str(h), 16)) if h is not None else None
                elif ext == ".mp4":
                    return None  # 跳过视频
                else:
                    compressed = compress_image(fpath)
                    if compressed != fpath:
                        compressed_count[0] += 1
                    h = self.compute_hash(compressed)
                    return (fpath, int(str(h), 16)) if h is not None else None
            except Exception as e:
                log.warning("DEDUP_FIND_HASH_ERR: %s %s", os.path.basename(fpath), str(e)[:80])
                return None

        def _report_progress():
            """线程安全地更新进度"""
            with progress_lock:
                scanned[0] += 1
                cur = scanned[0]
            if progress_callback and cur % max(1, len(all_files) // 100) == 0:
                # 每 1% 上报一次，避免高频回调
                progress_callback(cur, len(all_files), "", "compute_hash")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_process_one, fpath): fpath for fpath in all_files}

            for future in as_completed(futures):
                _report_progress()
                try:
                    result_item = future.result()
                    if result_item is not None:
                        with hash_lock:
                            hash_ints.append(result_item)
                    else:
                        with hash_lock:
                            errors[0] += 1
                except Exception:
                    with hash_lock:
                        errors[0] += 1

        result["stats"]["scanned"] = scanned[0]
        result["stats"]["compressed"] = compressed_count[0]
        result["stats"]["errors"] = errors[0]

        if len(hash_ints) < 2:
            result["stats"]["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return result

        # 2. Union-Find 聚类 ──────────────────────────────
        log.info("DEDUP_FIND: clustering %d hashes (threshold=%d)...", len(hash_ints), threshold)

        n = len(hash_ints)
        parent = list(range(n))

        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(x, y):
            px, py = _find(x), _find(y)
            if px != py:
                parent[px] = py

        # 对不同扩展名的图片不做跨格式比较（GIF 只和 GIF 比）
        # 防止压缩过的 JPG phash 跳到不同位置
        # 按扩展名分组，同组内比较
        ext_groups: dict[str, list[int]] = {}
        for i, (fpath, _) in enumerate(hash_ints):
            ext = os.path.splitext(fpath)[1].lower()
            ext_groups.setdefault(ext, []).append(i)

        cluster_total = sum(len(v) for v in ext_groups.values())
        cluster_done = [0]  # 线程安全用 list 包裹
        for ext, indices in ext_groups.items():
            m = len(indices)
            for ai in range(m):
                cluster_done[0] += 1
                if progress_callback and cluster_done[0] % max(1, cluster_total // 50) == 0:
                    progress_callback(cluster_done[0], cluster_total, "", "cluster")
                hi = hash_ints[indices[ai]][1]
                for bj in range(ai + 1, m):
                    if (hi ^ hash_ints[indices[bj]][1]).bit_count() <= threshold:
                        _union(indices[ai], indices[bj])
                        break  # 一个匹配就够了，继续下一个

        # 3. 按根节点分组 ──────────────────────────────────
        root_map: dict[int, list[int]] = {}
        for i in range(n):
            root = _find(i)
            root_map.setdefault(root, []).append(i)

        # 4. 过滤有重复的组（>=2 文件），构建输出 ────────
        for indices in root_map.values():
            if len(indices) < 2:
                continue

            group_files = []
            for i in indices:
                fpath = hash_ints[i][0]
                try:
                    mtime = os.path.getmtime(fpath)
                    size = os.path.getsize(fpath)
                except OSError:
                    mtime = 0
                    size = 0
                group_files.append({
                    "path": fpath,
                    "fname": os.path.basename(fpath),
                    "dir": os.path.dirname(fpath),
                    "size": size,
                    "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "mtime_ts": mtime,
                })

            # 按修改时间排序（最旧排前）
            group_files.sort(key=lambda f: f["mtime_ts"])
            for f in group_files:
                f.pop("mtime_ts")

            result["stats"]["groups"] += 1
            result["stats"]["duplicate_files"] += len(group_files)
            result["groups"].append({
                "hash": hex(hash_ints[indices[0]][1]),
                "files": group_files,
                "count": len(group_files),
            })

        # 按组大小降序排列
        result["groups"].sort(key=lambda g: g["count"], reverse=True)

        result["stats"]["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(
            "DEDUP_FIND_DONE: scanned=%d groups=%d dup_files=%d errors=%d",
            result["stats"]["scanned"], result["stats"]["groups"],
            result["stats"]["duplicate_files"], result["stats"]["errors"],
        )
        return result

    @staticmethod
    def apply_deletion(groups_selection: list[dict]) -> dict:
        """
        执行用户审核后的删除操作。
        groups_selection: [
            {"files": [{"path": "...", "delete": true}, ...]},
            ...
        ]
        删除文件 + 更新去重库。返回执行结果。
        """
        stats = {"deleted": 0, "failed": 0, "kept": 0, "errors": []}

        for group in groups_selection:
            for f in group.get("files", []):
                path = f.get("path", "")
                if f.get("delete", False):
                    try:
                        os.remove(path)
                        stats["deleted"] += 1
                    except Exception as e:
                        stats["failed"] += 1
                        stats["errors"].append({"path": path, "error": str(e)[:80]})
                else:
                    stats["kept"] += 1

        return stats

    @staticmethod
    def record_remaining(groups_selection: list[dict], dedup) -> dict:
        """
        将用户保留的文件记录到去重库中。
        groups_selection: 同上格式；delete=false 的文件将被记录。
        返回 {recorded, errors}
        """
        stats = {"recorded": 0, "errors": 0, "error_details": []}

        for group in groups_selection:
            for f in group.get("files", []):
                path = f.get("path", "")
                if not f.get("delete", False):  # 保留的文件
                    try:
                        h = dedup.compute_hash(path)
                        if h is not None:
                            dedup.record(path, h)
                            stats["recorded"] += 1
                    except Exception as e:
                        stats["errors"] += 1
                        stats["error_details"].append({"path": path, "error": str(e)[:80]})

        return stats


    # ── Prune（清理失效记录） ──────────────────────────────

    def prune(self) -> dict:
        """
        扫描所有记录，清理文件已被删除的脏记录。
        返回 {removed, remaining}。
        """
        removed = 0
        stale = []

        with self._lock:
            cursor = self.conn.execute("SELECT id, file_path FROM images")
            for row_id, fpath in cursor:
                if not os.path.isfile(fpath):
                    stale.append(row_id)

            for row_id in stale:
                self.conn.execute("DELETE FROM images WHERE id = ?", (row_id,))
            self.conn.commit()

            removed = len(stale)
            if removed:
                self._load_memory()  # 重新加载内存镜像

        log.info("DEDUP_PRUNE: removed %d stale records", removed)
        return {
            "removed": removed,
            "remaining": len(self.path_to_hash),
        }

    # ── 热加载 ────────────────────────────────────────────

    def reload(self):
        """
        重新从 SQLite 加载所有记录到内存镜像。
        其他进程（如 config_web）可能已更新 DB，调用此方法同步。
        """
        with self._lock:
            self._load_memory()
        log.info("DEDUP_RELOAD: reloaded %d records", len(self.path_to_hash))

    # ── 状态查询 ────────────────────────────────────────────

    def get_status(self) -> dict:
        """返回去重数据库状态。"""
        return {
            "db_path": self.db_path,
            "db_exists": os.path.isfile(self.db_path),
            "total_records": len(self.path_to_hash),
            "unique_hashes": len(self.hash_to_paths),
            "fwd_dir": DEFAULT_FWD_DIR,
            "fwd_dir_exists": os.path.isdir(DEFAULT_FWD_DIR),
        }

    # ── 生命周期 ────────────────────────────────────────────

    def close(self):
        """关闭数据库连接。"""
        try:
            self.conn.close()
        except Exception:
            pass
