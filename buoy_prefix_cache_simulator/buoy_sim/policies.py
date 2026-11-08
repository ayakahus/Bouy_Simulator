"""Cache replacement policies: Buoy, LRU, and S3-FIFO."""
from __future__ import annotations
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Optional

@dataclass
class Chunk:
    chunk_id: str
    depth: int
    token_count: int
    visited: bool = False
    freq: int = 0  # S3-FIFO frequency counter (unused by Buoy/LRU)

class BuoyPolicy:
    name = "buoy"

    def __init__(self, hbm_capacity: int, dram_capacity: int, ghost_capacity: int = 4096,
                 initial_depth_threshold: int = 1, admission: bool = True):
        if hbm_capacity < 0 or dram_capacity < 0 or ghost_capacity < 0:
            raise ValueError("capacities must be >= 0")
        self.hcap, self.mcap, self.gcap = hbm_capacity, dram_capacity, ghost_capacity
        self.initial_threshold = max(1, initial_depth_threshold)
        self.admission = admission
        # OrderedDict order: oldest/tail at beginning, newest/head at end.
        self.hbm: OrderedDict[str, Chunk] = OrderedDict()
        self.dram: OrderedDict[str, Chunk] = OrderedDict()
        self.ghost: deque[str] = deque()
        self.ghost_set: set[str] = set()
        self.reuse_depth_sum = 0
        self.reuse_events = 0
        self.stats = self._new_stats()

    @staticmethod
    def _new_stats():
        return dict(total_chunks=0, hbm_hits=0, dram_hits=0, misses=0,
                    admitted_chunks=0, rejected_chunks=0, evictions=0,
                    demotions=0, ghost_hits=0, second_chances=0)

    @property
    def depth_threshold(self) -> int:
        if self.reuse_events == 0:
            return self.initial_threshold
        return max(1, int(self.reuse_depth_sum / self.reuse_events))

    def _ghost_contains(self, cid: str) -> bool:
        return cid in self.ghost_set

    def _ghost_add(self, cid: str):
        if self.gcap <= 0:
            return
        if cid in self.ghost_set:
            # FIFO semantics: refresh by removing and appending, keeping one copy.
            try:
                self.ghost.remove(cid)
            except ValueError:
                pass
        self.ghost.append(cid)
        self.ghost_set.add(cid)
        while len(self.ghost) > self.gcap:
            old = self.ghost.popleft()
            self.ghost_set.discard(old)

    def _ghost_remove(self, cid: str):
        if cid in self.ghost_set:
            self.ghost_set.discard(cid)
            try:
                self.ghost.remove(cid)
            except ValueError:
                pass

    def _insert_head(self, queue: OrderedDict[str, Chunk], node: Chunk):
        queue[node.chunk_id] = node
        queue.move_to_end(node.chunk_id, last=True)

    def _evict_hbm(self):
        if not self.hbm:
            return
        cid, node = self.hbm.popitem(last=False)
        self._insert_head(self.dram, node)
        self.stats["demotions"] += 1
        self._trim_dram()

    def _trim_dram(self):
        # Capacity is a maximum resident count. Visited entries receive a second chance.
        if self.mcap == 0:
            while self.dram:
                cid, node = self.dram.popitem(last=False)
                if node.visited:
                    node.visited = False
                    self.stats["second_chances"] += 1
                    self._ghost_add(cid)
                else:
                    self._ghost_add(cid)
                    self.stats["evictions"] += 1
            return
        guard = 0
        while len(self.dram) > self.mcap:
            guard += 1
            if guard > max(10, 4 * (len(self.dram) + 1)):
                # Defensive guard for pathological zero/equal-capacity configurations.
                cid, node = self.dram.popitem(last=False)
                self._ghost_add(cid)
                self.stats["evictions"] += 1
                continue
            cid, node = self.dram.popitem(last=False)
            if node.visited:
                node.visited = False
                self.stats["second_chances"] += 1
                self._insert_head(self.dram, node)
            else:
                self._ghost_add(cid)
                self.stats["evictions"] += 1

    def _trim_hbm(self):
        while len(self.hbm) > self.hcap:
            self._evict_hbm()

    def access_request(self, chunks: list[tuple[str, int, int]]) -> dict:
        """Process one request's prefix chunks in root-to-leaf order."""
        self.stats["total_chunks"] += len(chunks)
        hbm_hits = dram_hits = misses = hits_tokens = 0
        # The paper temporarily removes accessed nodes and reinserts in reverse path order.
        targets: list[tuple[Chunk, str]] = []
        for cid, depth, token_count in chunks:
            if cid in self.hbm:
                node = self.hbm.pop(cid)
                node.depth, node.token_count = depth, token_count
                node.visited = True
                targets.append((node, "hbm"))
                hbm_hits += 1
                hits_tokens += token_count
                self.reuse_depth_sum += depth
                self.reuse_events += 1
            elif cid in self.dram:
                node = self.dram.pop(cid)
                node.depth, node.token_count = depth, token_count
                node.visited = True
                targets.append((node, "hbm"))
                dram_hits += 1
                hits_tokens += token_count
                self.reuse_depth_sum += depth
                self.reuse_events += 1
            else:
                misses += 1
                self.stats["misses"] += 1
                in_ghost = self._ghost_contains(cid)
                if in_ghost:
                    self.stats["ghost_hits"] += 1
                    self._ghost_remove(cid)
                node = Chunk(cid, depth, token_count, visited=in_ghost)
                threshold = self.depth_threshold
                if self.admission and depth > threshold and not in_ghost:
                    self.stats["rejected_chunks"] += 1
                    self._ghost_add(cid)
                    continue
                self.stats["admitted_chunks"] += 1
                targets.append((node, "dram"))
        # Reverse insertion preserves ancestor-to-descendant order with deeper nodes nearer tail.
        for node, tier in reversed(targets):
            if tier == "hbm" and self.hcap > 0:
                self._insert_head(self.hbm, node)
            elif self.mcap > 0:
                self._insert_head(self.dram, node)
            else:
                self._ghost_add(node.chunk_id)
                self.stats["evictions"] += 1
        self._trim_hbm()
        self._trim_dram()
        self.stats["hbm_hits"] += hbm_hits
        self.stats["dram_hits"] += dram_hits
        self.stats["misses"] += 0  # misses incremented per chunk above
        return dict(hbm_hits=hbm_hits, dram_hits=dram_hits, misses=misses,
                    hit_chunks=hbm_hits + dram_hits, hit_tokens=hits_tokens,
                    request_tokens=sum(c[2] for c in chunks))

    def snapshot(self):
        return {
            "policy": self.name,
            "hbm_oldest_to_newest": [{"chunk_id": c, "depth": n.depth, "visited": n.visited} for c,n in self.hbm.items()],
            "dram_oldest_to_newest": [{"chunk_id": c, "depth": n.depth, "visited": n.visited} for c,n in self.dram.items()],
            "ghost_oldest_to_newest": list(self.ghost),
            "depth_threshold": self.depth_threshold,
            "reuse_events": self.reuse_events,
            "stats": self.stats,
        }

