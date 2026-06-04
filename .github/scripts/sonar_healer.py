import os
import sys
import subprocess
import time
import requests
import anthropic
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from git_manager import GitManager

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SONAR_TOKEN = os.environ.get("SONAR_TOKEN")
SONAR_HOST_URL = os.environ.get("SONAR_HOST_URL", "https://sonarcloud.io")
SONAR_PROJECT_KEY = os.environ.get("SONAR_PROJECT_KEY")

if not all([ANTHROPIC_API_KEY, SONAR_TOKEN, SONAR_PROJECT_KEY]):
    print("Error: ANTHROPIC_API_KEY, SONAR_TOKEN, and SONAR_PROJECT_KEY must all be set.")
    exit(1)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def get_coding_standards(file_path=".github/scripts/coding-standards.md"):
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return f.read()
    return "Apply general modern Python best practices."


def run_sonar_scan():
    print("Starting SonarCloud analysis via sonar-scanner...")

    cmd = [
        "sonar-scanner",
        f"-Dsonar.token={SONAR_TOKEN}",
        f"-Dsonar.host.url={SONAR_HOST_URL}",
        f"-Dsonar.projectKey={SONAR_PROJECT_KEY}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("Warning: sonar-scanner exited with non-zero status. Proceeding anyway.")
        print(f"stdout: {result.stdout[-2000:]}")
        print(f"stderr: {result.stderr[-1000:]}")
    else:
        print("SonarCloud scan submitted successfully.")


def wait_for_analysis(report_task_path=".scannerwork/report-task.txt", timeout=300, poll_interval=10):
    """Reads the ceTaskId from the sonar-scanner report and polls until analysis is complete."""
    if not os.path.exists(report_task_path):
        print(f"Warning: {report_task_path} not found. Skipping analysis wait.")
        return

    task_id = None
    with open(report_task_path, 'r') as f:
        for line in f:
            if line.startswith("ceTaskId="):
                task_id = line.strip().split("=", 1)[1]
                break

    if not task_id:
        print("Warning: ceTaskId not found in report-task.txt. Skipping analysis wait.")
        return

    print(f"Waiting for SonarCloud analysis task {task_id} to complete...")
    url = f"{SONAR_HOST_URL}/api/ce/task"
    deadline = time.time() + timeout

    while time.time() < deadline:
        response = requests.get(url, params={"id": task_id}, auth=(SONAR_TOKEN, ""))
        response.raise_for_status()
        status = response.json().get("task", {}).get("status", "")
        print(f"  Analysis status: {status}")

        if status == "SUCCESS":
            print("Analysis complete.")
            return
        elif status in ("FAILED", "CANCELLED"):
            print(f"Analysis ended with status: {status}. Proceeding anyway.")
            return

        time.sleep(poll_interval)

    print(f"Timed out after {timeout}s waiting for analysis. Proceeding anyway.")


def fetch_sonar_issues():
    """Fetches open bugs, vulnerabilities, and code smells from SonarCloud."""
    url = f"{SONAR_HOST_URL}/api/issues/search"
    params = {
        "componentKeys": SONAR_PROJECT_KEY,
        "resolved": "false",
        "types": "BUG,VULNERABILITY,CODE_SMELL",
        "ps": 100,
    }
    response = requests.get(url, params=params, auth=(SONAR_TOKEN, ""))
    response.raise_for_status()
    issues = response.json().get("issues", [])
    print(f"Found {len(issues)} open Sonar issues.")
    return issues


def group_issues_by_file(issues):
    grouped = {}
    for issue in issues:
        component = issue.get("component", "")
        file_path = component.split(":")[-1] if ":" in component else component
        if file_path not in grouped:
            grouped[file_path] = []
        grouped[file_path].append(issue)
    return grouped


def format_issues_for_prompt(issues):
    lines = []
    for i in issues:
        rule = i.get("rule", "")
        message = i.get("message", "")
        line = i.get("line", "?")
        severity = i.get("severity", "")
        issue_type = i.get("type", "")
        lines.append(f"  - Line {line} [{severity} {issue_type}] ({rule}): {message}")
    return "\n".join(lines)


def generate_fix(file_path, issues, coding_standards):
    """Asks Claude to fix all Sonar issues in a Python file."""
    if not os.path.exists(file_path):
        print(f"  Skipping {file_path} — file not found locally.")
        return None

    with open(file_path, 'r') as f:
        source_code = f.read()

    issues_text = format_issues_for_prompt(issues)

    system_prompt = f"""You are a Senior Python Staff Engineer fixing SonarCloud issues in a CI/CD pipeline.
You must strictly follow these team coding standards:

### TEAM CODING STANDARDS ###
{coding_standards}
"""

    user_prompt = f"""Fix all of the following SonarCloud issues in this Python file.

File: {file_path}

Issues to fix:
{issues_text}

Current source code:
{source_code}

Return ONLY the raw updated Python code. Do not include markdown formatting like ```python.
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}]
    )

    return message.content[0].text.replace("```python", "").replace("```", "").strip()


def commit_fixes(changed_files):
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    git = GitManager(workspace)

    source_branch = os.environ.get("GITHUB_REF_NAME", "main")
    print(f"Source branch: {source_branch}")

    branch_name, created_now = git.create_branch()
    print(f"Created new branch: {branch_name}")

    committed = git.commit_changes(
        files=changed_files,
        message="fix: AI auto-fix for SonarCloud issues"
    )

    if not committed:
        print("No changes to commit.")
        return

    git.push_branch(branch_name)
    print(f"Fixes committed and pushed to branch '{branch_name}'.")

    pr_url = git.create_pr(
        branch_name=branch_name,
        files_changed=changed_files,
        base_branch=source_branch
    )

    if pr_url:
        print(f"PR Created: {pr_url}")
    else:
        print("PR creation failed or PR already exists.")


if __name__ == "__main__":
    print(f"Starting Sonar Self-Healing for project: {SONAR_PROJECT_KEY}")

    coding_standards = get_coding_standards()
    run_sonar_scan()
    wait_for_analysis()
    issues = fetch_sonar_issues()

    if not issues:
        print("No open Sonar issues found. Nothing to fix.")
        exit(0)

    grouped = group_issues_by_file(issues)
    changed_files = []

    for file_path, file_issues in grouped.items():
        print(f"\nFixing {len(file_issues)} issue(s) in {file_path}...")
        fixed_code = generate_fix(file_path, file_issues, coding_standards)

        if fixed_code:
            with open(file_path, 'w') as f:
                f.write(fixed_code)
            print(f"  Written fix for {file_path}")
            changed_files.append(file_path)

    if changed_files:
        print(f"\nFixed {len(changed_files)} file(s). Committing fixes...")
        commit_fixes(changed_files)
    else:
        print("No local files were fixable.")
