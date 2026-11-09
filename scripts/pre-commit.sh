#!/usr/bin/env bash
# Pre-commit hook: block secrets from entering git history.
# Install: cp scripts/pre-commit.sh .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

RED='\033[0;31m'
BOLD='\033[1m'
RESET='\033[0m'

BLOCKED=0

block() {
  echo -e "${RED}${BOLD}[BLOCKED]${RESET} ${RED}$1${RESET}" >&2
  BLOCKED=1
}

# ── 1. Filename checks ────────────────────────────────────────────────────────
DANGEROUS_FILES=$(git diff --cached --name-only | grep -E \
  '(^|/)\.env$|\.pem$|\.db$|private_key|service_account')

if [[ -n "$DANGEROUS_FILES" ]]; then
  while IFS= read -r f; do
    block "Dangerous filename staged: $f"
  done <<< "$DANGEROUS_FILES"
fi

# ── 2. Content checks (private-key headers) ───────────────────────────────────
# Iterate over staged text files and grep their staged content.
while IFS= read -r file; do
  # Skip binary files and deleted files.
  git diff --cached --diff-filter=D --name-only | grep -qF "$file" && continue
  mime=$(git diff --cached --name-only -- "$file" | xargs -I{} git show ":$file" 2>/dev/null \
    | file --mime-type - 2>/dev/null | awk '{print $2}')
  # Use git show to read the staged blob and scan it.
  if git show ":$file" 2>/dev/null | grep -qE -- '-----BEGIN[[:space:]].*PRIVATE KEY-----|-----BEGIN RSA PRIVATE KEY-----'; then
    block "Private key header detected in staged file: $file"
  fi
done < <(git diff --cached --name-only --diff-filter=ACMR)

# ── Result ────────────────────────────────────────────────────────────────────
if [[ "$BLOCKED" -eq 1 ]]; then
  echo -e "\n${RED}${BOLD}Commit aborted.${RESET} Remove the sensitive file(s) or content and try again." >&2
  echo -e "  To unstage a file:  ${BOLD}git reset HEAD <file>${RESET}" >&2
  exit 1
fi

exit 0
