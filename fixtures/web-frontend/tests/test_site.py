from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class SiteTests(unittest.TestCase):
    def setUp(self):
        server.set_products("normal")

    def test_the_shop_page_is_served_as_html(self):
        status, headers, body = server.dispatch("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"<title>Corner Stationery</title>", body)

    def test_the_about_page_is_served(self):
        status, _headers, body = server.dispatch("GET", "/about.html")
        self.assertEqual(status, 200)
        self.assertIn(b"About the shop", body)

    def test_the_script_and_the_style_sheet_are_served_with_their_types(self):
        self.assertTrue(server.dispatch("GET", "/app.js")[1]["Content-Type"].startswith("text/javascript"))
        self.assertTrue(server.dispatch("GET", "/styles.css")[1]["Content-Type"].startswith("text/css"))

    def test_the_product_list_has_three_products(self):
        status, headers, body = server.dispatch("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual([product["name"] for product in json.loads(body)], ["Notebook", "Fountain pen", "Graph paper pad"])

    def test_an_unknown_path_is_not_found(self):
        self.assertEqual(server.dispatch("GET", "/missing.html")[0], 404)

    def test_a_path_outside_the_site_is_not_found(self):
        self.assertEqual(server.dispatch("GET", "/../server.py")[0], 404)

    def test_only_reads_are_allowed(self):
        self.assertEqual(server.dispatch("POST", "/api/products")[0], 405)


if __name__ == "__main__":
    unittest.main()
