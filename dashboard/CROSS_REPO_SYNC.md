# Cross-Repository Journal Sync

The scheduled workflow writes a daily journal snapshot to `.github/engineering-activity-log.md` only in public repositories whose names contain `Activities` or `Lab` (case-insensitive). Each target repository receives at most one entry per UTC date. Re-running the workflow on the same day updates that date instead of making another journal commit.

## Workflow Location

This checkout's Git remote is `Comp-Alex/Engineering-Activity-Log`. The workflow's normal README and activity-log commit therefore updates that repository. It does not update the profile README in `Comp-Alex/Comp-Alex`; to do that, install the workflow in the profile repository or add an explicit profile-repository sync.

The snapshot commit is an automated journal checkpoint, not a claim that source code changed. Other repositories are read for the profile summary but are not modified unless their names match the configured keywords.

## Required Secret

Cross-repository writes require a fine-grained personal access token because the workflow's built-in `GITHUB_TOKEN` can write only to this profile repository.

1. Create a fine-grained token owned by `Comp-Alex` and select only the repositories that should receive journal entries.
2. Grant **Contents: Read and write**. Repository metadata read access is included automatically.
3. In the repository that contains this workflow, open **Settings → Secrets and variables → Actions** and add a repository secret named `REPO_ACTIVITY_TOKEN` with the token value. For this checkout, that is `Comp-Alex/Engineering-Activity-Log`.

Do not put the token in `config.json` or commit it to the repository. If the secret is missing, the profile dashboard still updates and the workflow reports that cross-repository sync was skipped.