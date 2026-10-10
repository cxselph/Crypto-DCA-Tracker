#!/bin/sh
# Run by deploy.sh on the NAS, as root, to keep the stack's private files private.
# The Synology shared folder's ACL lets every NAS account read what's under it, and the stack inherits it.
# Editing ACL entries with synoacltool proved unreliable (it can drop a folder to plain Linux mode without
# owner write), so this removes the ACL outright and sets plain Unix permissions, then checks the result.
#   lock-folders.sh OWNER DIR...      data folders: owned by OWNER (the app's uid:gid), folders 700, files 600
#   lock-folders.sh --secret FILE...  secrets like .env: owner unchanged (Docker reads it as root), mode 600
set -eu
ACL=${SYNOACLTOOL:-/usr/syno/bin/synoacltool}  # override only for testing

has_acl() { [ -x "$ACL" ] && "$ACL" -get "$1" 2>/dev/null | grep -q '^Archive:.*has_ACL'; }
drop_acls() { find "$1" | while IFS= read -r p; do if has_acl "$p"; then "$ACL" -del "$p" >/dev/null; fi; done; }
acl_left() { find "$1" | while IFS= read -r p; do if has_acl "$p"; then echo "$p (ACL)"; fi; done; }
open_to_others='-perm -g=r -o -perm -g=w -o -perm -g=x -o -perm -o=r -o -perm -o=w -o -perm -o=x'

if [ "${1:-}" = --secret ]; then
  shift
  for f in "$@"; do
    [ -f "$f" ] || continue
    drop_acls "$f"
    chmod 600 "$f"
    # shellcheck disable=SC2086
    bad=$(find "$f" \( $open_to_others \) -print)$(acl_left "$f")
    [ -z "$bad" ] || { printf 'Still open to other NAS accounts:\n%s\n' "$bad" >&2; exit 1; }
    echo "Private to its owner: $f"
  done
  exit 0
fi

OWNER=$1; shift
for dir in "$@"; do
  # Remove any Synology ACL first (it overrides the Unix bits), then set owner and bits explicitly.
  drop_acls "$dir"
  chown -R "$OWNER" "$dir"
  find "$dir" -type d -exec chmod 700 {} +
  find "$dir" ! -type d -exec chmod 600 {} +

  # Check everything under it, so a deploy never reports success while something is open to others
  # or the app can't write.
  # shellcheck disable=SC2086
  bad=$(find "$dir" \( ! -user "${OWNER%%:*}" -o $open_to_others -o ! -perm -u=rw -o \( -type d ! -perm -u=x \) \) -print)$(acl_left "$dir")
  [ -z "$bad" ] || { printf 'Wrong permissions after locking:\n%s\n' "$bad" >&2; exit 1; }
  echo "Private to $OWNER: $dir"
done
