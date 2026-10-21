# Cross-Repository Journal Sync

The scheduled workflow writes journal snapshots to `.github/engineering-activity-log.md` only in public repositories whose names contain `Activities` or `Lab` (case-insensitive), except explicitly excluded repositories. It runs three times daily in Manila time: 1:45 AM, 9:45 AM, and 5:45 PM. Each target gets one entry per scheduled slot; rerunning a slot updates that entry instead of creating a duplicate.

## Dashboard Destination

The workflow runs in `Comp-Alex/Engineering-Activity-Log`. Its main job updates this repository's root `README.md` and `dashboard/activity-log.md`. The cross-repository step separately writes journal entries to matching activity repositories. No other profile repository is a destination.

The snapshot commit is an automated journal checkpoint, not a claim that source code changed. Other repositories are read for the profile summary but are not modified unless their names match the configured keywords.

## Required Secret

Cross-repository writes require a fine-grained personal access token because the workflow's built-in `GITHUB_TOKEN` can write only to this profile repository.

1. Create a fine-grained token owned by `Comp-Alex` and select only the repositories that should receive journal entries.
2. Grant **Contents: Read and write**. Repository metadata read access is included automatically.
3. In `Comp-Alex/Engineering-Activity-Log`, open **Settings → Secrets and variables → Actions** and add a repository secret named `REPO_ACTIVITY_TOKEN` with the token value.

Do not put the token in `config.json` or commit it to the repository. If the secret is missing, this repository's dashboard still updates and the workflow reports that cross-repository sync was skipped.