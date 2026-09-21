#   -*- coding: utf-8 -*-
#   Copyright (C) 2020 Arcadiy Ivanov <arcadiy@ivanov.biz>
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import argparse
import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from pypi_cleanup import PypiCleanup, load_preserve_file, parse_before_date


class ContextResponse:
    def __init__(self, url, text=""):
        self.url = url
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        return None


class FakeConfirmationSession:
    def __init__(self):
        self.urls = []

    def get(self, url, headers=None):
        self.urls.append((url, headers))
        if url.startswith("https://pypi.org/account/confirm-login/"):
            return ContextResponse("https://pypi.org/manage/projects/")
        if url == "https://pypi.org/manage/projects/":
            return ContextResponse("https://pypi.org/manage/projects/")
        raise AssertionError(f"Unexpected URL {url}")


def cleanup_for_tests():
    return PypiCleanup(
        url="https://pypi.org",
        username="user",
        packages=["test-package"],
        do_it=False,
        patterns=None,
        verbose=False,
        days=0,
        query_only=False,
        leave_most_recent_only=False,
        confirm=False,
        delete_project=False
    )


class TestEmptyMatchesListRegression(unittest.TestCase):
    """
    Regression test for the bug where max() is called on an empty list when
    a package version has no matching files based on the package_matches_file filter.

    This can happen when a package has versions listed but the files don't match
    the expected naming patterns (e.g., .whl, .tar.gz, .zip with correct naming).

    Bug: ValueError: max() arg is an empty sequence
    """

    @patch('pypi_cleanup.requests.Session')
    def test_version_with_no_matching_files_does_not_crash(self, mock_session):
        """
        Test that when a version has files but none match the expected patterns,
        the code doesn't crash with ValueError from max() on empty list.
        """
        # Mock the session and response
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        # Mock response for package query with a version that has no matching files
        mock_response = Mock()
        mock_response.json.return_value = {
            "name": "test-package",
            "versions": ["1.0.0"],
            "files": [
                # Files that won't match the package_matches_file filter
                {
                    "filename": "wrongname-1.0.0.tar.gz",  # Wrong package name
                    "upload-time": "2024-01-01T12:00:00.000000+00:00"
                }
            ]
        }
        mock_response.raise_for_status = Mock()
        mock_session_instance.get.return_value.__enter__.return_value = mock_response

        # Create PypiCleanup instance in query-only mode (no auth needed)
        cleanup = PypiCleanup(
            url="https://test.pypi.org",
            username=None,
            packages=["test-package"],
            do_it=False,
            patterns=None,
            verbose=False,
            days=0,
            query_only=True,
            leave_most_recent_only=False,
            confirm=False,
            delete_project=False
        )

        # This should not raise ValueError from max() on empty list
        try:
            result = cleanup.run()
            # In query-only mode with no matching releases, it should return None
            self.assertIsNone(result)
        except ValueError as e:
            if "max() arg is an empty sequence" in str(e):
                self.fail("max() was called on empty list - bug not fixed!")
            raise


