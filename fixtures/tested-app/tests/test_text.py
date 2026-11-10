from tested_app import text


def test_slugify_lower_cases_and_hyphenates():
    assert text.slugify("Blue Mug, 350 ml!") == "blue-mug-350-ml"


def test_slugify_runs_on_a_title():
    text.slugify("Winter Sale 2026")
