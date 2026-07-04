import unittest

from vastkit.remote import (
    _job_dir,
    _rsync_unusable,
    env_prefix,
    ssh_argv,
    wrap_command,
)


class TestSSHCommand(unittest.TestCase):
    def test_ssh_argv(self):
        argv = ssh_argv("1.2.3.4", 2222, key="~/.ssh/k")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("2222", argv)
        self.assertEqual(argv[-1], "root@1.2.3.4")
        self.assertIn("StrictHostKeyChecking=no", argv)
        # key path expanded
        self.assertTrue(any(a.endswith("/.ssh/k") for a in argv))

    def test_env_prefix_quotes(self):
        import shlex

        prefix = env_prefix({"HF_TOKEN": "abc def'x"})
        self.assertIn("export HF_TOKEN=", prefix)
        # the quoted value must round-trip through shell tokenization intact
        tokens = shlex.split(prefix.rstrip("; "))
        self.assertIn("HF_TOKEN=abc def'x", tokens)

    def test_wrap_command(self):
        cmd = wrap_command("python run.py", env={"A": "1"}, cwd="/workspace/LUA")
        self.assertEqual(cmd, "export A=1; cd /workspace/LUA && python run.py")


class TestJobs(unittest.TestCase):
    def test_job_dir_sanitized(self):
        self.assertEqual(_job_dir("my job/../x"), "/tmp/vastkit-jobs/my-job-..-x")
        self.assertEqual(_job_dir("run_1.batch"), "/tmp/vastkit-jobs/run_1.batch")


class TestRsyncFallback(unittest.TestCase):
    def test_detects_missing_remote_rsync(self):
        self.assertTrue(_rsync_unusable(127, "bash: rsync: command not found"))
        self.assertTrue(_rsync_unusable(12, "rsync: not found\nprotocol error"))

    def test_real_errors_not_swallowed(self):
        self.assertFalse(_rsync_unusable(23, "permission denied"))
        self.assertFalse(_rsync_unusable(0, ""))


if __name__ == "__main__":
    unittest.main()
