# studiobook

Self-hosted booking and membership management for small studios — yoga, pilates, dance,
fitness, training. One small Python app and SQLite files: no SaaS fees, no vendor lock-in.

**Status:** v0.5 — multi-institution, class codes, student web app, admin dashboard.

## What it does

**Multi-institution.** One deployment serves many studios. Each institution has its own
database, its own class code, its own students, schedule and reports. Nothing is shared
between institutions.

**Students** sign up with an e-mail and password, join a class by typing the code their
teacher gave them, then book from a phone-first web app (installable to the home screen).
They see a month calendar, filter by class type, book or join a waitlist, cancel, and
track their credit packs and memberships. Teachers can set a new password for a
student who is locked out.

**Teachers** sign in with their admin e-mail and password and get a dashboard: today's
classes, check-in, a timeline week view, members, plans and sales, reports with CSV
export, class types and instructors, the class code to share, and a one-click database
backup of their own institution.

**Languages:** English, Traditional Chinese, Simplified Chinese. Add `?lang=en`,
`?lang=zh-Hant` or `?lang=zh-Hans` to any URL, or let the browser decide.

## Quick start

Windows: double-click `start.bat` (this computer only) or `start-lan.bat`
(everyone on the same Wi-Fi; it prints the address to share).

Any platform:

```bash
pip install -e .
python -m studiobook.app
```

Environment variables: `STUDIOBOOK_DB` (database path), `PORT`, `STUDIOBOOK_HOST`
(use `0.0.0.0` to accept connections from other machines).

## First run

1. An existing single-studio `studiobook.db` is adopted as the first institution.
2. An admin account is created: **`admin@local`**, with the password stored in
   `admin-password.txt` next to the database. Change it under Settings → Admin account.
3. The dashboard shows the institution's **class code**. Share it with students.

New institutions can be created from the sign-in page (Teacher tab → *Create an
institution*); they start empty and get their own code.

## How the data is organised

```
registry.db                 accounts, institutions, admins, memberships
studiobook.db               institution #1 (the original database)
tenants/tenant-xxxxxxxx.db  every institution created later
```

`studiobook/core.py` stays single-tenant: one `Studio` object is one institution, so
scheduling, credits, waitlists and reports never deal with tenant filters. The registry
only answers "who is this person, and which institution are they looking at".

## Tests

```bash
pip install -e . pytest
python -m pytest -q
```

91 tests: core booking rules, credits and waitlists, web flows, and multi-institution
behaviour (isolation, class codes, joining, leaving, team management, password resets).

## Not included

- Online payments (needs a payment provider account)
- E-mail / LINE reminders (needs provider credentials)
- Public hosting, HTTPS and backups — run it behind a reverse proxy if you expose it

## License

MIT
