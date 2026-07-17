import unittest
from unittest.mock import patch

import server


class ProjectServiceTests(unittest.TestCase):
    @patch("server.project_server_signature", return_value=True)
    def test_verified_listener_requires_project_port_python_and_server_command(self, signature):
        valid = {"pid": 10, "port": 8767, "name": "python.exe", "command": "python server.py --port 8767"}
        self.assertTrue(server.verified_project_listener(valid))
        self.assertFalse(server.verified_project_listener({**valid, "port": 9000}))
        self.assertFalse(server.verified_project_listener({**valid, "name": "database.exe"}))
        self.assertFalse(server.verified_project_listener({**valid, "command": "python other_server.py --port 8767"}))
        signature.assert_called_once_with(8767)

    @patch("server.project_server_signature", return_value=False)
    def test_listener_with_wrong_api_signature_is_not_verified(self, _signature):
        listener = {"pid": 10, "port": 8767, "name": "python.exe", "command": "python server.py --port 8767"}
        self.assertFalse(server.verified_project_listener(listener))


if __name__ == "__main__":
    unittest.main()
