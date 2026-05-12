# Vercel Deploy Guide

This project can be deployed on Vercel with Python Functions.

## Added files

- `api/index.py`: Vercel entrypoint (Flask app)
- `vercel.json`: Routes all paths to `api/index.py`

## Required environment variables

Set these in Vercel Project Settings -> Environment Variables:

- `GOOGLE_SPREADSHEET_ID`
- `GOOGLE_WORKSHEET_NAME` (optional, default: `list`)
- `GOOGLE_SERVICE_ACCOUNT_JSON` (recommended)
- `APP_USERNAME` (optional)
- `APP_PASSWORD` (optional)

If you set auth, set both `APP_USERNAME` and `APP_PASSWORD`.

## Deploy

1. Push this branch to GitHub.
2. Import the repository in Vercel.
3. Add environment variables.
4. Deploy.

## Notes

- Do not commit service account JSON files.
- `GOOGLE_SERVICE_ACCOUNT_JSON` supports raw JSON or base64-encoded JSON.
