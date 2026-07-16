# Talent Index Proposals

This folder contains AI-reviewed proposals for n8n Data Tables. Each proposal
has a human-readable Markdown review and a JSON payload with `articles`,
`talents`, and `articleTalents`.

Only send a reviewed JSON proposal to `POST /webhook/talent-index/apply`.
The apply workflow validates required fields and references before upserting.
New talent candidates must remain `pending` and search-disabled until
explicitly approved.