# Money Printer Fixed

This package keeps your HTML frontend the same and patches the backend loader.

## What was fixed
The `/api/savant` loader now:
- tries `pybaseball` first
- falls back to direct Baseball Savant CSV requests
- retries failed requests
- chunks date ranges into smaller requests
- uses browser-like headers
- uses a final SSL fallback for the `SSLEOFError: UNEXPECTED_EOF_WHILE_READING` issue

## Run
```bash
pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Then open `Money_Printer.html` and keep API Base URL as:

```text
http://127.0.0.1:8000
```
