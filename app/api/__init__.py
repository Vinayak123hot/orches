"""The HTTP edge -- the only package in the app that knows HTTP exists.

    routes/       the controllers, one module per group of endpoints
    responses.py  the payloads returned when a turn fails or a chat has ended
    schemas.py    request models + response models (the data-leakage guard)

A controller does three things and nothing else: validate the request shape, call ONE
service method, and turn the result -- or a domain error from core/errors.py -- into a
response. All sequencing and business policy lives in services/ and domain/, which is
what keeps them testable and reusable without a web server.

The service (and its per-request DB connection) arrives via Depends, so there is no
connection handling here either: deps.get_db() closes it after the response.

ROUTES ARE DEFINED WITHOUT THE PREFIX. main.py applies Settings.API_PREFIX (default
"/api"), so `@router.post("/chat")` below is served as POST /api/chat. Keeping the prefix
out of the route declarations is what lets it move by configuration -- it was /workflow
while the Functions host mounted the whole app there.
"""
