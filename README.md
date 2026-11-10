# studiobook

Self-hosted booking and membership management for small studios (yoga, pilates, dance, personal training, beauty).
No SaaS fees, no vendor lock-in: one small Python app and one SQLite file.

**Status:** early MVP (v0.1). Class scheduling, members with credit packs, bookings with capacity limits, cancellation with credit refund.

## Quick start

```bash
pip install -e .
studiobook            # serves on http://127.0.0.1:5000
```

Set `STUDIOBOOK_DB=/path/to/studio.db` to choose where data is stored, `PORT` to change the port.

## Admin login

Admin routes are protected with HTTP Basic auth (username `admin`). Set the password before starting:

```bash
# Linux / macOS
STUDIOBOOK_ADMIN_PASSWORD=choose-a-strong-password studiobook
# Windows PowerShell
$env:STUDIOBOOK_ADMIN_PASSWORD="choose-a-strong-password"; studiobook
```

If no password is set, admin routes return 503 (disabled) instead of being left open.
Use HTTPS (for example behind a reverse proxy) when exposing the app to the internet.

## API

| Method | Path | Body | Purpose |
|---|---|---|---|
| POST | `/api/members` (admin) | `{"name","email","credits"}` | Add a member |
| POST | `/api/members/<id>/credits` (admin) | `{"amount"}` | Add credits to a member |
| POST | `/api/classes` (admin) | `{"title","starts_at","capacity"}` | Create a class |
| GET | `/api/classes` | | List classes with seats left |
| POST | `/api/bookings` | `{"member_id","class_id"}` | Book (uses 1 credit) |
| DELETE | `/api/bookings/<id>` (admin) | | Cancel (refunds 1 credit) |

(admin) = admin only. Listing classes and booking are open for now; member login is planned.

Booking is rejected when the class is full (409), already booked (409), or the member has no credits (402).

## Roadmap

- [x] Admin login (HTTP Basic)
- [ ] Member login
- [ ] Waitlist when a class is full
- [ ] Recurring classes
- [ ] Email / LINE reminders
- [ ] Multi-language UI (English, Traditional Chinese, Simplified Chinese)
- [ ] Docker image

## Development

```bash
pip install -e .
python -m unittest discover -s tests -v
```

## License

MIT