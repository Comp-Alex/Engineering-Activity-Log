import unittest
from unittest.mock import patch

from dashboard.dashboard import GitHubAPIError, GitHubClient, sync_collection_repositories


class SyncCollectionRepositoriesTests(unittest.TestCase):
    def test_sync_ignores_repo_write_forbidden_errors(self):
        config = {
            "github_username": "octocat",
            "collection_repo_name_keywords": ["activities", "lab"],
            "collection_repo_names": [],
            "collection_repo_excluded_names": [],
            "collection_log_path": ".github/engineering-activity-log.md",
            "recent_repository_limit": 3,
            "focus_areas": ["Robotics"],
        }
        profile = {"public_repos": 1, "followers": 0, "following": 0, "public_gists": 0}
        repo = {
            "full_name": "octocat/activity-lab",
            "name": "activity-lab",
            "html_url": "https://github.com/octocat/activity-lab",
            "updated_at": "2026-10-25T12:00:00Z",
            "stargazers_count": 0,
            "forks_count": 0,
        }

        class FakeReadClient:
            def get_json(self, endpoint):
                if endpoint.startswith("/users/"):
                    return profile
                if endpoint.startswith("/users/octocat/repos"):
                    return [repo]
                raise AssertionError(f"Unexpected read endpoint: {endpoint}")

            def get_repositories(self, username):
                return [repo]

        class FakeWriteClient:
            def get_json(self, endpoint):
                raise GitHubAPIError(403, '{"message":"Resource not accessible by integration"}')

            def put_json(self, endpoint, payload):
                raise AssertionError("put_json should not be called on forbidden repos")

        try:
            sync_collection_repositories(
                FakeReadClient(),
                FakeWriteClient(),
                config,
                "2026-10-25",
                "0945",
                "9:45 AM",
            )
        except RuntimeError as exc:
            self.fail(f"sync_collection_repositories should skip inaccessible repositories, not fail: {exc}")


if __name__ == "__main__":
    unittest.main()
