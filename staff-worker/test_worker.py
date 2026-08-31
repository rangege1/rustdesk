import unittest
from unittest.mock import patch

import worker


class FileTransferWorkerTests(unittest.TestCase):
    def test_file_transfer_task_uses_rustdesk_file_transfer_mode(self):
        command: list[str] = []

        class Process:
            def terminate(self):
                pass

        def popen(args, **_):
            command.extend(args)
            return Process()

        with patch.object(worker, "close_dashboard_windows"), patch.object(worker, "find_rustdesk", return_value=r"C:\\staff\\rustdesk.exe"), patch.object(worker.subprocess, "Popen", side_effect=popen), patch.object(worker, "session_window_open", return_value=True):
            self.assertTrue(worker.connect_rustdesk("123456789", "password", lambda: None, lambda: False, "file_transfer"))

        self.assertEqual(command, [r"C:\\staff\\rustdesk.exe", "--file-transfer", "123456789", "--password", "password"])


if __name__ == "__main__":
    unittest.main()
