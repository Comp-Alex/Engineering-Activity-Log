#!/usr/bin/env python3
"""Build the profile README and append a daily GitHub activity snapshot."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://api.github.com"
ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
MANILA_OFFSET = timedelta(hours=8)
SCHEDULE_SLOTS = {
    "45 1 * * *": ("09:45", "9:45 AM"),
    "45 9 * * *": ("17:45", "5:45 PM"),
    "45 17 * * *": ("01:45", "1:45 AM"),
}


def get_run_context(
    schedule: str | None = None,
    now_utc: datetime | None = None,
) -> tuple[str, str, str]:
    current_utc = now_utc or datetime.now(timezone.utc)
    current_manila = current_utc + MANILA_OFFSET
    schedule = schedule if schedule is not None else os.environ.get("GITHUB_EVENT_SCHEDULE", "")
    slot, slot_label = SCHEDULE_SLOTS.get(
        schedule,
        (current_manila.strftime("%H:%M"), current_manila.strftime("%I:%M %p").lstrip("0")),
    )
    return current_manila.date().isoformat(), slot, slot_label


def entry_marker(today: str, slot: str) -> str:
    return f"<!-- dashboard-entry:{today}:{slot.replace(':', '')} -->"


def was_pushed_on_manila_date(pushed_at: str | None, today: str) -> bool:
    if not pushed_at:
        return False
    try:
        timestamp = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (timestamp + MANILA_OFFSET).date().isoformat() == today


class GitHubAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def is_transient_gitHub_api_error(error: GitHubAPIError) -> bool:
    return error.status in (401, 403, 404, 409, 429)


class GitHubClient:
    def __init__(self, token: str | None) -> None:
        self.token = token

    def get_json(self, endpoint: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-dashboard",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(f"{API_BASE}{endpoint}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(error.code, f"GitHub API {endpoint}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Could not reach the GitHub API: {error.reason}") from error

    def put_json(self, endpoint: str, payload: dict[str, Any]) -> Any:
        if not self.token:
            raise RuntimeError("A GitHub token is required to update a repository")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "profile-dashboard",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }
        request = urllib.request.Request(
            f"{API_BASE}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise GitHubAPIError(error.code, f"GitHub API {endpoint}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Could not reach the GitHub API: {error.reason}") from error

    def get_repositories(self, username: str) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "type": "owner",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                }
            )
            page_repositories = self.get_json(f"/users/{username}/repos?{query}")
            if not isinstance(page_repositories, list) or not page_repositories:
                break
            repositories.extend(
                repo for repo in page_repositories if not repo.get("private", False)
            )
            if len(page_repositories) < 100:
                break
            page += 1
        return repositories


def has_non_automated_commit(client: GitHubClient, repository: dict[str, Any], today: str) -> bool:
    local_midnight = datetime.fromisoformat(today).replace(tzinfo=timezone(MANILA_OFFSET))
    since = local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"since": since, "per_page": 100})
    try:
        commits = client.get_json(f"/repos/{repository['full_name']}/commits?{query}")
    except GitHubAPIError as error:
        if error.status in (401, 403, 404, 409, 429):
            print(
                f"Warning: could not inspect commit history for {repository['full_name']} "
                f"(HTTP {error.status}); continuing with a conservative snapshot."
            )
            return False
        raise

    automated_prefixes = (
        "chore: update profile dashboard",
        "docs: update engineering journal",
    )
    for commit in commits if isinstance(commits, list) else []:
        author = commit.get("author") or {}
        committer = commit.get("committer") or {}
        login = (author.get("login") or committer.get("login") or "").casefold()
        message = (commit.get("commit", {}).get("message") or "").splitlines()[0].casefold()
        if login == "github-actions[bot]" or message.startswith(automated_prefixes):
            continue
        return True
    return False


def markdown_cell(value: Any) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return text.replace("|", "&#124;").replace("`", "&#96;")


def short_date(value: str | None) -> str:
    if not value:
        return "Unknown"
    return value[:10]


def repository_link(repository: dict[str, Any]) -> str:
    name = markdown_cell(repository.get("name", "Repository"))
    url = repository.get("html_url", "#")
    return f"[{name}]({url})"


def collect_dashboard_data(
    client: GitHubClient,
    config: dict[str, Any],
    today: str,
) -> dict[str, Any]:
    username = config["github_username"]
    try:
        profile = client.get_json(f"/users/{urllib.parse.quote(username, safe='')}")
    except GitHubAPIError as error:
        if error.status not in (401, 403, 429):
            raise
        print(
            f"Warning: GitHub profile lookup failed for {username} (HTTP {error.status}); using a minimal dashboard snapshot."
        )
        profile = {
            "login": username,
            "public_repos": 0,
            "followers": 0,
            "following": 0,
            "public_gists": 0,
        }

    try:
        repositories = client.get_repositories(urllib.parse.quote(username, safe=""))
    except GitHubAPIError as error:
        if error.status not in (401, 403, 429):
            raise
        print(
            f"Warning: GitHub repository listing failed for {username} (HTTP {error.status}); continuing with no repository data."
        )
        repositories = []
    repositories.sort(key=lambda repo: repo.get("updated_at") or "", reverse=True)

    recent_limit = max(1, int(config.get("recent_repository_limit", 5)))
    commit_limit = max(1, int(config.get("latest_commit_limit", 5)))
    language_scan_limit = max(1, int(config.get("language_scan_limit", 20)))
    recent_repositories = repositories[:recent_limit]

    language_bytes: dict[str, int] = defaultdict(int)
    scanned_repositories = repositories[:language_scan_limit]
    for repository in scanned_repositories:
        try:
            languages = client.get_json(f"/repos/{repository['full_name']}/languages")
        except GitHubAPIError as error:
            if error.status in (401, 403, 404, 409, 429):
                print(
                    f"Warning: could not read language stats for {repository['full_name']} "
                    f"(HTTP {error.status}); skipping that repository's language data."
                )
                continue
            raise
        if isinstance(languages, dict):
            for language, byte_count in languages.items():
                language_bytes[language] += int(byte_count)

    language_total = sum(language_bytes.values())
    languages = [
        {
            "name": name,
            "bytes": byte_count,
            "percent": (byte_count / language_total * 100) if language_total else 0,
        }
        for name, byte_count in sorted(language_bytes.items(), key=lambda item: (-item[1], item[0]))
    ]

    latest_commits: list[dict[str, Any]] = []
    for repository in repositories[:commit_limit]:
        try:
            commits = client.get_json(f"/repos/{repository['full_name']}/commits?per_page=1")
        except GitHubAPIError as error:
            if error.status in (401, 403, 404, 409, 429):
                print(
                    f"Warning: could not inspect recent commits for {repository['full_name']} "
                    f"(HTTP {error.status}); skipping commit metadata for this repository."
                )
                continue
            raise
        if not isinstance(commits, list) or not commits:
            continue
        commit = commits[0]
        commit_data = commit.get("commit", {})
        latest_commits.append(
            {
                "repository": repository,
                "sha": commit.get("sha", "")[:7],
                "message": (commit_data.get("message") or "").splitlines()[0],
                "date": (commit_data.get("author") or commit_data.get("committer") or {}).get(
                    "date"
                ),
                "url": commit.get("html_url", repository.get("html_url", "#")),
            }
        )

    public_repository_count = int(profile.get("public_repos", len(repositories)))
    stats = {
        "repository_count": len(repositories),
        "public_repository_count": public_repository_count,
        "followers": int(profile.get("followers", 0)),
        "following": int(profile.get("following", 0)),
        "public_gists": int(profile.get("public_gists", 0)),
        "stars": sum(int(repository.get("stargazers_count", 0)) for repository in repositories),
        "forks": sum(int(repository.get("forks_count", 0)) for repository in repositories),
    }
    pushed_today = [
        repository
        for repository in repositories
        if was_pushed_on_manila_date(repository.get("pushed_at"), today)
        and has_non_automated_commit(client, repository, today)
    ]

    if pushed_today:
        names = ", ".join(repository.get("name", "repository") for repository in pushed_today)
        engineering_summary = (
            f"GitHub recorded non-dashboard pushes to {len(pushed_today)} public "
            f"{'repository' if len(pushed_today) == 1 else 'repositories'} today: {names}. "
            "This journal entry captures the current profile and repository snapshot."
        )
    else:
        engineering_summary = (
            f"No non-dashboard repository pushes were recorded on {today}. This entry captures the daily "
            "profile snapshot and keeps the engineering journal continuous; it does not infer "
            "coding activity from an unchanged repository list."
        )

    return {
        "profile": profile,
        "repositories": repositories,
        "recent_repositories": recent_repositories,
        "latest_commits": latest_commits,
        "languages": languages,
        "language_repository_count": len(scanned_repositories),
        "stats": stats,
        "pushed_today": pushed_today,
        "engineering_summary": engineering_summary,
    }


def render_stats(stats: dict[str, int]) -> str:
    return (
        "| Repositories | Public repositories | Followers | Following | Public gists | Stars | Forks |\n"
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n"
        f"| {stats['repository_count']} | {stats['public_repository_count']} | {stats['followers']} | "
        f"{stats['following']} | {stats['public_gists']} | {stats['stars']} | {stats['forks']} |"
    )


def render_languages(data: dict[str, Any]) -> str:
    languages = data["languages"]
    if not languages:
        return "Language data is not available yet."
    items = [f"{item['name']} ({item['percent']:.1f}%)" for item in languages]
    summary = ", ".join(items)
    return (
        f"{summary}<br>\n"
        f"*Measured across the {data['language_repository_count']} most recently updated public repositories.*"
    )


def render_recent_repositories(repositories: list[dict[str, Any]]) -> str:
    if not repositories:
        return "No public repositories were returned by the GitHub API."
    rows = [
        "| Repository | Language | Updated | Description |",
        "| --- | --- | ---: | --- |",
    ]
    for repository in repositories:
        rows.append(
            f"| {repository_link(repository)} | {markdown_cell(repository.get('language') or '-')} | "
            f"{short_date(repository.get('updated_at'))} | {markdown_cell(repository.get('description') or '-')} |"
        )
    return "\n".join(rows)


def render_latest_commits(commits: list[dict[str, Any]]) -> str:
    if not commits:
        return "No commit data is available for the tracked repositories yet."
    rows = ["| Repository | Latest commit | Date |", "| --- | --- | ---: |"]
    for item in commits:
        message = markdown_cell(item["message"] or "(no commit message)")
        commit_link = f"[{item['sha'] or 'commit'}]({item['url']}) {message}"
        rows.append(
            f"| {repository_link(item['repository'])} | {commit_link} | {short_date(item['date'])} |"
        )
    return "\n".join(rows)


def render_focus_areas(focus_areas: list[str]) -> str:
    return "\n".join(f"- {markdown_cell(area)}" for area in focus_areas)


def render_activity_entry(
    today: str,
    slot: str,
    slot_label: str,
    data: dict[str, Any],
    config: dict[str, Any],
) -> str:
    stats = data["stats"]
    if data["recent_repositories"]:
        recent_lines = [
            f"  - {repository_link(repository)} (updated {short_date(repository.get('updated_at'))})"
            for repository in data["recent_repositories"]
        ]
        recent = "\n".join(recent_lines)
    else:
        recent = "  - No public repositories were returned by the GitHub API."

    focus = ", ".join(config["focus_areas"])
    return f"""{entry_marker(today, slot)}
