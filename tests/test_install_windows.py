import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPO / "scripts" / "install.py"

spec = importlib.util.spec_from_file_location("install_script", INSTALLER_PATH)
install_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_script)


class InstallWindowsTests(unittest.TestCase):
    def test_write_log_file_writes_utf8(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "install.log"
            install_script.write_log_file(path, ["line with emoji ☃", "another line"])
            self.assertTrue(path.exists())
            self.assertIn("☃", path.read_text(encoding="utf-8"))

    def test_link_cli_creates_cmd_wrapper_when_symlink_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dest_dir = Path(tmpdir)
            with mock.patch.object(install_script.os.path, "islink", return_value=False), \
                 mock.patch.object(install_script.os.path, "exists", return_value=False), \
                 mock.patch.object(install_script.os, "chmod", return_value=None), \
                 mock.patch.object(install_script.os, "symlink",
                                   side_effect=OSError("[WinError 1314] A required privilege is not held by the client")):
                success = install_script.link_cli(str(dest_dir))
            self.assertTrue(success)
            wrapper = dest_dir / "playwrong.cmd"
            self.assertTrue(wrapper.exists())
            self.assertIn("python", wrapper.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
