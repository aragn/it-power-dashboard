#!/usr/bin/env bash
# The dashboard's data files (app/data/*.json) live on the "data" branch,
# not on main, so hourly updates don't grow the repository history: the
# branch always holds a single commit, replaced on every update.
#
#   data-branch.sh restore [PATH...]   copy the data files into app/data/
#                                      (only those under PATH(s), if given)
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
#
# DATA_LOCAL (a folder) replaces the data branches with a folder on this
# machine holding app/data/ (both repositories' files): the self-hosted
# runner's mode, where the data stays on the owner's laptop (D:/Power/
# it-power-dashboard, the local clone, so the local dashboard shows each
# update).  "restore" makes app/data/ match the folder's copy, "publish"
# copies the FILEs into it, with the same rules as for the branch.

set -euo pipefail

BRANCH=data
REMOTE="${DATA_REMOTE:-origin}"
LOCAL="${DATA_LOCAL:-}"

# "path<TAB>time<TAB>size" for each file under the PATHs in ROOT (not the
# temporary copies of a publish under way).
listing() {
  local root=$1
  shift
  (cd "$root" 2>/dev/null && find "$@" -type f ! -name '*.tmp-[0-9]*' -printf '%p\t%T@\t%s\n' 2>/dev/null) || true
}

# sync FROM TO PATH...: make the PATHs under TO match those under FROM.
# A file whose time or size differs is copied with its time (so it matches
# next time) under a temporary name and renamed into place, so the
# dashboard never reads half a file; a file only under TO is removed.
sync_paths() {
  local from=$1 to=$2 op file
  shift 2
  awk -F'\t' 'FILENAME == ARGV[1] { have[$1] = $2 FS $3; next }
    { if (have[$1] != $2 FS $3) print "copy\t" $1; delete have[$1] }
    END { for (f in have) print "remove\t" f }' \
    <(listing "$to" "$@") <(listing "$from" "$@") |
    while IFS=$'\t' read -r op file; do
      if [ "$op" = copy ]; then
        [ -d "$to/${file%/*}" ] || mkdir -p "$to/${file%/*}"
        cp -p "$from/$file" "$to/$file.tmp-$$"
        mv -f "$to/$file.tmp-$$" "$to/$file"
      else
        rm -f "$to/$file"
      fi
    done
}

fetch_branch() {
  # The branch is a single parentless commit, so no --depth is needed.
  git fetch --quiet "$REMOTE" "$BRANCH"
  git rev-parse FETCH_HEAD
}

restore() {
  if [ -n "$LOCAL" ]; then
    sync_paths "$LOCAL" . "${@:-app/data}"
    echo "Restored ${*:-app/data} from $LOCAL."
    return 0
  fi

  local head
  head=$(fetch_branch)
  # Only the files on the branch, so the other repository's files restored
  # before are left alone.
  git ls-tree -r --name-only "$head" -- "${@:-app/data}" |
    xargs -r git restore --source="$head" --worktree --
  echo "Restored ${*:-app/data} from $BRANCH ($head)."
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

  if [ -n "$LOCAL" ]; then
    sync_paths . "$LOCAL" "$@"
    echo "Published $* to $LOCAL."
    return 0
  fi

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
  restore) shift; restore "$@" ;;
  publish) shift; publish "$@" ;;
  *) echo "usage: $0 restore [PATH...] | publish FILE..." >&2; exit 2 ;;
esac