## {today} - {slot_label} Manila

- **Current date:** {today} (Manila)
- **Run time:** {slot_label} Manila
- **Repository count:** {stats['repository_count']} tracked public repositories
- **Recent repositories:**
{recent}
- **Focus areas:** {focus}
- **GitHub profile statistics:** {stats['public_repository_count']} public repositories, {stats['followers']} followers, {stats['following']} following, {stats['public_gists']} public gists, {stats['stars']} stars, and {stats['forks']} forks across tracked repositories
- **Engineering summary:** {data['engineering_summary']}
""".rstrip()


def upsert_daily_entry(log_text: str, entry: str, today: str, slot: str | None = None) -> str:
    marker = entry_marker(today, slot) if slot else f"<!-- dashboard-entry:{today} -->"
    start = log_text.find(marker)
    if start >= 0:
        next_entry = re.search(
            r"(?m)^<!-- dashboard-entry:\d{4}-\d{2}-\d{2}(?::\d{4})? -->",
            log_text[start + len(marker) :],
        )
        end = start + len(marker) + next_entry.start() if next_entry else len(log_text)
        suffix = "\n\n" if next_entry else ""
        updated = log_text[:start] + entry + suffix + log_text[end:]
        return updated.rstrip() + "\n"
    return log_text.rstrip() + "\n\n" + entry + "\n"


def collection_entry(
    today: str,
    slot: str,
    slot_label: str,
    repository: dict[str, Any],
    data: dict[str, Any],
    config: dict[str, Any],
) -> str:
    recent = (
        "\n".join(
            f"  - {repository_link(item)} (updated {short_date(item.get('updated_at'))})"
            for item in data["recent_repositories"]
        )
        or "  - No public repositories were returned by the GitHub API."
    )
    stats = data["stats"]
    return f"""{entry_marker(today, slot)}
