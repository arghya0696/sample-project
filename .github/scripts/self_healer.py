import os
import glob
import subprocess
import re
import json
import anthropic
from pathlib import Path
from git_manager import GitManager
from typing import List, Dict, Optional
import logging
import shutil

# 1. Initialize the Anthropic client
api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    print("Error: ANTHROPIC_API_KEY environment variable is not set.")
    exit(1)

client = anthropic.Anthropic(api_key=api_key)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def load_skills(file_path=".github/scripts/ai-skills.json"):
    """Loads the AI skills from an external JSON file."""
    if os.path.exists(file_path):
        logger.info(f"Loaded AI skills from {file_path}")
        with open(file_path, 'r') as file:
            return json.load(file)
    else:
        logger.warning(f"{file_path} not found. Proceeding with default skills.")
        return {
            "model_version": "claude-sonnet-4-6",
            "test_reports_glob": "target/surefire-reports/*.txt",
            "target_exceptions": [],
            "file_extraction_rules": [
                "Look for the highest user-created classes in the execution stack.",
                "Ignore standard Java libraries (java.base) and framework internal classes."
            ]
        }

def get_coding_standards(file_path=".github/scripts/coding-standards.md"):
    """Reads the coding standards from an external markdown file."""
    if os.path.exists(file_path):
        logger.info(f"Loaded coding standards from {file_path}")
        with open(file_path, 'r') as file:
            return file.read()
    else:
        logger.warning(f"{file_path} not found. Proceeding with default AI knowledge.")
        return "Apply general modern Java best practices."

def build_dynamic_context(skills: dict) -> str:
    """Dynamically builds prompt context from all list-based rules in the skills JSON."""
    context_blocks = []

    for key, rules in skills.items():
        if isinstance(rules, list) and rules:
            category_title = key.replace('_', ' ').upper()
            rules_str = "\n".join([f"- {rule}" for rule in rules])
            context_blocks.append(f"### {category_title} ###\n{rules_str}")

    return "\n\n".join(context_blocks)

def find_exception_in_reports(target_exceptions, reports_glob):
    """Scans test reports for target exceptions."""
    reports = glob.glob(reports_glob)

    fallback_content, fallback_exc_type = None, None

    for report in reports:
        if not os.path.isfile(report): continue
        with open(report, 'r') as file:
            content = file.read()

            for exc_type in target_exceptions:
                if exc_type in content:
                    logger.info(f"Found targeted {exc_type} in report: {report}")
                    return content, exc_type

            if not fallback_exc_type:
                match = re.search(r'([a-zA-Z0-9_.]+(?:Exception|Error|Failure))', content)
                if match:
                    fallback_exc_type = match.group(1)
                    fallback_content = content

    return fallback_content, fallback_exc_type