class LRUPolicy:
    """Two-tier LRU baseline.

    HBM and DRAM are separate LRU queues with capacities ``hbm_capacity`` and
    ``dram_capacity`` chunks (total resident capacity is unchanged). Request
    handling mirrors Buoy's tier movement so comparisons isolate Buoy's
    admission/ghost/second-chance machinery:

    - hit in HBM: move to the HBM MRU position;
    - hit in DRAM: promote to the HBM MRU position (Buoy also moves every hit
      to HBM);
    - miss: insert at the HBM MRU position; the HBM LRU tail is *demoted to
      DRAM* (not discarded) when HBM overflows, and the DRAM LRU tail is
      evicted when DRAM overflows.

    Promotion-on-hit plus demotion-on-overflow keeps the two queues in strict
    global-recency order, so hit/miss decisions match a single-tier LRU of
    capacity ``hbm + dram``; only tier accounting (hbm/dram hits, demotions,
    evictions) differs.
    """

    name = "lru"

    def __init__(self, hbm_capacity: int, dram_capacity: int = 0, ghost_capacity: int = 0, **kwargs):
        if hbm_capacity < 0 or dram_capacity < 0:
            raise ValueError("capacities must be >= 0")
        self.hcap = hbm_capacity
        self.mcap = dram_capacity
        # OrderedDict order: oldest/LRU at the beginning, newest/MRU at the end.
        self.hbm: OrderedDict[str, Chunk] = OrderedDict()
        self.dram: OrderedDict[str, Chunk] = OrderedDict()
        self.stats = dict(total_chunks=0, hbm_hits=0, dram_hits=0, misses=0,
                          admitted_chunks=0, rejected_chunks=0, evictions=0,
                          demotions=0, ghost_hits=0, second_chances=0)

    def _demote_hbm_overflow(self):
        """Move HBM LRU-tail chunks into DRAM instead of discarding them."""
        while len(self.hbm) > self.hcap:
            cid, node = self.hbm.popitem(last=False)
            if self.mcap > 0:
                self.dram[cid] = node
                self.stats["demotions"] += 1
                self._trim_dram()
            else:
                self.stats["evictions"] += 1

    def _trim_dram(self):
        while len(self.dram) > self.mcap:
            self.dram.popitem(last=False)
            self.stats["evictions"] += 1

    def access_request(self, chunks: list[tuple[str, int, int]]) -> dict:
        hbm_hits = dram_hits = misses = hits_tokens = 0
        self.stats["total_chunks"] += len(chunks)
        for cid, depth, token_count in chunks:
            if cid in self.hbm:
                node = self.hbm.pop(cid)
                node.depth, node.token_count = depth, token_count
                self.hbm[cid] = node  # re-append at the MRU end
                hbm_hits += 1
                hits_tokens += token_count
            elif cid in self.dram:
                node = self.dram.pop(cid)
                node.depth, node.token_count = depth, token_count
                self.hbm[cid] = node  # promote to HBM MRU
                dram_hits += 1
                hits_tokens += token_count
                self._demote_hbm_overflow()
            else:
                misses += 1
                self.stats["misses"] += 1
                if self.hcap > 0:
                    self.hbm[cid] = Chunk(cid, depth, token_count)
                    self.stats["admitted_chunks"] += 1
                    self._demote_hbm_overflow()
                elif self.mcap > 0:
                    self.dram[cid] = Chunk(cid, depth, token_count)
                    self.stats["admitted_chunks"] += 1
                    self._trim_dram()
        self.stats["hbm_hits"] += hbm_hits
        self.stats["dram_hits"] += dram_hits
        return dict(hbm_hits=hbm_hits, dram_hits=dram_hits, misses=misses,
                    hit_chunks=hbm_hits + dram_hits, hit_tokens=hits_tokens,
                    request_tokens=sum(c[2] for c in chunks))

    def snapshot(self):
        return {
            "policy": self.name,
            "hbm_oldest_to_newest": [{"chunk_id": c, "depth": n.depth} for c, n in self.hbm.items()],
            "dram_oldest_to_newest": [{"chunk_id": c, "depth": n.depth} for c, n in self.dram.items()],
            "stats": self.stats,
        }

