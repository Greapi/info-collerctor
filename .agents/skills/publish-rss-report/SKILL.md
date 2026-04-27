---
name: publish-rss-report
description: Build and publish this repository's RSS daily report site to GitHub Pages. Use when Codex is asked to publish RSS reports, update the GitHub Pages diary/report site, run the local report generation flow, build docs/ from reports/*.md, commit report/site changes, or push a freshly generated daily report from this repository.
---

# Publish RSS Report

## Overview

Use this skill to publish the RSS daily report site by running local generation, rebuilding the static GitHub Pages files, and pushing the resulting changes. Keep secrets and long-running collection work local; GitHub Pages only serves generated static HTML from `docs/`.

## Repository Assumptions

- Work from the repository root.
- Source reports live in `reports/*.md`.
- Static GitHub Pages output lives in `docs/`.
- `build_site.py` converts reports into `docs/index.html` and `docs/reports/*.html`.
- Local RSS/model configuration stays in `.env`, `.rss_collector.sqlite3`, and `rss-downloads/`; do not stage these private/local files.

## Workflow

1. Inspect state:
   - Run `git status --short`.
   - Notice existing uncommitted files before changing anything. Do not revert user changes.

2. Generate or refresh the report only when requested:
   - For normal publish without email, run `python3 daily_workflow.py --no-send`.
   - If the user specifies a date, pass `--date YYYY-MM-DD`.
   - If the user asks only to rebuild the site from existing reports, skip `daily_workflow.py`.

3. Build the static site:
   - Run `python3 build_site.py`.
   - Verify it reports the number of generated pages.
   - Optionally spot-check `docs/index.html` or one `docs/reports/YYYY-MM-DD.html` when the output changed in a surprising way.

4. Review changes:
   - Run `git status --short`.
   - Use `git diff --stat` or targeted `git diff` to summarize changed files.
   - Expected staged candidates are `reports/*.md`, `docs/index.html`, `docs/reports/*.html`, and possibly `README.md` or `build_site.py` if the publishing system itself was edited.

5. Commit and push when the user asked to publish:
   - Stage only relevant report and site files, for example `git add reports docs`.
   - If `build_site.py` or docs were intentionally changed, stage them too.
   - Commit with a date-specific message such as `Add daily report 2026-04-27`.
   - Push the current branch with `git push`.
   - After successful staging, commit, or push, emit the corresponding Codex app git directive in the final answer.

## Guardrails

- Never stage `.env`, `.env.rsshub`, `.rss_collector.sqlite3`, `rss-downloads/`, `logs/`, `__pycache__/`, or `.DS_Store`.
- Do not run destructive cleanup commands unless the user explicitly requests them.
- If model/RSS collection fails because of missing credentials or network errors, explain the blocker and still offer to rebuild/publish existing reports if useful.
- If GitHub Pages has not been enabled yet, tell the user to set `Settings -> Pages -> Deploy from a branch -> main/master -> /docs`.
- Keep the final response short: report what was generated, what was committed/pushed, and any required manual GitHub Pages setup.
