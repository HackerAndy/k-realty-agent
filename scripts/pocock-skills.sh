#!/usr/bin/env bash
# Download Matt Pocock's selected skills and support files into your project.
# Run from your project root: bash _scripts/pocock-skills.sh

set -euo pipefail

GITHUB_BASE="https://github.com/mattpocock/skills/tree/main/skills"
RAW_BASE="https://raw.githubusercontent.com/mattpocock/skills/main/skills"
DEST=".claude/skills"

# Format: "category/skill-name"
SKILLS=(
  "engineering/grill-with-docs"
  "engineering/wayfinder"
  "engineering/to-spec"
  "engineering/to-tickets"
  "engineering/implement"
  "engineering/code-review"
  "engineering/improve-codebase-architecture"
  "engineering/domain-modeling"
  "engineering/codebase-design"
  "engineering/prototype"  
  "productivity/grilling"
)

usage() {
  cat <<'EOF'
Usage:
  bash _scripts/pocock-skills.sh [skill]

Examples:
  bash _scripts/pocock-skills.sh
  bash _scripts/pocock-skills.sh engineering/code-review
  bash _scripts/pocock-skills.sh code-review

If [skill] is provided, only that skill is downloaded.
EOF
}

TARGET_SKILL="${1:-}"
if [[ "$TARGET_SKILL" == "-h" || "$TARGET_SKILL" == "--help" ]]; then
  usage
  exit 0
fi

SELECTED_SKILLS=()
if [[ -n "$TARGET_SKILL" ]]; then
  for entry in "${SKILLS[@]}"; do
    category="${entry%%/*}"
    skill="${entry##*/}"
    if [[ "$TARGET_SKILL" == "$entry" || "$TARGET_SKILL" == "$skill" ]]; then
      SELECTED_SKILLS+=("$entry")
    fi
  done

  if [[ "${#SELECTED_SKILLS[@]}" -eq 0 ]]; then
    echo "No matching skill found for: $TARGET_SKILL"
    echo "Expected one of:"
    printf '  %s\n' "${SKILLS[@]}"
    exit 1
  fi
else
  SELECTED_SKILLS=("${SKILLS[@]}")
fi

mkdir -p "$DEST"

for entry in "${SELECTED_SKILLS[@]}"; do
  category="${entry%%/*}"
  skill="${entry##*/}"

  echo "Downloading $skill ($category)..."

  # Scrape the GitHub directory listing to find all files
  files=$(curl -fsSL "$GITHUB_BASE/$category/$skill" \
    | grep -oE "href=\"/mattpocock/skills/blob/main/skills/$category/$skill/[^\"]+\"" \
    | sed "s|href=\"/mattpocock/skills/blob/main/skills/$category/$skill/||;s|\"||g" \
    | sort -u)

  if [[ -z "$files" ]]; then
    echo "  ✗ No files found for $skill — check the skill name."
    continue
  fi

  mkdir -p "$DEST/$skill"
  while IFS= read -r file; do
    if curl -fsSL "$RAW_BASE/$category/$skill/$file" -o "$DEST/$skill/$file"; then
      echo "  ✓ Saved to $DEST/$skill/$file"
    else
      echo "  ✗ Failed to download $file"
    fi
  done <<< "$files"
done

# --- Skill docs ---
RAW_ROOT="https://raw.githubusercontent.com/mattpocock/skills/main"
DOCS_DEST="docs/skills"

echo ""
echo "Downloading skill docs..."

for entry in "${SELECTED_SKILLS[@]}"; do
  category="${entry%%/*}"
  skill="${entry##*/}"
  dest_dir="$DOCS_DEST/$category"
  mkdir -p "$dest_dir"
  if curl -fsSL "$RAW_ROOT/docs/$category/$skill.md" -o "$dest_dir/$skill.md"; then
    echo "  ✓ Saved to $dest_dir/$skill.md"
  else
    echo "  ✗ No doc found for $skill"
  fi
done

# --- Single files from the repo root ---

echo ""
echo "Downloading support files..."

# CONTEXT.md — domain vocabulary template (goes in project root)
if curl -fsSL "$RAW_ROOT/CONTEXT.md" -o "CONTEXT.md"; then
  echo "  ✓ Saved to CONTEXT.md"
else
  echo "  ✗ Failed to download CONTEXT.md"
fi

# .agents/ files — agent invocation and doc-writing instructions
mkdir -p ".agents"
for agent_file in "invocation.md" "writing-docs.md"; do
  if curl -fsSL "$RAW_ROOT/.agents/$agent_file" -o ".agents/$agent_file"; then
    echo "  ✓ Saved to .agents/$agent_file"
  else
    echo "  ✗ Failed to download .agents/$agent_file"
  fi
done

echo ""
echo "Done."
echo "  Skills      → $DEST/"
echo "  Skill docs  → $DOCS_DEST/"
echo "  Context     → CONTEXT.md (fill in your project's domain vocabulary)"
echo "  Agent rules → .agents/"
echo ""
echo "Update .claude/skills/README.md to add the new skills to your skills table."