class S3FIFOPolicy:
    """S3-FIFO (Yang et al., "FIFO Queues Are All You Need for Cache Eviction",
    SOSP 2023) with HBM as the small FIFO and DRAM as the main FIFO.

    The paper's two-queue design maps onto the storage tiers as follows:

    - **HBM = small FIFO (S)**: every admitted chunk enters HBM with
      ``freq = 0``. When HBM overflows, the queue head is examined: a chunk
      re-accessed while queued (``freq >= move_to_main_threshold``) moves to
      the DRAM queue tail with freq reset; a never-re-accessed chunk is
      evicted and its ID recorded in the ghost FIFO.
    - **DRAM = main FIFO (M)**: holds chunks that proved popular in HBM.
      Overflow is resolved with a 2-bit Clock: a chunk with ``freq > 0`` is
      reinserted at the tail with ``freq -= 1`` (second chance); a chunk with
      ``freq == 0`` is evicted.
    - **Ghost FIFO** (IDs only, no chunk data): a miss whose ID is in the
      ghost — i.e. the chunk was evicted from HBM and requested again —
      bypasses the HBM trial queue and is admitted straight to DRAM.

    Unlike LRU, hits never move a chunk between tiers or within a queue; they
    only bump a small frequency counter. Per-hit work is O(1), HBM writes are
    limited to fresh admissions (no promote-on-hit), and one-shot chunks are
    evicted FIFO-fast instead of polluting the hot tier.
    """

    name = "s3fifo"

    def __init__(self, hbm_capacity: int, dram_capacity: int, ghost_capacity: int = 4096,
                 move_to_main_threshold: int = 1, **kwargs):
        if hbm_capacity < 0 or dram_capacity < 0 or ghost_capacity < 0:
            raise ValueError("capacities must be >= 0")
        self.hcap, self.mcap, self.gcap = hbm_capacity, dram_capacity, ghost_capacity
        # Paper default: one re-access inside the small queue promotes a chunk
        # to the main queue ("accessed at least twice" counting admission).
        self.move_to_main_threshold = max(1, move_to_main_threshold)
        # OrderedDict order: oldest/tail at the beginning, newest/head at the end.
        self.hbm: OrderedDict[str, Chunk] = OrderedDict()
        self.dram: OrderedDict[str, Chunk] = OrderedDict()
        self.ghost: deque[str] = deque()
        self.ghost_set: set[str] = set()
        self.stats = dict(total_chunks=0, hbm_hits=0, dram_hits=0, misses=0,
                          admitted_chunks=0, rejected_chunks=0, evictions=0,
                          demotions=0, ghost_hits=0, second_chances=0)

    # ---- ghost FIFO (IDs only) -------------------------------------------------
    def _ghost_contains(self, cid: str) -> bool:
        return cid in self.ghost_set

    def _ghost_add(self, cid: str):
        if self.gcap <= 0:
            return
        if cid in self.ghost_set:
            # FIFO semantics: refresh by removing and appending, keeping one copy.
            try:
                self.ghost.remove(cid)
            except ValueError:
                pass
        self.ghost.append(cid)
        self.ghost_set.add(cid)
        while len(self.ghost) > self.gcap:
            old = self.ghost.popleft()
            self.ghost_set.discard(old)

    def _ghost_remove(self, cid: str):
        if cid in self.ghost_set:
            self.ghost_set.discard(cid)
            try:
                self.ghost.remove(cid)
            except ValueError:
                pass

    # ---- queue helpers ----------------------------------------------------------
    def _insert_head(self, queue: OrderedDict[str, Chunk], node: Chunk):
        queue[node.chunk_id] = node
        queue.move_to_end(node.chunk_id, last=True)

    def _trim_dram(self):
        """Resolve DRAM (main queue) overflow with 2-bit Clock second chances.

        Per the paper, main-queue evictions do NOT enter the ghost: only
        small-queue evictions do. The freq decrement guarantees termination;
        the guard mirrors BuoyPolicy's defense against pathological configs.
        """
        guard = 0
        limit = max(10, 4 * (len(self.dram) + 1))
        while self.dram and len(self.dram) > self.mcap:
            guard += 1
            cid, node = self.dram.popitem(last=False)
            if node.freq >= 1 and guard <= limit:
                node.freq -= 1
                self.stats["second_chances"] += 1
                self._insert_head(self.dram, node)
            else:
                self.stats["evictions"] += 1

    def _trim_hbm(self):
        """Resolve HBM (small queue) overflow: promote hot heads, evict cold ones.

        Re-accessed chunks move to the DRAM tail with freq reset (paper
        semantics); never-re-accessed chunks are evicted and recorded in the
        ghost FIFO so a prompt re-request can skip the HBM trial queue. With
        ``dram_capacity == 0`` there is no main queue, so promotions degrade
        to plain evictions.
        """
        while len(self.hbm) > self.hcap:
            cid, node = self.hbm.popitem(last=False)
            if node.freq >= self.move_to_main_threshold and self.mcap > 0:
                node.freq = 0
                self._insert_head(self.dram, node)
                self.stats["demotions"] += 1
                self._trim_dram()
            else:
                self._ghost_add(cid)
                self.stats["evictions"] += 1

    def access_request(self, chunks: list[tuple[str, int, int]]) -> dict:
        """Process one request's prefix chunks in root-to-leaf order."""
        self.stats["total_chunks"] += len(chunks)
        hbm_hits = dram_hits = misses = hits_tokens = 0
        for cid, depth, token_count in chunks:
            node = self.hbm.get(cid)
            if node is not None:
                # Hit in the small queue: bump the frequency bit, move nothing.
                node.depth, node.token_count = depth, token_count
                node.freq = min(node.freq + 1, self.move_to_main_threshold)
                hbm_hits += 1
                hits_tokens += token_count
                continue
            node = self.dram.get(cid)
            if node is not None:
                # Hit in the main queue: 2-bit clock counter, move nothing.
                node.depth, node.token_count = depth, token_count
                node.freq = min(node.freq + 1, 3)
                dram_hits += 1
                hits_tokens += token_count
                continue
            misses += 1
            self.stats["misses"] += 1
            in_ghost = self._ghost_contains(cid)
            if in_ghost:
                self.stats["ghost_hits"] += 1
                self._ghost_remove(cid)
            if self.mcap > 0 and (in_ghost or self.hcap == 0):
                # Ghost members skip the HBM trial queue (paper admission rule);
                # with no HBM configured, every chunk enters DRAM directly.
                self.stats["admitted_chunks"] += 1
                self._insert_head(self.dram, Chunk(cid, depth, token_count))
                self._trim_dram()
            elif self.hcap > 0:
                self.stats["admitted_chunks"] += 1
                self._insert_head(self.hbm, Chunk(cid, depth, token_count))
                self._trim_hbm()
            # else: no capacity configured anywhere; count as a miss only.
        self.stats["hbm_hits"] += hbm_hits
        self.stats["dram_hits"] += dram_hits
        return dict(hbm_hits=hbm_hits, dram_hits=dram_hits, misses=misses,
                    hit_chunks=hbm_hits + dram_hits, hit_tokens=hits_tokens,
                    request_tokens=sum(c[2] for c in chunks))

    def snapshot(self):
        return {
            "policy": self.name,
            "hbm_oldest_to_newest": [{"chunk_id": c, "depth": n.depth, "freq": n.freq} for c, n in self.hbm.items()],
            "dram_oldest_to_newest": [{"chunk_id": c, "depth": n.depth, "freq": n.freq} for c, n in self.dram.items()],
            "ghost_oldest_to_newest": list(self.ghost),
            "move_to_main_threshold": self.move_to_main_threshold,
            "stats": self.stats,
        }
