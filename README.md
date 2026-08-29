# KeeperCoach MVP

A working full-stack goalkeeper development MVP.

## Included
- Account registration and sign-in (keeper, coach, parent roles)
- Keeper profiles
- Match records
- Real video upload and playback (MP4/MOV/M4V/WebM, MVP limit 2 GB)
- Fast timeline tagging using the current video timestamp
- Goalkeeper action scoring (technique, decision making, positioning, execution)
- Match ratings, strengths, development focus and training recommendations
- Progress history by keeper
- SQLite persistence and file-based video storage
- JSON export API
- Responsive mobile/desktop UI and installable-web-app manifest
- Clear backend boundary for a future computer-vision analysis service

## Run locally
Requires Python 3.10+ and ffprobe/FFmpeg (duration detection is optional).

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
./run.sh                    # Windows: python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000

Demo account:
- Email: demo@keepercoach.app
- Password: keepercoach

## Production notes
This is an MVP, not yet production-hardened. Before public launch, replace local video storage with object storage, add email verification/password reset, rate limiting, audit logging, backups, privacy/retention controls, signed streaming URLs, automated tests and a production reverse proxy/TLS setup.

Automatic goalkeeper-event detection is intentionally **not faked** in this build. The current MVP uses manual tagging; the next technical milestone is a real computer-vision pipeline that proposes action timestamps for coach review.
