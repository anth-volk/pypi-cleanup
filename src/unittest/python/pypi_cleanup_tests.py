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
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from pypi_cleanup import CsfrParser, FlashMessageParser, PypiCleanup, load_preserve_file, main, parse_before_date


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


PYPI_URL = "https://test.pypi.org"
PACKAGE = "test-package"
VERSIONS = ["1.0.0.dev1", "1.0.0.dev2", "1.0.0"]
CSRF = "csrf-token-value"

# Mirrors warehouse.utils.project.DELETE_RELEASE_ACKNOWLEDGMENTS
RELEASE_ACKNOWLEDGMENTS = ("acknowledge_delete_files",
                           "acknowledge_install_break",
                           "acknowledge_no_reupload",
                           "acknowledge_irreversible",
                           "acknowledge_admins_cannot_undo",
                           )


def _checkbox(name):
    return f'<input class="checkbox-list__input" type="checkbox" name="{name}" data-confirm-target="checkbox">'


def release_page(version):
    """Release management page as rendered by Warehouse: yank, delete release and delete file modals,
    all of which post back to the release page itself."""
    action = f"/manage/project/{PACKAGE}/release/{version}/"
    return f"""
    <html><body>
    <form method="GET" action="/search/"><input name="q" type="text"></form>
    <form method="POST" class="modal__form" action="{action}">
      <input name="csrf_token" type="hidden" value="{CSRF}">
      <input name="yanked_reason" type="text">
      <input name="confirm_yank_version" type="text">
    </form>
    <form method="POST" class="modal__form" action="{action}">
      <input name="csrf_token" type="hidden" value="{CSRF}">
      <ul class="checkbox-list">
        {"".join(f"<li><label>{_checkbox(name)}<span>Text</span></label></li>" for name in RELEASE_ACKNOWLEDGMENTS)}
      </ul>
      <input id="delete_version-modal-confirm_delete_version" name="confirm_delete_version" type="text">
    </form>
    <form method="POST" class="modal__form" action="{action}">
      <input name="csrf_token" type="hidden" value="{CSRF}">
      <input name="file_id" type="hidden" value="1234">
      {_checkbox("acknowledge_file_only")}
      {_checkbox("acknowledge_irreversible")}
      <input name="confirm_project_name" type="text">
    </form>
    </body></html>
    """


def login_page():
    return f"""
    <form method="POST" action="/account/login/">
      <input name="csrf_token" type="hidden" value="{CSRF}">
      <input name="username" type="text"><input name="password" type="password">
    </form>"""


def flash_messages(errors):
    return "\n".join(f"""
    <div class="notification-bar notification-bar--danger notification-bar--dismissable" role="alert">
      <div class="notification-bar__container">
        <span class="notification-bar__icon"><i class="fa fa-exclamation-triangle" aria-hidden="true"></i>
          <span class="sr-only">Error</span></span>
        <span class="notification-bar__message">{error}</span>
        <button type="button" class="notification-bar__dismiss" aria-label="Close"><i class="fa fa-times"></i></button>
      </div>
    </div>""" for error in errors)


