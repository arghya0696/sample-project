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
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Configuration file {file_path} is missing. Cannot proceed.")

    with open(file_path, 'r') as file:
        skills = json.load(file)

    if "model_version" not in skills or not skills["model_version"]:
        raise ValueError("Mandatory parameter 'model_version' is missing in ai-skills.json")
    if "test_reports_glob" not in skills or not skills["test_reports_glob"]:
        raise ValueError("Mandatory parameter 'test_reports_glob' is missing in ai-skills.json")

    # --- NEW: Placeholder Validation ---
    def check_placeholders(prompt_key, required_placeholders):
        prompt_data = skills.get(prompt_key)
        if not prompt_data:
            return  # Fallback to defaults later, which are safe
        prompt_str = "\n".join(prompt_data) if isinstance(prompt_data, list) else str(prompt_data)
        for ph in required_placeholders:
            if f"{{{ph}}}" not in prompt_str:
                raise ValueError(f"CRITICAL ERROR: The custom '{prompt_key}' in ai-skills.json is missing the mandatory '{{{ph}}}' placeholder.")

    check_placeholders("file_extraction_prompt", ["dynamic_knowledge_base", "stack_trace"])
    check_placeholders("fix_generation_system_prompt", ["coding_standards", "dynamic_knowledge_base"])
    check_placeholders("fix_generation_user_prompt", ["exc_type", "stack_trace", "file_path", "code"])

    return skills

def get_coding_standards(file_path=".github/scripts/coding-standards.md"):
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            return file.read()
    return ""

def build_dynamic_context(skills: dict) -> str:
    exclude_keys = [
        "model_version", "test_reports_glob", "max_retries", "target_exceptions",
        "test_command", "cleanup_directories", "fallback_exception_regex",
        "file_extraction_prompt", "fix_generation_system_prompt", "fix_generation_user_prompt"
    ]

    context_blocks = []
    for key, rules in skills.items():
        if key not in exclude_keys and isinstance(rules, list) and rules:
            category_title = key.replace('_', ' ').upper()
            rules_str = "\n".join([f"- {rule}" for rule in rules])
            context_blocks.append(f"### {category_title} ###\n{rules_str}")

    return "\n\n".join(context_blocks)

def parse_prompt_config(prompt_data, default_prompt: str) -> str:
    if not prompt_data:
        return default_prompt
    if isinstance(prompt_data, list):
        return "\n".join(prompt_data)
    return str(prompt_data)

def find_exception_in_reports(skills: dict):
    reports_glob = skills["test_reports_glob"]
    target_exceptions = skills.get("target_exceptions", [])
    fallback_regex = skills.get("fallback_exception_regex", r'([a-zA-Z0-9_.]+(?:Exception|Error|Failure))')

    reports = glob.glob(reports_glob)
    fallback_content, fallback_exc_type = None, None

    for report in reports:
        if not os.path.isfile(report): continue
        with open(report, 'r') as file:
            content = file.read()

            for exc_type in target_exceptions:
                if exc_type in content:
                    logger.info(f"Found targeted '{exc_type}' in report: {report}")
                    return content, exc_type

            if not fallback_exc_type:
                match = re.search(fallback_regex, content)
                if match:
                    fallback_exc_type = match.group(1)
                    fallback_content = content

    return fallback_content, fallback_exc_type