def get_failing_files_from_ai(stack_trace: str, skills: dict) -> List[str]:
    """Uses Claude to intelligently identify ALL failing files from the stack trace or compiler log."""
    dynamic_knowledge_base = build_dynamic_context(skills)
    model_version = skills.get("model_version", "claude-3-5-sonnet-20241022")

    prompt = f"""
    Analyze the following Java stack trace or compilation error log to identify the main project source files that need to be modified.
    
    CRITICAL EXTRACTION CONSTRAINTS:
    1. Your primary goal is to find the broken APPLICATION source code (e.g., files in src/main/java).
    2. DO NOT return Test classes (e.g., GithubActionDemoApplicationTests.java) just because the test failed. You must trace the error down the stack to find the actual service or component causing the failure.
    
    {dynamic_knowledge_base}
    
    Stack Trace / Error Log:
    {stack_trace}
    
    Return ONLY a raw JSON list of exact file names with their extensions (e.g., ["NPETestServiceImpl.java", "MyConfig.java"]). Do not output markdown blocks or any other text.
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
        java_code = file.read()

    dynamic_knowledge_base = build_dynamic_context(skills)
    model_version = skills.get("model_version", "claude-3-5-sonnet-20241022")

    system_instructions = f"""
    You are a Senior Java Staff Engineer resolving CI/CD pipeline failures. 
    You must strictly adhere to the following Team Coding Standards.
    
    CRITICAL CONSTRAINTS:
    1. NEVER delete, skip, or comment out test cases (e.g., `@Test` methods) to resolve a failure. 
    2. If you are explicitly provided a Test file to fix, you may ONLY update assertions, mock behaviors, or fix syntax errors. You CANNOT remove the test logic.
    3. Your primary goal is to fix the underlying source code logic to make the tests pass naturally.
    
    ### TEAM CODING STANDARDS ###
    {coding_standards}
    
    {dynamic_knowledge_base}
    """

    user_prompt = f"""
    The following code throws a {exc_type}.
    
    Error Log / Stack Trace:
    {stack_trace}
    
    Java Code (File: {file_path}):
    {java_code}
    
    Fix the {exc_type} in the code addressing the root cause indicated by the stack trace or compiler error.
    Return ONLY the raw, updated Java code. Do not include markdown formatting like ```java.
    """

    message = client.messages.create(
        model=model_version,
        max_tokens=4000,
        system=system_instructions,
        messages=[{"role": "user", "content": user_prompt}]
    )

    return message.content[0].text.replace('```java', '').replace('```', '').strip()

def create_pr_and_commit(git_manager: GitManager, fixes_applied: List[Dict]) -> Optional[str]:

    if not fixes_applied:
        return None

    fixed_files = [fix["file"] for fix in fixes_applied]

    if not git_manager.has_uncommitted_changes(fixed_files):
        logger.info("No changes detected")
        return None

    # capture original branch BEFORE checkout
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
    reports_glob = skills.get("test_reports_glob", "target/surefire-reports/*.txt")
    reports_dir = os.path.dirname(reports_glob) or "."

    attempt = 1
    success = False
    compilation_error_output = None

    logger.info(f"Starting Self-Healing Loop (Max Attempts: {MAX_RETRIES})")

    while attempt <= MAX_RETRIES:
        logger.info(f"--- Attempt {attempt} of {MAX_RETRIES} ---")

        if compilation_error_output:
            logger.info("Using compilation error output from previous attempt...")
            stack_trace = compilation_error_output
            exc_type = "Java Compilation Error"
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
            logger.warning("Could not map stack trace/error to local files. Breaking loop.")
            break

        logger.info(f"AI identified failing files: {failing_files}. Generating fixes...")

        for file_path in failing_files:
            fixed_code = generate_fix(file_path, stack_trace, exc_type, standards, skills)
            with open(file_path, "w") as file:
                file.write(fixed_code)
                print(f"fix applied to {file_path}:\n", fixed_code)

            modified_files_map[file_path] = exc_type

        # Use the dynamic directory extracted from the glob for cleanup
        if os.path.exists(reports_dir):
            shutil.rmtree(reports_dir)
            logger.info(f"Cleaned up old test reports at {reports_dir}.")

        logger.info("Running Maven test to validate fixes...")
        test_result = subprocess.run(["mvn", "test"], capture_output=True, text=True)

        if test_result.returncode == 0:
            logger.info("✅ Tests passed successfully!")
            success = True
            break
        else:
            logger.error(f"❌ Fix validation failed on attempt {attempt}.")

            # Validate against the dynamic paths
            if not os.path.exists(reports_dir) or not glob.glob(reports_glob):
                logger.error("Compilation failed. Extracting Maven error log for AI correction.")
                compilation_error_output = test_result.stdout[-4000:]

            attempt += 1

    if success and modified_files_map:
        logger.info("Generating Pull Request with all accumulated fixes...")

        fixes_applied = [{"file": path, "exception": exc} for path, exc in modified_files_map.items()]

        pr_url = create_pr_and_commit(git_manager, fixes_applied)
        if pr_url:
            print(f"🎉 PR Created: {pr_url}")
        else:
            print("Failed to create PR.")
    elif not success and modified_files_map:
        logger.error("All AI attempts failed. The local files were modified, but tests are still failing. No PR will be created.")