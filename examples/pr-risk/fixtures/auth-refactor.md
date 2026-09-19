# Refactor session middleware to support API tokens

Allows service accounts to call the API with a bearer token instead of a cookie
session. The middleware now resolves the principal from either source and the
`require_role` decorator reads roles from the resolved principal.

- new `resolve_principal()` handles cookie sessions and `Authorization: Bearer` tokens
- `require_role` no longer falls through to the anonymous user when the session is missing
- tokens are looked up in the new `api_tokens` table (migration included)
