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

## API

| Method | Path | Body | Purpose |
|---|---|---|---|
| POST | `/api/members` | `{"name","email","credits"}` | Add a member |
| POST | `/api/members/<id>/credits` | `{"amount"}` | Add credits to a member |
| POST | `/api/classes` | `{"title","starts_at","capacity"}` | Create a class |
| GET | `/api/classes` | | List classes with seats left |
| POST | `/api/bookings` | `{"member_id","class_id"}` | Book (uses 1 credit) |
| DELETE | `/api/bookings/<id>` | | Cancel (refunds 1 credit) |

Booking is rejected when the class is full (409), already booked (409), or the member has no credits (402).

## Roadmap

- [ ] Admin and member login
- [ ] Waitlist when a class is full
- [ ] Recurring classes
- [ ] Email / LINE reminders
- [ ] Multi-language UI (English, 繁體中文, 简体中文)
- [ ] Docker image

## Development

```bash
pip install -e .
python -m unittest discover -s tests -v
```

## License

MIT
