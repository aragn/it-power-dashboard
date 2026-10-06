#!/usr/bin/env bash
# The dashboard's data files (app/data/*.json) live on the "data" branch,
# not on main, so hourly updates don't grow the repository history: the
# branch always holds a single commit, replaced on every update.
#
#   data-branch.sh restore             copy the data files into app/data/
#   data-branch.sh publish FILE...     replace FILE(s) on the data branch;
#                                      a directory replaces everything under
#                                      it (files deleted locally go too), and
#                                      a path that no longer exists locally
#                                      is removed; a file's recent version
#                                      (NAME.recent.json, etl/compact.py)
#                                      goes with it when there is one
#
# "restore" also works locally (Git Bash) to get the data for a preview.
#
# DATA_REMOTE (a remote name or URL, default origin) picks the repository
# whose data branch is used: the GME data lives on the private
# repository's (aragn/it-power-dashboard-private), the rest on this one's.
# The full dashboard locally: restore both,
#   bash .github/scripts/data-branch.sh restore
#   DATA_REMOTE=private bash .github/scripts/data-branch.sh restore

set -euo pipefail

BRANCH=data
REMOTE="${DATA_REMOTE:-origin}"

fetch_branch() {
  # The branch is a single parentless commit, so no --depth is needed.
  git fetch --quiet "$REMOTE" "$BRANCH"
  git rev-parse FETCH_HEAD
}

restore() {
  local head
  head=$(fetch_branch)
  # Only the files on the branch, so the other repository's files restored
  # before are left alone.
  git ls-tree -r --name-only "$head" -- app/data |
    xargs git restore --source="$head" --worktree --
  echo "Restored app/data from $BRANCH ($head)."
}

publish() {
  [ "$#" -gt 0 ] || { echo "publish: no files given" >&2; exit 2; }

  local path companion expanded=()
  for path in "$@"; do
    expanded+=("$path")
    companion="${path%.json}.recent.json"
    if [ "$companion" != "$path" ] && [ -f "$companion" ]; then
      expanded+=("$companion")
    fi
  done
  set -- "${expanded[@]}"

  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

  local attempt head tree index commit
  for attempt in 1 2 3 4 5; do
    head=$(fetch_branch)

    # Start from the branch's current files and swap in ours, so updates
    # from the other workflows are kept.
    index=$(mktemp -u)
    GIT_INDEX_FILE=$index git read-tree "$head"
    for path in "$@"; do
      if [ -d "$path" ]; then
        GIT_INDEX_FILE=$index git ls-files -z -- "$path" |
          GIT_INDEX_FILE=$index xargs -0 -r git update-index --force-remove --
        while IFS= read -r file; do
          GIT_INDEX_FILE=$index git update-index --add \
            --cacheinfo "100644,$(git hash-object -w "$file"),$file"
        done < <(find "$path" -type f | sort)
      elif [ ! -e "$path" ]; then
        GIT_INDEX_FILE=$index git ls-files -z -- "$path" |
          GIT_INDEX_FILE=$index xargs -0 -r git update-index --force-remove --
      else
        GIT_INDEX_FILE=$index git update-index --add \
          --cacheinfo "100644,$(git hash-object -w "$path"),$path"
      fi
    done
    tree=$(GIT_INDEX_FILE=$index git write-tree)
    rm -f "$index"

    if [ "$tree" = "$(git rev-parse "$head^{tree}")" ]; then
      echo "No changes to publish."
      return 0
    fi

    # A parentless commit: the old data drops out of history.
    commit=$(git commit-tree "$tree" -m "Update $* [automated]")

    # The lease fails if another workflow published in the meantime;
    # then start again from its version.
    if git push --quiet --force-with-lease="refs/heads/$BRANCH:$head" \
        "$REMOTE" "$commit:refs/heads/$BRANCH"; then
      echo "Published $* to $BRANCH ($commit)."
      return 0
    fi

    echo "Data branch moved; retrying ($attempt/5)..."
    sleep $((RANDOM % 10 + 5))
  done

  echo "Could not publish after 5 attempts." >&2
  exit 1
}

case "${1:-}" in
  restore) restore ;;
  publish) shift; publish "$@" ;;
  *) echo "usage: $0 restore | publish FILE..." >&2; exit 2 ;;
esac
