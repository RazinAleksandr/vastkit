import time
import unittest

from vastkit.models import Instance, Offer, normalize_instance_payload


class TestOffer(unittest.TestCase):
    def test_from_raw_defensive(self):
        o = Offer.from_raw({"id": "7", "gpu_ram": None, "dph_total": "0.5",
                            "gpu_name": None})
        self.assertEqual(o.id, 7)
        self.assertEqual(o.gpu_ram, 0.0)
        self.assertEqual(o.dph_total, 0.5)
        self.assertEqual(o.gpu_name, "")

    def test_reliability2_preferred(self):
        o = Offer.from_raw({"id": 1, "reliability": 0.5, "reliability2": 0.99})
        self.assertEqual(o.reliability, 0.99)

    def test_gpu_ram_gb(self):
        o = Offer.from_raw({"id": 1, "gpu_ram": 49152})
        self.assertAlmostEqual(o.gpu_ram_gb, 48.0)


class TestInstance(unittest.TestCase):
    def test_from_raw(self):
        i = Instance.from_raw({"id": 5, "actual_status": "running",
                               "ssh_host": "h", "ssh_port": 22, "dph_total": 0.4})
        self.assertTrue(i.has_ssh)
        self.assertEqual(i.actual_status, "running")

    def test_accrued_cost(self):
        i = Instance.from_raw({"id": 5, "dph_total": 1.0,
                               "start_date": time.time() - 7200})
        self.assertAlmostEqual(i.accrued_cost, 2.0, places=1)

    def test_no_ssh(self):
        i = Instance.from_raw({"id": 5})
        self.assertFalse(i.has_ssh)
        self.assertEqual(i.accrued_cost, 0.0)


class TestNormalizePayload(unittest.TestCase):
    def test_direct_dict(self):
        d = {"id": 1, "actual_status": "running"}
        self.assertEqual(normalize_instance_payload(d, 1)["id"], 1)

    def test_wrapped_dict(self):
        d = {"instances": {"id": 2}}
        self.assertEqual(normalize_instance_payload(d, 2)["id"], 2)

    def test_wrapped_list_finds_by_id(self):
        d = {"instances": [{"id": 1}, {"id": 2}]}
        self.assertEqual(normalize_instance_payload(d, 2)["id"], 2)

    def test_missing_raises(self):
        with self.assertRaises(LookupError):
            normalize_instance_payload({"instances": [{"id": 1}]}, 99)
        with self.assertRaises(LookupError):
            normalize_instance_payload({"instances": None}, 1)


if __name__ == "__main__":
    unittest.main()
