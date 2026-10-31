import unittest

from dashboard.dashboard import (
    GitHubAPIError,
    GitHubClient,
    collect_dashboard_data,
    sync_collection_repositories,
)


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

        class FakeReadClient(GitHubClient):
            def __init__(self) -> None:
                super().__init__(None)

            def get_json(self, endpoint):
                if endpoint.startswith("/users/"):
                    return profile
                if endpoint.startswith("/users/octocat/repos"):
                    return [repo]
                raise AssertionError(f"Unexpected read endpoint: {endpoint}")

            def get_repositories(self, username):
                return [repo]

        class FakeWriteClient(GitHubClient):
            def __init__(self) -> None:
                super().__init__("token")

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
            self.fail(
                f"sync_collection_repositories should skip inaccessible repositories, not fail: {exc}"
            )

    def test_collect_dashboard_data_ignores_api_access_errors(self):
        config = {
            "github_username": "octocat",
            "recent_repository_limit": 3,
            "latest_commit_limit": 2,
            "language_scan_limit": 2,
            "focus_areas": ["Robotics"],
        }
        repo = {
            "full_name": "octocat/hello-world",
            "name": "hello-world",
            "html_url": "https://github.com/octocat/hello-world",
            "updated_at": "2026-10-25T12:00:00Z",
            "stargazers_count": 7,
            "forks_count": 2,
            "private": False,
        }
        profile = {"public_repos": 1, "followers": 10, "following": 2, "public_gists": 3}

        class FakeClient(GitHubClient):
            def __init__(self) -> None:
                super().__init__(None)

            def get_json(self, endpoint):
                if endpoint.startswith("/users/"):
                    return profile
                if endpoint.startswith("/users/octocat/repos"):
                    return [repo]
                if endpoint.startswith("/repos/octocat/hello-world/languages"):
                    raise GitHubAPIError(403, '{"message":"API rate limit exceeded"}')
                if endpoint.startswith("/repos/octocat/hello-world/commits"):
                    return []
                raise AssertionError(f"Unexpected endpoint: {endpoint}")

            def get_repositories(self, username):
                return [repo]

        data = collect_dashboard_data(FakeClient(), config, "2026-10-25")
        self.assertEqual(data["stats"]["repository_count"], 1)
        self.assertEqual(data["languages"], [])
        self.assertIn(
            "No non-dashboard repository pushes were recorded", data["engineering_summary"]
        )


if __name__ == "__main__":
    unittest.main()
