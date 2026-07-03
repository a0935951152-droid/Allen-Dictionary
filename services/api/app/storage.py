"""JSON-backed 節點儲存（§6 併發優先）。

每個 batch 一個檔、字典一個檔 → 不同 batch 天生可並行（檔案層隔離）。
同一 batch 內以 per-batch 鎖做樂觀並發，不鎖整個系統。
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Optional

from fastapi import HTTPException

from .config import settings
from .schemas import BatchNode, Classification, SpanNode, TermEntry

_SNAPSHOT_CAP = 10   # 每批最多保留幾份輪前快照（§3 退回）
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class Store:
    def __init__(self, root: str):
        self.batches_dir = os.path.join(root, "batches")
        correct_dir = os.path.join(root, "correct")           # 糾正側落地：活字典 + 分類預設
        self.dict_path = os.path.join(correct_dir, "dictionary.json")
        self.cls_path = os.path.join(correct_dir, "classifications.json")
        os.makedirs(self.batches_dir, exist_ok=True)
        os.makedirs(correct_dir, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._dict_lock = threading.Lock()
        self._cls_lock = threading.Lock()

    # ── 鎖（節點層級）─────────────────────────────────────────
    def lock(self, batch_id: str) -> threading.Lock:
        with self._locks_guard:
            if batch_id not in self._locks:
                self._locks[batch_id] = threading.Lock()
            return self._locks[batch_id]

    # ── 原子寫入 ──────────────────────────────────────────────
    @staticmethod
    def _atomic_write(path: str, payload: dict) -> None:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ── Batch ─────────────────────────────────────────────────
    def _batch_path(self, batch_id: str) -> str:
        if not _BATCH_ID_RE.match(batch_id):
            raise HTTPException(400, "batch_id 格式不合法（僅允許英數字/._-，長度1-80）")
        return os.path.join(self.batches_dir, f"{batch_id}.json")

    def list_batches(self) -> list[str]:
        return sorted(
            f[:-5] for f in os.listdir(self.batches_dir) if f.endswith(".json")
        )

    def get_batch(self, batch_id: str) -> Optional[BatchNode]:
        path = self._batch_path(batch_id)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return BatchNode.model_validate(json.load(f))

    def save_batch(self, batch: BatchNode) -> None:
        self._atomic_write(
            self._batch_path(batch.batch_id),
            batch.model_dump(by_alias=True),
        )

    # ── Dictionary ────────────────────────────────────────────
    def load_dict(self) -> dict[str, TermEntry]:
        if not os.path.exists(self.dict_path):
            return {}
        with open(self.dict_path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: TermEntry.model_validate(v) for k, v in raw.items()}

    def save_dict(self, terms: dict[str, TermEntry]) -> None:
        from .engine.variants import to_tw
        with self._dict_lock:
            for v in terms.values():            # 輸出合規：正解一律台灣字面（簡→繁＋大陸用語→台灣用語）
                v.correct = to_tw(v.correct)
            self._atomic_write(
                self.dict_path,
                {k: v.model_dump(by_alias=True) for k, v in terms.items()},
            )

    # ── Classifications（分類＝具名 LLM 提示詞，§3）─────────────
    def load_classifications(self) -> dict[str, Classification]:
        if not os.path.exists(self.cls_path):
            return {}
        with open(self.cls_path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: Classification.model_validate(v) for k, v in raw.items()}

    def save_classifications(self, items: dict[str, Classification]) -> None:
        with self._cls_lock:
            self._atomic_write(
                self.cls_path,
                {k: v.model_dump() for k, v in items.items()},
            )

    # ── 快照 / 退回（§3 循環退回）─────────────────────────────
    @staticmethod
    def snapshot_spans(batch: BatchNode) -> None:
        """跑輪前把當前 spans 深拷貝入 batch.snapshots（上限 _SNAPSHOT_CAP）。"""
        snap = [s.model_dump(by_alias=True) for s in batch.spans]
        batch.snapshots.append(snap)
        if len(batch.snapshots) > _SNAPSHOT_CAP:
            batch.snapshots = batch.snapshots[-_SNAPSHOT_CAP:]

    @staticmethod
    def restore_last(batch: BatchNode) -> bool:
        """退回上一快照；成功回 True，無快照可退回 False。"""
        if not batch.snapshots:
            return False
        snap = batch.snapshots.pop()
        batch.spans = [SpanNode.model_validate(s) for s in snap]
        return True


store = Store(settings.data_dir)
