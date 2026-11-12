# A smoke check of a running API. Copy it to .qa/work/api/smoke.hurl, adapt the paths to the inventory, and run it
# through the kernel with --variable base=http://127.0.0.1:<port> (references/tools.md). Every request here is one
# the contract documents; nothing is sent that the API does not expect.

# The readiness probe answers 200.
GET {{base}}/health
HTTP 200

# A protected operation refuses a call without a token.
GET {{base}}/orders
HTTP 401

# An unknown route is not served.
GET {{base}}/no-such-route
HTTP 404
