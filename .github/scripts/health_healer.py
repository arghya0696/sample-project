import os
import sys
import json
import requests
import anthropic
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from git_manager import GitManager

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GITHUB_WORKSPACE  = os.environ.get("GITHUB_WORKSPACE", ".")


def load_app_properties(path="src/main/resources/application.properties"):
    """Reads key=value pairs from application.properties."""
    props = {}
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    props[key.strip()] = value.strip()
    return props


_props = load_app_properties()
APP_HEALTH_URL = os.environ.get("APP_HEALTH_URL") or _props.get("app.health.url")
APP_LOGS_URL   = os.environ.get("APP_LOGS_URL")   or _props.get("app.logs.url")
APP_LANGUAGE   = os.environ.get("APP_LANGUAGE")   or _props.get("app.language", "Java")

if not all([ANTHROPIC_API_KEY, APP_HEALTH_URL, APP_LOGS_URL]):
    print("Error: ANTHROPIC_API_KEY, APP_HEALTH_URL, and APP_LOGS_URL must all be set.")
    print("  Set them as env vars or add app.health.url / app.logs.url to application.properties.")
    exit(1)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def check_health():
    """Returns True if the app is healthy."""
    try:
        response = requests.get(APP_HEALTH_URL, timeout=10)
        if response.ok:
            body = {}
            if "application/json" in response.headers.get("Content-Type", ""):
                try:
                    body = response.json()
                except Exception:
                    pass
            status = body.get("status", "UP")
            if status == "UP":
                print(f"Health check passed: {response.status_code} status={status}")
                return True
        print(f"Health check failed: {response.status_code} {response.text[:200]}")
        return False
    except requests.exceptions.ConnectionError:
        print(f"Health check unreachable: could not connect to {APP_HEALTH_URL}")
        return False
    except requests.exceptions.Timeout:
        print(f"Health check timed out after 10s: {APP_HEALTH_URL}")
        return False
    except Exception as e:
        print(f"Health check error: {e}")
        return False


def fetch_logs(max_lines=200):
    """Fetches the last N lines from the logs URL."""
    try:
        response = requests.get(APP_LOGS_URL, timeout=15)
        if not response.ok:
            print(f"Failed to fetch logs: {response.status_code} {response.text[:200]}")
            return None
        lines = response.text.strip().splitlines()
        if not lines:
            print("Logs endpoint returned empty response.")
            return None
        tail = "\n".join(lines[-max_lines:])
        print(f"Fetched {len(lines)} log lines (using last {min(len(lines), max_lines)}).")
        return tail
    except requests.exceptions.ConnectionError:
        print(f"Could not connect to logs URL: {APP_LOGS_URL}")
        return None
    except requests.exceptions.Timeout:
        print(f"Logs fetch timed out after 15s: {APP_LOGS_URL}")
        return None
    except Exception as e:
        print(f"Error fetching logs: {e}")
        return None


def get_coding_standards(file_path=".github/scripts/coding-standards.md"):
    try:
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return f.read()
    except Exception as e:
        print(f"Could not read coding standards: {e}")
    return f"Apply general modern {APP_LANGUAGE} best practices."


def analyze_logs(logs):
    """Uses Claude to identify root cause and affected source files from logs."""
    try:
        prompt = f"""Analyze the following application logs from a {APP_LANGUAGE} project.
Identify:
1. The root cause of the failure.
2. The exact source files that need to be fixed (not test files, not framework internals).

Logs:
{logs}

Return ONLY a raw JSON object with two keys:
- "error_summary": one sentence describing the root cause
- "files": list of exact file names with extensions (e.g. ["OrderService.java"] or ["app.py"])

Do not output markdown or any other text.
"""
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip().replace("```json", "").replace("```", "")
        result = json.loads(raw)
        print(f"Root cause: {result.get('error_summary', 'unknown')}")
        return result.get("files", []), result.get("error_summary", "")
    except json.JSONDecodeError as e:
        print(f"AI returned invalid JSON: {e}\nRaw output: {message.content[0].text}")
        return [], ""
    except Exception as e:
        print(f"Log analysis failed: {e}")
        return [], ""