class TestBeforeAndPreserveOptions(unittest.TestCase):
    def write_preserve_file(self, directory, data):
        path = Path(directory) / "preserve.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_before_date_is_utc_midnight(self):
        self.assertEqual(
            parse_before_date("2026-07-01"),
            datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc),
        )

    def test_before_date_rejects_non_iso_date(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_before_date("July 1, 2026")

    def test_preserve_file_loads_versions_for_normalized_package_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_preserve_file(
                directory,
                {"package": "test_package", "preserve_versions": ["1.0.0", "2.0.0"]},
            )

            self.assertEqual(
                load_preserve_file(path, ["test-package"]),
                frozenset(("1.0.0", "2.0.0")),
            )

    def test_preserve_file_rejects_wrong_package(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_preserve_file(
                directory,
                {"package": "other-package", "preserve_versions": ["1.0.0"]},
            )

            with self.assertRaisesRegex(ValueError, "not requested package"):
                load_preserve_file(path, ["test-package"])

    def test_preserve_file_rejects_invalid_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_preserve_file(
                directory,
                {"package": "test-package", "preserve_versions": ["1.0.0", " 2.0.0"]},
            )

            with self.assertRaisesRegex(ValueError, "non-empty strings"):
                load_preserve_file(path, ["test-package"])

    @patch("pypi_cleanup.requests.Session")
    def test_preserved_release_is_excluded_after_before_date_selection(self, mock_session):
        mock_session_instance = MagicMock()
        mock_session.return_value.__enter__.return_value = mock_session_instance

        mock_response = Mock()
        mock_response.json.return_value = {
            "name": "test-package",
            "versions": ["1.0.0", "1.0.1", "1.0.2"],
            "files": [
                {
                    "filename": "test_package-1.0.0.tar.gz",
                    "upload-time": "2026-06-30T23:58:00.000000+00:00",
                },
                {
                    "filename": "test_package-1.0.1.tar.gz",
                    "upload-time": "2026-06-30T23:59:00.000000+00:00",
                },
                {
                    "filename": "test_package-1.0.2.tar.gz",
                    "upload-time": "2026-07-01T00:00:00.000000+00:00",
                },
            ],
        }
        mock_response.raise_for_status = Mock()
        mock_session_instance.get.return_value.__enter__.return_value = mock_response

        cleanup = PypiCleanup(
            url="https://pypi.org",
            username=None,
            packages=["test-package"],
            do_it=False,
            patterns=[re.compile(".*")],
            verbose=False,
            days=0,
            before=parse_before_date("2026-07-01"),
            preserve_file="preserve.json",
            preserve_versions=frozenset(("1.0.0",)),
            query_only=True,
            leave_most_recent_only=False,
            confirm=False,
            delete_project=False,
        )

        with self.assertLogs(level="INFO") as logs:
            self.assertIsNone(cleanup.run())

        output = "\n".join(logs.output)
        self.assertIn("Releases selected before applying preserve file", output)
        self.assertIn("Releases excluded by preserve file", output)
        self.assertIn("Final releases of package 'test-package' to delete", output)
        self.assertIn(" 1.0.0", output)
        final_releases = output.split("Final releases of package 'test-package' to delete:", 1)[1]
        self.assertIn(" 1.0.1", final_releases)
        self.assertNotIn(" 1.0.0", final_releases)
        self.assertNotIn(" 1.0.2", output)


class TestCsrfParsing(unittest.TestCase):
    def test_delete_release_form_csrf_is_found(self):
        cleanup = cleanup_for_tests()
        response = ContextResponse(
            "https://pypi.org/manage/project/test-package/release/1.0.0/",
            """
            <form method="POST" action="/manage/project/test-package/release/1.0.0/">
              <input name="csrf_token" type="hidden" value="csrf-value">
              <input name="confirm_delete_version" type="text">
            </form>
            """,
        )

        self.assertEqual(
            cleanup._csrf_from_response(
                response,
                "/manage/project/test-package/release/1.0.0/",
                "confirm_delete_version",
            ),
            "csrf-value",
        )

    def test_missing_delete_form_explains_email_confirmation(self):
        cleanup = cleanup_for_tests()
        response = ContextResponse(
            "https://pypi.org/account/confirm-login/",
            "<html><head><title>Please confirm this login</title></head></html>",
        )

        with self.assertRaisesRegex(ValueError, "PyPI requires email confirmation"):
            cleanup._csrf_from_response(
                response,
                "/manage/project/test-package/release/1.0.0/",
                "confirm_delete_version",
            )


class TestLoginConfirmation(unittest.TestCase):
    @patch("pypi_cleanup.getpass.getpass")
    def test_email_confirmation_url_is_followed_in_same_session(self, mock_getpass):
        cleanup = cleanup_for_tests()
        session = FakeConfirmationSession()
        mock_getpass.return_value = "https://pypi.org/account/confirm-login/?token=abc123"

        self.assertTrue(cleanup._complete_email_login_confirmation(session))
        self.assertEqual(
            session.urls,
            [
                (
                    "https://pypi.org/account/confirm-login/?token=abc123",
                    {"referer": "https://pypi.org/account/confirm-login/"},
                ),
                ("https://pypi.org/manage/projects/", None),
            ],
        )

    @patch("pypi_cleanup.getpass.getpass")
    def test_unexpected_confirmation_url_is_rejected(self, mock_getpass):
        cleanup = cleanup_for_tests()
        session = FakeConfirmationSession()
        mock_getpass.return_value = "https://example.com/account/confirm-login/?token=abc123"

        self.assertFalse(cleanup._complete_email_login_confirmation(session))
        self.assertEqual(session.urls, [])


if __name__ == '__main__':
    unittest.main()