## {today} - {slot_label} Manila

- **Current date:** {today} (Manila)
- **Run time:** {slot_label} Manila
- **Repository count:** {stats['repository_count']} public repositories
- **Recent repositories:**
{recent}
- **Focus areas:** {', '.join(config['focus_areas'])}
- **GitHub profile statistics:** {stats['public_repository_count']} public repositories, {stats['followers']} followers, {stats['following']} following, {stats['public_gists']} public gists, {stats['stars']} stars, and {stats['forks']} forks
- **Engineering summary:** Automated daily journal checkpoint for [{markdown_cell(repository['name'])}]({repository['html_url']}). This record keeps the journal continuous and does not imply that project source files changed today.
""".rstrip()


def sync_collection_repositories(
    read_client: GitHubClient,
    write_client: GitHubClient,
    config: dict[str, Any],
    today: str,
    slot: str,
    slot_label: str,
) -> None:
    username = urllib.parse.quote(config["github_username"], safe="")
    profile = read_client.get_json(f"/users/{username}")
    repositories = read_client.get_repositories(username)
    repositories.sort(key=lambda repo: repo.get("updated_at") or "", reverse=True)

    keywords = [value.casefold() for value in config["collection_repo_name_keywords"]]
    explicit_names = {value.casefold() for value in config.get("collection_repo_names", [])}
    excluded_names = {
        value.casefold() for value in config.get("collection_repo_excluded_names", [])
    }
    targets = [
        repository
        for repository in repositories
        if repository.get("name", "").casefold() not in excluded_names
        and (
            repository.get("name", "").casefold() in explicit_names
            or any(keyword in repository.get("name", "").casefold() for keyword in keywords)
        )
    ]
    recent_limit = max(1, int(config.get("recent_repository_limit", 5)))
    stats = {
        "repository_count": len(repositories),
        "public_repository_count": int(profile.get("public_repos", len(repositories))),
        "followers": int(profile.get("followers", 0)),
        "following": int(profile.get("following", 0)),
        "public_gists": int(profile.get("public_gists", 0)),
        "stars": sum(int(repository.get("stargazers_count", 0)) for repository in repositories),
        "forks": sum(int(repository.get("forks_count", 0)) for repository in repositories),
    }
    data = {"recent_repositories": repositories[:recent_limit], "stats": stats}
    log_path = config["collection_log_path"]
    encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in log_path.split("/"))
    failures: list[str] = []
    skipped: list[str] = []

    for repository in targets:
        endpoint = f"/repos/{repository['full_name']}/contents/{encoded_path}"
        try:
            try:
                contents = write_client.get_json(endpoint)
            except GitHubAPIError as error:
                if error.status in (401, 403):
                    skipped.append(f"{repository['full_name']}: {error.status}")
                    print(
                        f"Skipped: {repository['full_name']}/{log_path} "
                        f"(HTTP {error.status}); check REPO_ACTIVITY_TOKEN permissions."
                    )
                    continue
                if error.status != 404:
                    raise
                contents = None

            if contents:
                existing = base64.b64decode(contents["content"]).decode("utf-8")
                sha = contents["sha"]
            else:
                existing = (
                    "# Engineering Activity Journal\n\n"
                    "Automated daily snapshots for this activity-collection repository.\n"
                )
                sha = None

            entry = collection_entry(today, slot, slot_label, repository, data, config)
            updated = upsert_daily_entry(existing, entry, today, slot)
            if updated == existing:
                print(f"Unchanged: {repository['full_name']}/{log_path}")
                continue

            payload: dict[str, Any] = {
                "message": f"docs: update engineering journal {today} {slot_label} Manila",
                "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
            }
            if sha:
                payload["sha"] = sha
            try:
                write_client.put_json(endpoint, payload)
            except GitHubAPIError as error:
                if error.status in (401, 403):
                    skipped.append(f"{repository['full_name']}: {error.status}")
                    print(
                        f"Skipped: {repository['full_name']}/{log_path} "
                        f"(HTTP {error.status}); check REPO_ACTIVITY_TOKEN permissions."
                    )
                    continue
                raise
            print(f"Updated: {repository['full_name']}/{log_path}")
        except Exception as error:
            failures.append(f"{repository['full_name']}: {error}")

    exclusions = ", ".join(sorted(excluded_names)) or "none"
    print(f"Matched {len(targets)} repositories; excluded names: {exclusions}")
    if skipped:
        print(f"Skipped {len(skipped)} repository entries due to access restrictions.")
    if failures:
        raise RuntimeError(
            "Some collection repositories could not be updated: " + "; ".join(failures)
        )


def build_readme(
    template: str,
    config: dict[str, Any],
    data: dict[str, Any],
    today: str,
    slot_label: str,
) -> str:
    profile = data["profile"]
    values = {
        "DISPLAY_NAME": markdown_cell(config["display_name"]),
        "HEADLINE": markdown_cell(config["headline"]),
        "INTRO": config["intro"].strip(),
        "PROFILE_URL": f"https://github.com/{urllib.parse.quote(config['github_username'], safe='')}",
        "LOCATION_LINE": (
            f"**Location:** {markdown_cell(config['location'])}  \n"
            if config.get("location")
            else ""
        ),
        "PROFILE_STATS": render_stats(data["stats"]),
        "FOCUS_AREAS": render_focus_areas(config["focus_areas"]),
        "LANGUAGES": render_languages(data),
        "RECENT_REPOSITORIES": render_recent_repositories(data["recent_repositories"]),
        "LATEST_COMMITS": render_latest_commits(data["latest_commits"]),
        "ENGINEERING_SUMMARY": data["engineering_summary"],
        "LAST_REFRESHED": f"{today} {slot_label} Manila",
        "PROFILE_BIO": markdown_cell(
            profile.get("bio") or "Computer Engineering student building practical systems."
        ),
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    if re.search(r"\{\{[A-Z_]+\}\}", rendered):
        raise ValueError("README template contains an unrecognized placeholder")
    return rendered.rstrip() + "\n"


def write_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        print(f"Unchanged: {path.relative_to(ROOT)}")
        return False
    path.write_text(content, encoding="utf-8")
    print(f"Updated: {path.relative_to(ROOT)}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync-collection-repos-only",
        action="store_true",
        help="Update the daily journal in repositories matching configured name keywords.",
    )
    args = parser.parse_args()
    config = json.loads((DASHBOARD_DIR / "config.json").read_text(encoding="utf-8"))
    today, slot, slot_label = get_run_context()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if args.sync_collection_repos_only:
        write_token = os.environ.get("REPO_ACTIVITY_TOKEN")
        if not write_token:
            print(
                "::warning::REPO_ACTIVITY_TOKEN is not configured; "
                "collection repositories were not updated. See dashboard/CROSS_REPO_SYNC.md."
            )
            return
        sync_collection_repositories(
            GitHubClient(token),
            GitHubClient(write_token),
            config,
            today,
            slot,
            slot_label,
        )
        return

    template = (DASHBOARD_DIR / "README_TEMPLATE.md").read_text(encoding="utf-8")
    data = collect_dashboard_data(GitHubClient(token), config, today)
    readme = build_readme(template, config, data, today, slot_label)
    log_path = DASHBOARD_DIR / "activity-log.md"
    log = log_path.read_text(encoding="utf-8")
    entry = render_activity_entry(today, slot, slot_label, data, config)
    log = upsert_daily_entry(log, entry, today, slot)

    write_if_changed(ROOT / "README.md", readme)
    write_if_changed(log_path, log)


if __name__ == "__main__":
    main()