def find_file_paths(file_names):
    """Walks the workspace to resolve file names to full relative paths."""
    found = []
    try:
        for name in file_names:
            for root, _, files in os.walk("."):
                if name in files:
                    found.append(os.path.join(root, name))
                    break
            else:
                print(f"  Warning: could not find '{name}' in workspace.")
    except Exception as e:
        print(f"Error walking workspace: {e}")
    return found


def generate_fix(file_path, logs, error_summary, coding_standards):
    """Asks Claude to fix the production issue in a source file."""
    try:
        with open(file_path, "r") as f:
            source_code = f.read()

        system_prompt = f"""You are a Senior {APP_LANGUAGE} Engineer fixing a production issue identified from application logs.
You must strictly follow these coding standards:

### CODING STANDARDS ###
{coding_standards}
"""

        user_prompt = f"""The application is unhealthy in production.

Error summary: {error_summary}

Relevant log output:
{logs}

Fix the issue in the file below. Make the smallest change needed to resolve the root cause.

File: {file_path}
Source code:
{source_code}

Return ONLY the raw fixed source code. Do not include markdown formatting.
"""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        return message.content[0].text.replace("```", "").strip()
    except FileNotFoundError:
        print(f"  File not found: {file_path}")
        return None
    except Exception as e:
        print(f"  Fix generation failed for {file_path}: {e}")
        return None


def commit_fixes(changed_files):
    """Creates a health-fix branch, commits fixes, pushes, and opens a PR."""
    try:
        workspace = Path(GITHUB_WORKSPACE)
        git = GitManager(workspace)

        source_branch = os.environ.get("GITHUB_REF_NAME", "master")
        print(f"Source branch: {source_branch}")

        branch_name, created_now = git.create_branch(prefix="health-fix")
        print(f"On branch: {branch_name}")

        committed = git.commit_changes(
            files=changed_files,
            message="fix: AI health healer auto-fix for production issue"
        )

        if not committed:
            print("No changes to commit.")
            return

        git.push_branch(branch_name)

        if created_now:
            pr_url = git.create_pr(
                branch_name=branch_name,
                files_changed=changed_files,
                base_branch=source_branch
            )
            if pr_url:
                print(f"PR created: {pr_url}")
            else:
                print("PR creation failed.")
        else:
            print(f"Pushed fixes to existing branch '{branch_name}'.")
    except Exception as e:
        print(f"Failed to commit and push fixes: {e}")


if __name__ == "__main__":
    try:
        print(f"Starting Health Healer | language={APP_LANGUAGE}")
        print(f"Health URL : {APP_HEALTH_URL}")
        print(f"Logs URL   : {APP_LOGS_URL}")

        if check_health():
            print("App is healthy. Nothing to do.")
            exit(0)

        print("App is unhealthy. Fetching logs...")
        logs = fetch_logs()

        if not logs:
            print("Could not fetch logs. Cannot proceed.")
            exit(1)

        coding_standards = get_coding_standards()

        print("Analyzing logs with AI...")
        file_names, error_summary = analyze_logs(logs)

        if not file_names:
            print("AI could not identify any files to fix. Check logs manually.")
            exit(1)

        print(f"AI identified files to fix: {file_names}")
        file_paths = find_file_paths(file_names)

        if not file_paths:
            print("Could not locate identified files in workspace. Cannot proceed.")
            exit(1)

        changed_files = []
        for file_path in file_paths:
            print(f"\nGenerating fix for {file_path}...")
            fixed_code = generate_fix(file_path, logs, error_summary, coding_standards)
            if fixed_code is None:
                print(f"  Skipping {file_path} — fix generation failed.")
                continue
            with open(file_path, "w") as f:
                f.write(fixed_code)
            print(f"Fix applied to {file_path}.")
            changed_files.append(file_path)

        if changed_files:
            print(f"\nFixed {len(changed_files)} file(s). Creating PR...")
            commit_fixes(changed_files)
        else:
            print("No files were fixed.")

    except Exception as e:
        print(f"Health healer encountered an unexpected error: {e}")
        exit(1)