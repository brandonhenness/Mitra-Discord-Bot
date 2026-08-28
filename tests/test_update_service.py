from __future__ import annotations

import unittest
from unittest.mock import patch

from mitra_bot.services.update_service import check_latest_release


def _release_payload(version: str) -> dict[str, object]:
    return {
        "tag_name": version,
        "zipball_url": "https://example.test/release.zip",
        "html_url": "https://example.test/release",
        "body": "notes",
    }


class UpdateServiceVersionTests(unittest.TestCase):
    def _check(self, *, current: str, latest: str):
        with (
            patch(
                "mitra_bot.services.update_service.get_current_version",
                return_value=current,
            ),
            patch(
                "mitra_bot.services.update_service.resolve_github_repo",
                return_value="owner/repo",
            ),
            patch(
                "mitra_bot.services.update_service.get_updater_config",
                return_value={"include_prerelease": True},
            ),
            patch("mitra_bot.services.update_service.set_updater_config"),
            patch(
                "mitra_bot.services.update_service._fetch_release_payload",
                return_value=(_release_payload(latest), None),
            ),
        ):
            return check_latest_release()

    def test_only_strictly_newer_versions_are_available(self) -> None:
        cases = (
            ("1.9.0", "v1.10.0", True),
            ("1.0.0", "v1.0", False),
            ("2.0.0", "v1.99.0", False),
            ("1.2.3", "v1.3.0-rc.1", True),
            ("1.3.0", "v1.3.0-rc.1", False),
        )

        for current, latest, expected in cases:
            with self.subTest(current=current, latest=latest):
                result = self._check(current=current, latest=latest)
                self.assertEqual(result.available, expected)
                self.assertIsNone(result.error)

    def test_invalid_release_version_is_not_offered(self) -> None:
        result = self._check(current="1.0.0", latest="not-a-version")

        self.assertFalse(result.available)
        self.assertEqual(result.latest_version, "not-a-version")
        self.assertIsNotNone(result.error)
        self.assertIn("Could not compare release versions", result.error or "")


if __name__ == "__main__":
    unittest.main()
