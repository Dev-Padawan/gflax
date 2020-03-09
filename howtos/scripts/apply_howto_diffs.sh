#!/bin/bash
#
# Creates a HOWTO branch for each HOWTO diff file by apply the changes to the
# master branch. Removes all howto branches for which no diff file exists.

. howtos/scripts/common.sh

set -x  # Verbose output.

# check values
if [ -z "${GITHUB_TOKEN}" ]; then
    printf "error: GITHUB_TOKEN not found"
    exit 1
fi

# initialize git
remote_repo="https://${GITHUB_ACTOR}:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git config http.sslVerify false
git config user.name "Automated Publisher"
git config user.email "actions@users.noreply.github.com"

# Fetch all branches
git fetch --no-tags --prune --depth=1 origin +refs/heads/*:refs/remotes/origin/*

# Delete all remote branches starting with "howto-". This ensures we clean up
# HOWTO branches for which the diff files have been deleted.
for b in $(git branch -r | grep origin/howto); do
  branch=${b##*/}  # Strip "origin/" prefix.
  git push origin --delete $branch
done

# Get names of howto's from dif files.
cd $howto_diff_path
howtos=$(ls *.diff | sed -e 's/.diff//')
cd $top_dir

printf "Applying HOWTO diffs to branches..\n"

for howto in $howtos; do
  git checkout -b $howto
  diff_file="${howto_diff_path}/${howto}.diff"
  if [[ -n $(git apply --check "${diff_file}") ]]; then
    printf "\nERROR: Cannot apply ${howto}! ==> PLEASE FIX HOWTO\n"
    exit 1
  fi
  git apply $diff_file
  git commit -am "Added howto branch ${howto}"
  git push -u origin $howto
  # Make sure to checkout the master branch, otherwise the next diff branch
  # will be branched off of the current diff branch.
  git checkout $master_branch
done

cd $old_pwd
