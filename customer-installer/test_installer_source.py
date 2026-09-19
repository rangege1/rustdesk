from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
PROGRAM = (ROOT / "Program.cs").read_text(encoding="utf-8")
BUILD_SCRIPT = (ROOT / "build-single-client.ps1").read_text(encoding="utf-8")


class InstallerSourceTests(unittest.TestCase):
    def test_installation_is_serialized_before_runtime_is_modified(self):
        self.assertIn('Global\\RemoteInstallRuntimeInstaller', PROGRAM)
        self.assertIn('WaitOne(TimeSpan.FromMinutes(10))', PROGRAM)

    def test_old_runtime_is_removed_instead_of_mixing_versions(self):
        self.assertIn('DeleteDirectoryWithRetry(destinationRoot)', PROGRAM)

    def test_copied_runtime_is_verified_before_rustdesk_starts(self):
        verify = PROGRAM.index('VerifyRuntimeCopy(installRoot, finalInstallRoot)')
        start = PROGRAM.index('StartChild(rustDesk, "rustdesk", finalInstallRoot)')
        self.assertLess(verify, start)
        self.assertIn('ValidateRustDeskRuntime(finalInstallRoot)', PROGRAM)

    def test_packaging_smoke_tests_the_exact_runtime_being_embedded(self):
        for name in (
            'rustdesk.exe',
            'librustdesk.dll',
            'flutter_windows.dll',
            'desktop_multi_window_plugin.dll',
        ):
            self.assertIn(name, BUILD_SCRIPT)
        self.assertIn('--version', BUILD_SCRIPT)


if __name__ == "__main__":
    unittest.main()
