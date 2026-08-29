# Put KeeperCoach online with Railway

This package is prepared for Railway as a single Dockerized FastAPI service.

## Recommended test deployment

1. Create a GitHub repository named `keepercoach` and upload the contents of this folder to it.
2. In Railway, choose **New Project → Deploy from GitHub repo** and select the repository.
3. Railway detects the included `Dockerfile` and `railway.json`.
4. Add a Railway **Volume** to the KeeperCoach service and set its mount path to:

   `/app/data`

   This is essential because KeeperCoach currently stores its SQLite database and uploaded match videos in that directory.
5. In the service **Networking** settings, choose **Generate Domain**.
6. Open the generated `*.railway.app` address on iPhone, iPad or computer.
7. For the included demo data, sign in with:

   - Email: `demo@keepercoach.app`
   - Password: `keepercoach`

## Environment variables

The Dockerfile already defines safe MVP defaults:

- `KEEPERCOACH_DATA_DIR=/app/data`
- `KEEPERCOACH_MAX_UPLOAD_MB=500`

You can override the upload limit in Railway Variables. For early phone testing, 500 MB is more practical than the original 2 GB local limit.

## Important MVP limitation

Railway Volumes are suitable for private/early MVP testing, but production video storage should move to S3-compatible object storage. This will make large match uploads, streaming, scaling and backups more robust.

## iPhone install

After opening the deployed site in Safari:

1. Tap the Share button.
2. Tap **Add to Home Screen**.
3. Name it `KeeperCoach`.

KeeperCoach's included web-app manifest allows it to behave more like an installed web app.
