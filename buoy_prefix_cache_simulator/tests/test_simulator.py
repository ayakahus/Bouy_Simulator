import unittest
from buoy_sim.trace import Request, prefix_chunks
from buoy_sim.policies import BuoyPolicy, LRUPolicy, OPTPolicy, S3FIFOPolicy
from buoy_sim.simulator import run_policy

class SimulatorTests(unittest.TestCase):
    def test_prefix_chunk_ids_share_only_identical_prefixes(self):
        a = prefix_chunks(["sys","a","b","c"], 2)
        b = prefix_chunks(["sys","a","b","d"], 2)
        self.assertEqual(a[0][0], b[0][0])
        self.assertNotEqual(a[1][0], b[1][0])

    def test_repeated_request_gets_hits(self):
        reqs = [
            Request(0, 1, "a", ["sys","hello","world","!"]),
            Request(1, 2, "b", ["sys","hello","world","!"]),
        ]
        policy = BuoyPolicy(4, 8, 32, initial_depth_threshold=4)
        summary, rows = run_policy(reqs, policy, 2)
        self.assertGreater(rows[1]["hit_chunks"], 0)
        self.assertGreater(summary["chunk_hit_rate"], 0)

    def test_lru_capacity_is_bounded(self):
        p = LRUPolicy(1, 1)
        p.access_request(prefix_chunks(["a","b","c","d"], 1))
        self.assertLessEqual(len(p.hbm) + len(p.dram), 2)

    def test_lru_demotes_hbm_overflow_to_dram(self):
        # HBM holds 1 chunk, DRAM holds 1 chunk: "a" overflows HBM into DRAM.
        p = LRUPolicy(1, 1)
        p.access_request(prefix_chunks(["a", "b"], 1))
        self.assertEqual(len(p.dram), 1)
        self.assertGreater(p.stats["demotions"], 0)
        # A demoted chunk stays cached: re-accessing it hits in DRAM.
        result = p.access_request(prefix_chunks(["a", "c"], 1))
        self.assertEqual(result["dram_hits"], 1)
        self.assertEqual(result["hbm_hits"], 0)

    def test_s3fifo_capacity_is_bounded(self):
        p = S3FIFOPolicy(2, 3)
        for tok in ["a", "b", "c", "d", "e", "f", "g"]:
            p.access_request(prefix_chunks([tok], 1))
        self.assertLessEqual(len(p.hbm), 2)
        self.assertLessEqual(len(p.dram), 3)

    def test_s3fifo_promotes_reaccessed_hbm_chunk_to_dram(self):
        # A chunk re-accessed while queued in HBM is promoted to DRAM (not
        # evicted) when HBM overflows, and then hits in DRAM.
        p = S3FIFOPolicy(1, 2)
        c1 = prefix_chunks(["x"], 1)[0]
        c2 = prefix_chunks(["y"], 1)[0]
        p.access_request([c1])   # miss -> HBM (small queue), freq = 0
        p.access_request([c1])   # HBM hit -> freq bump, no movement
        p.access_request([c2])   # HBM overflow -> re-accessed c1 promoted to DRAM
        self.assertEqual(p.stats["demotions"], 1)
        self.assertEqual(len(p.hbm), 1)
        self.assertEqual(len(p.dram), 1)
        result = p.access_request([c1])
        self.assertEqual(result["dram_hits"], 1)
        self.assertEqual(result["hbm_hits"], 0)

    def test_s3fifo_evicts_cold_chunk_and_ghost_hit_skips_to_dram(self):
        # A never-re-accessed chunk evicted from HBM enters the ghost; its next
        # request is a ghost hit admitted straight to DRAM, skipping the trial.
        p = S3FIFOPolicy(1, 2, ghost_capacity=8)
        c1 = prefix_chunks(["x"], 1)[0]
        c2 = prefix_chunks(["y"], 1)[0]
        p.access_request([c1])   # c1 enters HBM, never re-accessed
        p.access_request([c2])   # HBM overflow -> cold c1 evicted into ghost
        self.assertEqual(p.stats["evictions"], 1)
        self.assertIn(c1[0], p.ghost_set)
        result = p.access_request([c1])  # ghost hit -> straight to DRAM
        self.assertEqual(result["misses"], 1)
        self.assertEqual(p.stats["ghost_hits"], 1)
        self.assertIn(c1[0], p.dram)
        self.assertNotIn(c1[0], p.hbm)
        result = p.access_request([c1])  # stays cached in DRAM
        self.assertEqual(result["dram_hits"], 1)

    def test_s3fifo_second_chance_on_dram_overflow(self):
        # DRAM eviction is 2-bit Clock: a re-accessed chunk first gets its freq
        # decremented (second chance) before it is finally evicted.
        p = S3FIFOPolicy(1, 1, ghost_capacity=8)
        c1 = prefix_chunks(["x"], 1)[0]
        c2 = prefix_chunks(["y"], 1)[0]
        c3 = prefix_chunks(["z"], 1)[0]
        c4 = prefix_chunks(["w"], 1)[0]
        p.access_request([c1])           # HBM
        p.access_request([c1])           # HBM hit
        p.access_request([c2])           # promote c1 -> DRAM; c2 stays in HBM
        p.access_request([c1])           # DRAM hit -> freq 1
        p.access_request([c2])           # HBM hit
        p.access_request([c3])           # promote c2 -> DRAM overflow -> c1 second chance
        self.assertGreater(p.stats["second_chances"], 0)
        self.assertIn(c1[0], p.dram)     # c1 survived via its second chance
        p.access_request([c4])           # HBM overflow -> cold c3 evicted
        result = p.access_request([c1])  # c1 still cached in DRAM
        self.assertEqual(result["dram_hits"], 1)

    def test_opt_known_sequence(self):
        # Belady MIN on "A B A C B" with capacity 2: M M H M H
        # (evict A at "C" because A is never used again).
        p = OPTPolicy(2, 0)
        cids = [prefix_chunks([t], 1)[0] for t in ["a", "b", "a", "c", "b"]]
        p.prepare([[c] for c in cids])
        hits = [p.access_request([c])["hit_chunks"] for c in cids]
        self.assertEqual(hits, [0, 0, 1, 0, 1])

    def test_opt_dominates_lru(self):
        # MIN is optimal for the total capacity: its hit count must never be
        # below any online policy's on the same sequence and capacity.
        import random
        rng = random.Random(7)
        cids = [prefix_chunks([t], 1)[0] for t in (rng.choice("abcdefghij") for _ in range(400))]
        opt = OPTPolicy(4, 0)
        opt.prepare([[c] for c in cids])
        opt_hits = sum(opt.access_request([c])["hit_chunks"] for c in cids)
        lru = LRUPolicy(4, 0)
        lru_hits = sum(lru.access_request([c])["hit_chunks"] for c in cids)
        self.assertGreaterEqual(opt_hits, lru_hits)
        self.assertLessEqual(len(opt.hbm) + len(opt.dram), 4)

    def test_opt_pulls_soon_needed_chunk_into_hbm(self):
        # hbm=1, dram=2, sequence a b c b a a. After the first overflows "a"
        # sits in DRAM; at its i4 DRAM hit its next use (position 5) is sooner
        # than the HBM worst (b, INF), so the oracle swaps it into HBM (one
        # demotion) and i5 hits in HBM.
        p = OPTPolicy(1, 2)
        cids = [prefix_chunks([t], 1)[0] for t in ["a", "b", "c", "b", "a", "a"]]
        p.prepare([[c] for c in cids])
        results = [p.access_request([c]) for c in cids]
        self.assertEqual([r["hit_chunks"] for r in results], [0, 0, 0, 1, 1, 1])
        self.assertEqual(results[4]["dram_hits"], 1)
        self.assertEqual(results[5]["hbm_hits"], 1)
        self.assertEqual(p.stats["demotions"], 3)
        self.assertEqual(len(p.hbm), 1)
        self.assertEqual(len(p.dram), 2)
        self.assertIn(cids[4][0], p.hbm)   # a pulled into HBM by the oracle
        self.assertIn(cids[1][0], p.dram)  # b (stale next use) sunk to DRAM

if __name__ == "__main__":
    unittest.main()