class FakeResponse:
    def __init__(self, url, text="", json=None):
        self.url = url
        self.text = text
        self._json = json

    def raise_for_status(self):
        pass

    def json(self):
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakePypi:
    """Simulates Warehouse's release deletion view: the release is only deleted if every acknowledgment
    is checked, otherwise an error is flashed and the user is redirected back to the release page."""

    def __init__(self, deletion_disabled=False, silent_failure=False, unverified_device=False):
        self.deletion_disabled = deletion_disabled
        self.silent_failure = silent_failure
        # Login is accepted, but the session is not logged in until the device is confirmed via email
        self.unverified_device = unverified_device
        self.versions = list(VERSIONS)
        self.flash = []
        self.delete_posts = []
        self.headers = {}

    def __call__(self):
        session = MagicMock()
        session.__enter__.return_value = self
        return session

    def get(self, url, **_):
        path = url[len(PYPI_URL):]
        if path == f"/simple/{PACKAGE}/":
            return FakeResponse(url, json={
                "name": PACKAGE,
                "versions": self.versions,
                "files": [{"filename": f"{PACKAGE.replace('-', '_')}-{v}.tar.gz",
                           "upload-time": "2024-01-01T12:00:00.000000+00:00"} for v in self.versions]})
        if path == "/account/login/":
            return FakeResponse(url, text=login_page())
        if path == "/manage/projects/":
            if self.unverified_device:
                return FakeResponse(f"{PYPI_URL}/account/login/?next={path}", text=login_page())
            return FakeResponse(url)
        if path == "/_includes/unauthed/flash-messages/":
            errors, self.flash = self.flash, []
            return FakeResponse(url, text=flash_messages(errors))
        for version in self.versions:
            if path == f"/manage/project/{PACKAGE}/release/{version}/":
                if self.unverified_device:
                    return FakeResponse(f"{PYPI_URL}/account/login/?next={path}", text=login_page())
                return FakeResponse(url, text=release_page(version))
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url, data, **_):
        path = url[len(PYPI_URL):]
        if path == "/account/login/":
            if self.unverified_device:
                return FakeResponse(f"{PYPI_URL}/account/confirm-login/")
            return FakeResponse(f"{PYPI_URL}/manage/projects/")

        for version in self.versions:
            release_url = f"{PYPI_URL}/manage/project/{PACKAGE}/release/{version}/"
            if url == release_url:
                self.delete_posts.append(data)
                if data.get("csrf_token") != CSRF:
                    raise AssertionError("Bad CSRF")
                if self.deletion_disabled:
                    self.flash.append("Project deletion temporarily disabled.")
                    return FakeResponse(release_url, text=release_page(version))
                if self.silent_failure:
                    return FakeResponse(release_url, text=release_page(version))
                if data.get("confirm_delete_version") != version:
                    self.flash.append(f"Could not delete release - {data.get('confirm_delete_version')!r} "
                                      f"is not the same as {version!r}")
                    return FakeResponse(release_url, text=release_page(version))
                if not all(data.get(name) for name in RELEASE_ACKNOWLEDGMENTS):
                    self.flash.append("Could not delete release - acknowledge all of the consequences to continue")
                    return FakeResponse(release_url, text=release_page(version))
                self.versions.remove(version)
                return FakeResponse(f"{PYPI_URL}/manage/project/{PACKAGE}/releases/")
        raise AssertionError(f"Unexpected POST {url}")


class TestReleaseDeletion(unittest.TestCase):
    def run_cleanup(self, fake_pypi, verbose=False, debug=False, preserve_versions=None):
        cleanup = PypiCleanup(
            url=PYPI_URL,
            username="user",
            packages=[PACKAGE],
            do_it=True,
            patterns=None,
            verbose=verbose,
            days=0,
            query_only=False,
            leave_most_recent_only=False,
            confirm=True,
            delete_project=False,
            debug=debug,
            preserve_versions=preserve_versions
        )
        with patch("pypi_cleanup.requests.Session", fake_pypi), \
                patch("pypi_cleanup.time.sleep"), \
                patch.dict(os.environ, {"PYPI_CLEANUP_PASSWORD": "password"}):
            return cleanup.run()

    def test_delete_submits_acknowledgments(self):
        fake_pypi = FakePypi()

        with self.assertLogs(level="INFO") as logs:
            result = self.run_cleanup(fake_pypi)

        self.assertFalse(result)
        self.assertEqual(fake_pypi.versions, ["1.0.0"])
        self.assertEqual(len(fake_pypi.delete_posts), 2)
        for data, version in zip(fake_pypi.delete_posts, ["1.0.0.dev1", "1.0.0.dev2"]):
            self.assertEqual(data["confirm_delete_version"], version)
            for name in RELEASE_ACKNOWLEDGMENTS:
                self.assertEqual(data[name], "on")
            # Checkboxes from other forms on the page must not leak into the release deletion
            self.assertNotIn("acknowledge_file_only", data)
        self.assertIn(f"INFO:root:Deleted '{PACKAGE}' version 1.0.0.dev1", logs.output)
        self.assertIn(f"INFO:root:Deleted '{PACKAGE}' version 1.0.0.dev2", logs.output)

    def test_preserved_release_is_never_submitted_for_deletion(self):
        fake_pypi = FakePypi()

        with self.assertLogs(level="INFO") as logs:
            result = self.run_cleanup(fake_pypi, preserve_versions=frozenset({"1.0.0.dev2"}))

        self.assertFalse(result)
        self.assertEqual(fake_pypi.versions, ["1.0.0.dev2", "1.0.0"])
        self.assertEqual([data["confirm_delete_version"] for data in fake_pypi.delete_posts], ["1.0.0.dev1"])
        for name in RELEASE_ACKNOWLEDGMENTS:
            self.assertEqual(fake_pypi.delete_posts[0][name], "on")
        self.assertIn(f"INFO:root:Deleted '{PACKAGE}' version 1.0.0.dev1", logs.output)

    def test_rejected_delete_is_reported(self):
        fake_pypi = FakePypi(deletion_disabled=True)

        with self.assertLogs(level="INFO") as logs:
            result = self.run_cleanup(fake_pypi)

        self.assertEqual(result, 1)
        self.assertEqual(fake_pypi.versions, VERSIONS)
        # Stops at the first failure
        self.assertEqual(len(fake_pypi.delete_posts), 1)
        self.assertFalse([line for line in logs.output if "Deleted" in line])
        self.assertIn(f"ERROR:root:Failed to delete '{PACKAGE}' version 1.0.0.dev1: "
                      f"Project deletion temporarily disabled.", logs.output)

    def test_rejected_delete_without_flash_message_is_reported(self):
        fake_pypi = FakePypi(silent_failure=True)

        with self.assertLogs(level="INFO") as logs:
            result = self.run_cleanup(fake_pypi)

        self.assertEqual(result, 1)
        self.assertEqual(fake_pypi.versions, VERSIONS)
        self.assertEqual(len(fake_pypi.delete_posts), 1)
        self.assertFalse([line for line in logs.output if "Deleted" in line])
        self.assertIn(f"ERROR:root:Failed to delete '{PACKAGE}' version 1.0.0.dev1: "
                      f"redirected to {PYPI_URL}/manage/project/{PACKAGE}/release/1.0.0.dev1/", logs.output)


    def test_debug_logs_parsed_pages(self):
        with self.assertLogs(level="DEBUG") as logs:
            result = self.run_cleanup(FakePypi(), debug=True)

        self.assertFalse(result)
        fed = [line for line in logs.output if line.startswith("DEBUG:root:Feeding data:")]
        # Login page and one release page per deleted version
        self.assertEqual(len(fed), 3)
        self.assertIn('action="/account/login/"', fed[0])
        self.assertIn(f'action="/manage/project/{PACKAGE}/release/1.0.0.dev1/"', fed[1])
        self.assertIn(f'action="/manage/project/{PACKAGE}/release/1.0.0.dev2/"', fed[2])

    def test_verbose_does_not_log_parsed_pages(self):
        with self.assertLogs(level="DEBUG") as logs:
            result = self.run_cleanup(FakePypi(), verbose=True)

        self.assertFalse(result)
        self.assertFalse([line for line in logs.output if "Feeding data:" in line])


