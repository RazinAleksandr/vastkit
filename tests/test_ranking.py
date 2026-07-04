import unittest

from vastkit.models import Offer
from vastkit.ranking import rank_offers


def offer(oid, dph, bw_per_tb=0.0, dlperf=100.0):
    return Offer.from_raw({
        "id": oid, "gpu_name": "L40S", "dph_total": dph,
        "internet_down_cost_per_tb": bw_per_tb, "dlperf": dlperf,
    })


class TestRanking(unittest.TestCase):
    def test_effective_penalizes_expensive_bandwidth(self):
        # 0.50/hr with $20/TB bandwidth vs 0.60/hr with free bandwidth.
        # Downloading 50GB costs $1.00 on the first host -> second wins.
        cheap_rent_pricey_bw = offer(1, 0.50, bw_per_tb=20.0)
        pricier_rent_free_bw = offer(2, 0.60, bw_per_tb=0.0)
        ranked = rank_offers([cheap_rent_pricey_bw, pricier_rent_free_bw],
                             sort="effective", hours=1.0, download_gb=50.0)
        self.assertEqual([o.id for o in ranked], [2, 1])

    def test_price_ignores_bandwidth(self):
        a, b = offer(1, 0.50, bw_per_tb=20.0), offer(2, 0.60)
        ranked = rank_offers([a, b], sort="price")
        self.assertEqual([o.id for o in ranked], [1, 2])

    def test_speed(self):
        slow, fast = offer(1, 0.3, dlperf=50), offer(2, 0.9, dlperf=200)
        ranked = rank_offers([slow, fast], sort="speed")
        self.assertEqual(ranked[0].id, 2)

    def test_value_prefers_perf_per_dollar(self):
        # 100 dlperf / $1.0 session  vs  260 dlperf / $2.0 session
        a = offer(1, 1.0, dlperf=100)
        b = offer(2, 2.0, dlperf=260)
        ranked = rank_offers([a, b], sort="value", hours=1.0, download_gb=0.0)
        self.assertEqual(ranked[0].id, 2)

    def test_unknown_sort(self):
        with self.assertRaises(ValueError):
            rank_offers([], sort="bogus")

    def test_session_cost_math(self):
        o = offer(1, 0.5, bw_per_tb=10.0)  # $0.01/GB
        self.assertAlmostEqual(o.session_cost(hours=2, download_gb=100), 2.0)


if __name__ == "__main__":
    unittest.main()
