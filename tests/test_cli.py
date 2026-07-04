import unittest

from vastkit.cli import _strip_dashes, build_parser, fmt_table, money, parse_env


class TestParser(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_search_flags(self):
        args = self.parser.parse_args([
            "search", "--gpu", "L40S", "--gpu", "RTX 6000Ada", "--vram", "45",
            "--max-price", "1.0", "--sort", "effective", "-n", "5",
        ])
        self.assertEqual(args.gpu, ["L40S", "RTX 6000Ada"])
        self.assertEqual(args.vram, 45.0)
        self.assertEqual(args.limit, 5)

    def test_exec_remainder(self):
        args = self.parser.parse_args(["exec", "123", "--", "nvidia-smi", "-L"])
        self.assertEqual(args.instance, "123")
        self.assertEqual(_strip_dashes(args.cmd), ["nvidia-smi", "-L"])

    def test_exec_flags_after_positional(self):
        # regression: flags after the instance id must be parsed as flags,
        # not swallowed into the remote command (argparse.REMAINDER did that)
        from vastkit.cli import split_remote_command

        head, tail = split_remote_command([
            "exec", "123", "--detach", "--job", "j1", "--cwd", "/w",
            "--env", "A=1", "--", "pip", "install", "-q", "x",
        ])
        args = self.parser.parse_args(head)
        self.assertTrue(args.detach)
        self.assertEqual(args.job, "j1")
        self.assertEqual(args.cwd, "/w")
        self.assertEqual(args.env, ["A=1"])
        self.assertEqual(tail, ["pip", "install", "-q", "x"])

    def test_split_noop_for_other_commands(self):
        from vastkit.cli import split_remote_command

        head, tail = split_remote_command(["search", "--gpu", "L40S"])
        self.assertEqual(tail, [])
        self.assertEqual(head, ["search", "--gpu", "L40S"])

    def test_rent_env(self):
        args = self.parser.parse_args(["rent", "--env", "A=1", "--env", "B=x=y", "-y"])
        self.assertEqual(parse_env(args.env), {"A": "1", "B": "x=y"})

    def test_destroy_multiple(self):
        args = self.parser.parse_args(["destroy", "1", "2", "--yes"])
        self.assertEqual(args.instances, ["1", "2"])


class TestHelpers(unittest.TestCase):
    def test_parse_env_invalid(self):
        with self.assertRaises(SystemExit):
            parse_env(["NOEQUALS"])

    def test_money(self):
        self.assertEqual(money(0.5), "$0.500")
        self.assertEqual(money(12.3456), "$12.35")

    def test_fmt_table_alignment(self):
        out = fmt_table(["A", "LONGHEAD"], [["xx", "1"], ["y", "22"]], rjust={1})
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)  # header, separator, 2 rows
        # right-justified column: values end at the same column
        self.assertTrue(lines[2].endswith(" 1"))
        self.assertTrue(lines[3].endswith("22"))


if __name__ == "__main__":
    unittest.main()