class TestUnverifiedDevice(unittest.TestCase):
    def test_unconfirmed_login_is_reported_before_any_delete(self):
        fake_pypi = FakePypi(unverified_device=True)
        argv = ["pypi-cleanup", "-t", PYPI_URL, "-u", "user", "-p", PACKAGE, "--do-it"]

        with patch("pypi_cleanup.requests.Session", fake_pypi), \
                patch("pypi_cleanup.time.sleep"), \
                patch.dict(os.environ, {"PYPI_CLEANUP_PASSWORD": "password"}), \
                patch("sys.argv", argv), \
                self.assertLogs(level="INFO") as logs:
            result = main()

        self.assertEqual(result, 1)
        self.assertEqual(fake_pypi.versions, VERSIONS)
        self.assertEqual(fake_pypi.delete_posts, [])
        errors = [line for line in logs.output if line.startswith("ERROR:")]
        self.assertEqual(len(errors), 1)
        self.assertIn("PyPI login did not complete", errors[0])
        self.assertIn(f"{PYPI_URL}/account/login/", errors[0])


class TestFlashMessageParser(unittest.TestCase):
    def test_messages_extracted(self):
        parser = FlashMessageParser()
        parser.feed(flash_messages(["Could not delete release - acknowledge all of the consequences to continue",
                                    "Line one<br>line <span><b>two</b></span>\n   continued",
                                    ]))
        parser.close()

        self.assertEqual(parser.messages,
                         ["Could not delete release - acknowledge all of the consequences to continue",
                          "Line oneline two continued",
                          ])


class TestCsfrParser(unittest.TestCase):
    def test_checkboxes_scoped_to_form_with_input(self):
        parser = CsfrParser(f"/manage/project/{PACKAGE}/release/1.0.0/", "confirm_delete_version")
        parser.feed(release_page("1.0.0"))

        self.assertEqual(parser.csrf, CSRF)
        self.assertEqual(parser.checkboxes, {name: "on" for name in RELEASE_ACKNOWLEDGMENTS})

    def test_checkboxes_scoped_to_other_form_on_same_page(self):
        parser = CsfrParser(f"/manage/project/{PACKAGE}/release/1.0.0/", "confirm_project_name")
        parser.feed(release_page("1.0.0"))

        self.assertEqual(parser.csrf, CSRF)
        self.assertEqual(parser.checkboxes, {"acknowledge_file_only": "on", "acknowledge_irreversible": "on"})


if __name__ == '__main__':
    unittest.main()
