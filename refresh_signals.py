name: Refresh signals and dashboard

on:
  schedule:
    - cron: "2-59/5 * * * *"   # every 5 minutes, every day (2 minutes after the other jobs)
  workflow_dispatch: {}

permissions:
  contents: write

concurrency:
  group: signals
  cancel-in-progress: false

jobs:
  signals:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Build Congress, signals and dashboard
        run: python refresh_signals.py
      - name: Commit changes
        run: |
          git config user.name "koochooloo-bot"
          git config user.email "actions@users.noreply.github.com"
          git add data
          git diff --staged --quiet || (git commit -m "Signals refresh $(date -u +%Y-%m-%dT%H:%MZ)" && git pull --rebase -q && git push)
