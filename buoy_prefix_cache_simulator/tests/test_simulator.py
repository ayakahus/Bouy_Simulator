import unittest
from buoy_sim.trace import Request, prefix_chunks
from buoy_sim.policies import BuoyPolicy, LRUPolicy
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

if __name__ == "__main__":
    unittest.main()
