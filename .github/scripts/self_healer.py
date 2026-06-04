import os
import glob
import subprocess
import re
import json
import anthropic
import xml.etree.ElementTree as ET
from pathlib import Path
from git_manager import GitManager
from typing import List, Dict, Optional
import logging
import shutil

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    print("Error: ANTHROPIC_API_KEY environment variable is not set.")
    exit(1)

client = anthropic.Anthropic(api_key=api_key)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def load_skills(file_path=".github/scripts/ai-skills.json"):
    if os.path.exists(file_path):
        logger.info(f"Loaded AI skills from {file_path}")
        with open(file_path, 'r') as file:
            return json.load(file)
    else:
        logger.warning(f"{file_path} not found. Proceeding with default skills.")
        return {
            "model_version": "claude-sonnet-4-6",
            "test_reports_glob": "reports/test-results.xml",
            "target_exceptions": [],
            "file_extraction_rules": [
                "Look for the highest user-created modules in the traceback.",
                "Ignore standard library modules and third-party framework internals."
            ]
        }

def get_coding_standards(file_path=".github/scripts/coding-standards.md"):
    if os.path.exists(file_path):
        logger.info(f"Loaded coding standards from {file_path}")
        with open(file_path, 'r') as file:
            return file.read()
    else:
        logger.warning(f"{file_path} not found. Proceeding with default AI knowledge.")
        return "Apply general modern Python best practices."

def build_dynamic_context(skills: dict) -> str:
    context_blocks = []
    for key, rules in skills.items():
        if isinstance(rules, list) and rules:
            category_title = key.replace('_', ' ').upper()
            rules_str = "\n".join([f"- {rule}" for rule in rules])
            context_blocks.append(f"### {category_title} ###\n{rules_str}")
    return "\n\n".join(context_blocks)

def find_exception_in_reports(target_exceptions, reports_glob):
    """Scans pytest JUnit XML reports for test failures."""
    reports = glob.glob(reports_glob)

    fallback_content, fallback_exc_type = None, None

    for report in reports:
        if not os.path.isfile(report):
            continue
        try:
            tree = ET.parse(report)
            root = tree.getroot()

            for testcase in root.findall('.//testcase'):
                for tag in ('failure', 'error'):
                    node = testcase.find(tag)
                    if node is None:
                        continue
                    content = (node.get('message', '') + '\n' + (node.text or '')).strip()

                    for exc_type in target_exceptions:
                        if exc_type in content:
                            logger.info(f"Found targeted {exc_type} in {report}")
                            return content, exc_type

                    if not fallback_exc_type:
                        match = re.search(r'([A-Z][a-zA-Z]*(?:Error|Exception))', content)
                        if match:
                            fallback_exc_type = match.group(1)
                            fallback_content = content

        except ET.ParseError:
            with open(report, 'r') as f:
                content = f.read()
            match = re.search(r'([A-Z][a-zA-Z]*(?:Error|Exception))', content)
            if match and not fallback_exc_type:
                fallback_exc_type = match.group(1)
                fallback_content = content

    return fallback_content, fallback_exc_type

def get_failing_files_from_ai(stack_trace: str, skills: dict) -> List[str]:
    """Uses Claude to identify failing Python source files from the traceback."""
    dynamic_knowledge_base = build_dynamic_context(skills)
    model_version = skills.get("model_version", "claude-sonnet-4-6")

    prompt = f"""
    Analyze the following Python traceback or test failure output to identify the project source files that need to be modified.

    CRITICAL EXTRACTION CONSTRAINTS:
    1. Your primary goal is to find the broken APPLICATION source code (not test files, not third-party libraries).
    2. DO NOT return test files (e.g., test_*.py or *_test.py) just because the test failed. Trace the error to the actual module causing the failure.
    3. Ignore standard library modules and installed packages.

    {dynamic_knowledge_base}

    Stack Trace / Error Log:
    {stack_trace}

    Return ONLY a raw JSON list of exact file names with their extensions (e.g., ["order_service.py", "order.py"]). Do not output markdown blocks or any other text.
    """

    message = client.messages.create(
        model=model_version,
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}]
    )

    try:
        raw_output = message.content[0].text.strip().replace("```json", "").replace("```", "")
        file_names = json.loads(raw_output)

        found_paths = []
        for file_name in file_names:
            for root, dirs, files in os.walk('.'):
                if file_name in files:
                    found_paths.append(os.path.join(root, file_name))
                    break

        return found_paths
    except json.JSONDecodeError:
        logger.error(f"Failed to parse AI file identification response: {message.content[0].text}")
        return []

