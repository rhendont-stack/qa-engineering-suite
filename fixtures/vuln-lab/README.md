# Catalogue lab

A training lab: a small catalogue site with a product listing, a greeting page, a welcome note and a visitor session. It runs on
the Python standard library.

It is bound to loopback. It starts only on 127.0.0.1 or ::1 and refuses any other address, and it replies 421 to any request
whose Host header is not a loopback name (127.0.0.1, ::1 or localhost). Run it only inside a disposable container:

    python lab/app.py --host 127.0.0.1 --port 8080