def get_failing_files_from_ai(stack_trace: str, skills: dict) -> List[str]:
    dynamic_knowledge_base = build_dynamic_context(skills)
    model_version = skills["model_version"]

    default_prompt = "Identify broken files.\n{dynamic_knowledge_base}\nLog:\n{stack_trace}"
    raw_prompt = parse_prompt_config(skills.get("file_extraction_prompt"), default_prompt)

    prompt = raw_prompt.format(
        dynamic_knowledge_base=dynamic_knowledge_base,
        stack_trace=stack_trace
    )

    # HARDCODED XML EXTRACTION REQUIREMENT
    prompt += "\n\nCRITICAL FORMATTING REQUIREMENT:\nYou MUST NOT output any explanations, reasoning, or conversational text. Return ONLY the XML block.\nReturn the output wrapped EXACTLY in <files> tags containing only the JSON array. Example:\n<files>\n[\"path/to/broken_file.ext\"]\n</files>"

    message = client.messages.create(
        model=model_version,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw_output = message.content[0].text.strip()

    # Log Claude's raw output so we can see exactly what it's thinking if it breaks!
    logger.info(f"AI Raw Extraction Output:\n{raw_output}")

    match = re.search(r'<files>\s*(.*?)\s*</files>', raw_output, re.DOTALL)

    try:
        if match:
            json_str = match.group(1).replace("```json", "").replace("```", "").strip()
            file_names = json.loads(json_str)
        else:
            clean_str = re.sub(r'^.*?(\[.*\]).*$', r'\1', raw_output, flags=re.DOTALL)
            file_names = json.loads(clean_str)

        found_paths = []
        for file_name in file_names:
            # 1. If Claude returned a perfectly valid relative path, use it directly
            if os.path.isfile(file_name):
                found_paths.append(file_name)
                continue

            # 2. Otherwise, extract the base name and search the repository for it
            basename = os.path.basename(file_name)
            found = False
            for root, dirs, files in os.walk('.'):
                if basename in files:
                    found_paths.append(os.path.join(root, basename))
                    found = True
                    break

            if not found:
                logger.warning(f"AI suggested '{file_name}', but it does not exist in the repository.")

        return found_paths
    except json.JSONDecodeError:
        logger.error(f"Failed to parse AI file identification response.")
        return []

def generate_fix(file_path, stack_trace, exc_type, coding_standards, skills):
    with open(file_path, 'r') as file:
        code_content = file.read()

    dynamic_knowledge_base = build_dynamic_context(skills)
    model_version = skills["model_version"]

    default_sys = "Fix the code.\n{coding_standards}\n{dynamic_knowledge_base}"
    raw_sys_prompt = parse_prompt_config(skills.get("fix_generation_system_prompt"), default_sys)

    system_instructions = raw_sys_prompt.format(
        coding_standards=coding_standards,
        dynamic_knowledge_base=dynamic_knowledge_base
    )

    default_user = "Error: {exc_type}\nLog:\n{stack_trace}\nCode:\n{code}"
    raw_user_prompt = parse_prompt_config(skills.get("fix_generation_user_prompt"), default_user)

    user_prompt = raw_user_prompt.format(
        exc_type=exc_type,
        stack_trace=stack_trace,
        file_path=file_path,
        code=code_content
    )

    # HARDCODED XML EXTRACTION REQUIREMENT (Language Agnostic)
    user_prompt += "\n\nCRITICAL FORMATTING REQUIREMENT:\nReturn the updated code wrapped EXACTLY in <code> tags. NO markdown. NO preamble.\nExample:\n<code>\n// your updated code here...\n</code>"

    message = client.messages.create(
        model=model_version,
        max_tokens=4000,
        system=system_instructions,
        messages=[{"role": "user", "content": user_prompt}]
    )

    raw_output = message.content[0].text

    match = re.search(r'<code>\s*(.*?)\s*</code>', raw_output, re.DOTALL)

    if match:
        fixed_code = match.group(1).strip()
    else:
        fixed_code = re.sub(r'^```[a-zA-Z]*\n', '', raw_output)
        fixed_code = fixed_code.replace('```', '').strip()

    return fixed_code

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
        return git_manager.create_pr(branch_name=branch_name, files_changed=fixed_files, base_branch=source_branch)

    if pr_state == "OPEN":
        logger.info("Existing PR is open — branch updated automatically.")
        return git_manager.get_existing_pr_url(branch_name)

    logger.info("Branch exists but PR is merged/closed — creating a new PR.")
    return git_manager.create_pr(branch_name=branch_name, files_changed=fixed_files, base_branch=source_branch)

if __name__ == "__main__":
    workspace = Path(os.getcwd())
    git_manager = GitManager(workspace)
    modified_files_map = {}

    skills = load_skills(".github/scripts/ai-skills.json")
    standards = get_coding_standards(".github/scripts/coding-standards.md")

    MAX_RETRIES = skills.get("max_retries", 5)
    test_command = skills.get("test_command", ["mvn", "test"])
    logger.info(f"test command is : {test_command}")

    default_cleanup = [os.path.dirname(skills["test_reports_glob"]) or "."]
    cleanup_directories = skills.get("cleanup_directories", default_cleanup)

    attempt = 1
    success = False
    compilation_error_output = None

    logger.info(f"Starting Self-Healing Loop (Max Attempts: {MAX_RETRIES}) | Command: {' '.join(test_command)}")

    while attempt <= MAX_RETRIES:
        logger.info(f"--- Attempt {attempt} of {MAX_RETRIES} ---")

        if compilation_error_output:
            logger.info("Using syntax error output from previous attempt...")
            stack_trace = compilation_error_output
            exc_type = skills.get("compilation_error_identifier", "Compilation/Syntax Error")
            compilation_error_output = None
        else:
            stack_trace, exc_type = find_exception_in_reports(skills)

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
            logger.warning("Could not map trace/error to local files. Breaking loop.")
            break

        logger.info(f"AI identified failing files: {failing_files}. Generating fixes...")

        for file_path in failing_files:
            fixed_code = generate_fix(file_path, stack_trace, exc_type, standards, skills)
            with open(file_path, "w") as file:
                file.write(fixed_code)
                logger.info(f"fixed code is : {fixed_code}")
                logger.info(f"Fix applied to {file_path}")

            modified_files_map[file_path] = exc_type

        for directory in cleanup_directories:
            if os.path.exists(directory):
                shutil.rmtree(directory)
                logger.info(f"Cleaned up directory at {directory}")

        logger.info("Running configured test command to validate fixes...")
        test_result = subprocess.run(test_command, capture_output=True, text=True)

        if test_result.returncode == 0:
            logger.info("✅ Tests passed successfully!")
            success = True
            break
        else:
            logger.error(f"❌ Fix validation failed on attempt {attempt}.")

            reports_generated = any(glob.glob(skills["test_reports_glob"]))
            if not reports_generated:
                logger.error("No test logs generated. Extracting terminal error log for AI correction.")
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