def generate_fix(file_path, stack_trace, exc_type, coding_standards, skills):
    """Asks Claude to fix the exception using external coding standards and skill rules."""
    with open(file_path, 'r') as file:
        python_code = file.read()

    dynamic_knowledge_base = build_dynamic_context(skills)
    model_version = skills.get("model_version", "claude-sonnet-4-6")

    system_instructions = f"""
    You are a Senior Python Staff Engineer resolving CI/CD pipeline failures.
    You must strictly adhere to the following Team Coding Standards.

    CRITICAL CONSTRAINTS:
    1. NEVER delete, skip, or comment out test cases to resolve a failure.
    2. If you are given a test file to fix, you may ONLY update assertions, mock behaviors, or fix syntax errors. You CANNOT remove test logic.
    3. Your primary goal is to fix the underlying source code logic to make the tests pass naturally.

    ### TEAM CODING STANDARDS ###
    {coding_standards}

    {dynamic_knowledge_base}
    """

    user_prompt = f"""
    The following code raises a {exc_type}.

    Error Log / Stack Trace:
    {stack_trace}

    Python Code (File: {file_path}):
    {python_code}

    Fix the {exc_type} in the code addressing the root cause indicated by the traceback.
    Return ONLY the raw, updated Python code. Do not include markdown formatting like ```python.
    """

    message = client.messages.create(
        model=model_version,
        max_tokens=4000,
        system=system_instructions,
        messages=[{"role": "user", "content": user_prompt}]
    )

    return message.content[0].text.replace('```python', '').replace('```', '').strip()

def create_pr_and_commit(git_manager: GitManager, fixes_applied: List[Dict]) -> Optional[str]:
    if not fixes_applied:
        return None

    fixed_files = [fix["file"] for fix in fixes_applied]

    if not git_manager.has_uncommitted_changes(fixed_files):
        logger.info("No changes detected")
        return None

    source_branch = (
        os.environ.get("GITHUB_HEAD_REF")
        or os.environ.get("GITHUB_REF_NAME")
        or git_manager._get_current_branch()
    )

    branch_name, created_now = git_manager.create_branch()

    if not git_manager.commit_changes(files=fixed_files):
        return None

    git_manager.push_branch(branch_name)

    pr_state = git_manager.get_pr_state(branch_name)

    if created_now:
        return git_manager.create_pr(
            branch_name=branch_name,
            files_changed=fixed_files,
            base_branch=source_branch
        )

    if pr_state == "OPEN":
        logger.info("Existing PR is open — branch updated automatically.")
        return git_manager.get_existing_pr_url(branch_name)

    logger.info("Branch exists but PR is merged/closed — creating a new PR.")
    return git_manager.create_pr(
        branch_name=branch_name,
        files_changed=fixed_files,
        base_branch=source_branch
    )


if __name__ == "__main__":
    workspace = Path(os.getcwd())
    git_manager = GitManager(workspace)

    modified_files_map = {}

    skills = load_skills(".github/scripts/ai-skills.json")
    standards = get_coding_standards(".github/scripts/coding-standards.md")

    MAX_RETRIES = skills.get("max_retries", 3)
    reports_glob = skills.get("test_reports_glob", "reports/test-results.xml")
    reports_dir = os.path.dirname(reports_glob) or "."

    attempt = 1
    success = False
    compilation_error_output = None

    logger.info(f"Starting Self-Healing Loop (Max Attempts: {MAX_RETRIES})")

    while attempt <= MAX_RETRIES:
        logger.info(f"--- Attempt {attempt} of {MAX_RETRIES} ---")

        if compilation_error_output:
            logger.info("Using error output from previous attempt...")
            stack_trace = compilation_error_output
            exc_type = "Python Error"
            compilation_error_output = None
        else:
            stack_trace, exc_type = find_exception_in_reports(skills.get("target_exceptions", []), reports_glob)

            if not stack_trace or not exc_type:
                if attempt == 1:
                    logger.info("No target exceptions or test failures detected in initial reports.")
                else:
                    logger.info("No more exceptions found. Fix appears successful!")
                    success = True
                break

        logger.info(f"Analyzing cause: {exc_type}")
        failing_files = get_failing_files_from_ai(stack_trace, skills)

        if not failing_files:
            logger.warning("Could not map traceback to local files. Breaking loop.")
            break

        logger.info(f"AI identified failing files: {failing_files}. Generating fixes...")

        for file_path in failing_files:
            fixed_code = generate_fix(file_path, stack_trace, exc_type, standards, skills)
            with open(file_path, "w") as file:
                file.write(fixed_code)
                print(f"fix applied to {file_path}:\n", fixed_code)
            modified_files_map[file_path] = exc_type

        if os.path.exists(reports_dir):
            shutil.rmtree(reports_dir)
            logger.info(f"Cleaned up old test reports at {reports_dir}.")

        logger.info("Running pytest to validate fixes...")
        os.makedirs(reports_dir, exist_ok=True)
        test_result = subprocess.run(
            ["pytest", f"--junitxml={reports_glob}", "-v"],
            capture_output=True,
            text=True
        )

        if test_result.returncode == 0:
            logger.info("Tests passed successfully!")
            success = True
            break
        else:
            logger.error(f"Fix validation failed on attempt {attempt}.")
            if not glob.glob(reports_glob):
                logger.error("No test report generated. Extracting pytest output for AI correction.")
                compilation_error_output = (test_result.stdout + test_result.stderr)[-4000:]
            attempt += 1

    if success and modified_files_map:
        logger.info("Generating Pull Request with all accumulated fixes...")
        fixes_applied = [{"file": path, "exception": exc} for path, exc in modified_files_map.items()]
        pr_url = create_pr_and_commit(git_manager, fixes_applied)
        if pr_url:
            print(f"PR Created: {pr_url}")
        else:
            print("Failed to create PR.")
    elif not success and modified_files_map:
        logger.error("All AI attempts failed. The local files were modified, but tests are still failing. No PR will be created.")
