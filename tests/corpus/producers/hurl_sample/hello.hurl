# Holds: the corpus service greets.
GET {{base}}/hello
HTTP 200
[Asserts]
jsonpath "$.message" == "hello"
