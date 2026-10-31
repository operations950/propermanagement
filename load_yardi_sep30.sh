#!/bin/bash
# Brings the associations over from Yardi as of Sep 30 2026. Yardi is the record: each owner is matched to the contact you already have (same email, else same phone, else same
# or close name, and only when that contact's name fits) and that contact is made to read as Yardi has them (name, email, phone); an owner who fits no contact is a new
# contact built from Yardi. No duplicates, and nothing to decide by hand.
#
#   bash load_yardi_sep30.sh          LOOK ONLY: runs the whole load, prints everything it found, and changes nothing
#   bash load_yardi_sep30.sh apply    DO IT. Take a database backup first.
#
# Run the look first and read it. If it got a person wrong, settle them below:
#   * the same person:   add  --link "Name As Yardi Has It=contact id"
#   * a new person:      add  --new "Name As Yardi Has It"
# St Andrews Glen's books are taken out and loaded again (--wipe-first); the other associations have none yet, so nothing is removed from them.
PY=/app/.venv/bin/python3
cd /app || exit 1

LINKS=(
  --link "Lauri Kanervikkoaho=774"
  --link "Cherrie St Germain=464"
)
NEW=(
  # --new "Some Owner As Yardi Has Them"
)

FLAGS=(--wipe-first "${LINKS[@]}" "${NEW[@]}")
[ "$1" = "apply" ] && FLAGS+=(--apply)

"$PY" manage.py load_yardi_sep30 "${FLAGS[@]}" 2>&1 | grep -v "RuntimeWarning\|warnings.warn"
