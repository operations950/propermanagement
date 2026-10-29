#!/bin/bash
# Brings the associations over from Yardi as of Sep 30 2026, matching each owner to the contact you already have (same email, else same phone, else same or close name).
#
#   bash load_yardi_sep30.sh          LOOK ONLY: runs the whole load, prints everything it found, and changes nothing
#   bash load_yardi_sep30.sh apply    DO IT. Take a database backup first.
#
# Run the look first and read it. "TO DECIDE" lists owners who could be more than one existing contact; apply refuses while any are left. To settle them:
#   * the same person:   add  --link "Name As Yardi Has It=contact id"
#   * a new person:      add  --new "Name As Yardi Has It"
#   * all new people:    set  UNDECIDED=--new-for-undecided  below
# St Andrews Glen's books are taken out and loaded again (--wipe-first); the other associations have none yet, so nothing is removed from them.
PY=/app/.venv/bin/python3
cd /app || exit 1

UNDECIDED=""    # set to --new-for-undecided to make every owner still undecided a new person
LINKS=(
  --link "Lauri Kanervikkoaho=774"
  --link "Cherrie St Germain=464"
)
NEW=(
  # --new "Some Owner As Yardi Has Them"
)

FLAGS=(--wipe-first "${LINKS[@]}" "${NEW[@]}")
[ -n "$UNDECIDED" ] && FLAGS+=("$UNDECIDED")
[ "$1" = "apply" ] && FLAGS+=(--apply)

"$PY" manage.py load_yardi_sep30 "${FLAGS[@]}" 2>&1 | grep -v "RuntimeWarning\|warnings.warn"
