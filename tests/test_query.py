import unittest

from vastkit.query import (
    EU_COUNTRIES,
    build_query,
    expand_geolocation,
    match_geolocation,
    parse_filter,
)


class TestParseFilter(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(parse_filter("cuda_max_good=gte:12.4"),
                         {"cuda_max_good": {"gte": 12.4}})
        self.assertEqual(parse_filter("num_gpus=eq:2"), {"num_gpus": {"eq": 2}})

    def test_bool(self):
        self.assertEqual(parse_filter("datacenter=eq:true"), {"datacenter": {"eq": True}})
        self.assertEqual(parse_filter("external=eq:false"), {"external": {"eq": False}})

    def test_list(self):
        self.assertEqual(parse_filter("gpu_name=in:L40S,A40"),
                         {"gpu_name": {"in": ["L40S", "A40"]}})

    def test_string(self):
        self.assertEqual(parse_filter("gpu_name=eq:RTX 6000Ada"),
                         {"gpu_name": {"eq": "RTX 6000Ada"}})

    def test_invalid(self):
        for bad in ("nonsense", "a=b", "x=weird:1"):
            with self.assertRaises(ValueError):
                parse_filter(bad)


class TestBuildQuery(unittest.TestCase):
    def test_defaults_always_present(self):
        q = build_query()
        self.assertEqual(q["verified"], {"eq": True})
        self.assertEqual(q["rentable"], {"eq": True})
        self.assertEqual(q["type"], "on-demand")
        self.assertEqual(q["order"], [["dph_total", "asc"]])
        self.assertEqual(q["num_gpus"], {"eq": 1})

    def test_single_vs_multi_gpu_names(self):
        self.assertEqual(build_query(gpus=["L40S"])["gpu_name"], {"eq": "L40S"})
        self.assertEqual(build_query(gpus=["L40S", "A40"])["gpu_name"],
                         {"in": ["L40S", "A40"]})

    def test_vram_converted_to_mb(self):
        self.assertEqual(build_query(min_vram_gb=45)["gpu_ram"], {"gte": 45 * 1024.0})

    def test_thresholds(self):
        q = build_query(min_disk_gb=80, max_dph=1.0, min_inet_down=500,
                        min_reliability=0.95, min_cuda=12.4)
        self.assertEqual(q["disk_space"], {"gte": 80})
        self.assertEqual(q["dph_total"], {"lte": 1.0})
        self.assertEqual(q["inet_down"], {"gte": 500})
        self.assertEqual(q["reliability2"], {"gte": 0.95})
        self.assertEqual(q["cuda_max_good"], {"gte": 12.4})

    def test_extra_filters_override(self):
        q = build_query(extra_filters=["datacenter=eq:true", "num_gpus=eq:2"])
        self.assertEqual(q["datacenter"], {"eq": True})
        self.assertEqual(q["num_gpus"], {"eq": 2})  # extra wins over default


class TestGeolocation(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(expand_geolocation(""), [])
        self.assertEqual(expand_geolocation("se, no"), ["SE", "NO"])
        self.assertEqual(expand_geolocation("EU"), EU_COUNTRIES)

    def test_match_full_and_short_forms(self):
        self.assertTrue(match_geolocation("Spain, ES", ["ES"]))
        self.assertTrue(match_geolocation("ES", ["ES"]))
        self.assertFalse(match_geolocation("Estonia, EE", ["ES"]))
        self.assertTrue(match_geolocation("anything", []))  # no codes = match all


if __name__ == "__main__":
    unittest.main()
