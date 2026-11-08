from __future__ import annotations

import json
import os
import re

import pytest

from helpers import finishes
from qa import QaError, execution, redact, state, workspace

GH = "gh" + "p_" + "a1B2c3D4e5" * 3 + "f6G7h8"   # 36 chars after the prefix, built at runtime


@pytest.fixture
def redactor():
    return redact.Redactor(b"k" * 32, redact.load_patterns(), {})


def test_github_token_redacted(redactor):
    out, n = redactor.text(f"token {GH} end")
    assert GH not in out and n == 1 and re.search(r"\[REDACTED:github_token:[0-9a-f]{8}\]", out)


def test_marker_is_keyed_and_deterministic():
    assert redact.marker("k", "s", b"1" * 32) == redact.marker("k", "s", b"1" * 32)
    assert redact.marker("k", "s", b"1" * 32) != redact.marker("k", "s", b"2" * 32)


def test_env_value_redacted_by_name(ws):
    val = "s3cr3t" + "-value-x"   # entropy above the threshold, so built at runtime
    r = redact.load_redactor(ws, {"MY_API_TOKEN": val, "HOME": "/Users/x"})
    out, n = r.text(f"using {val} at /Users/x")
    assert val not in out and "/Users/x" in out and n == 1 and "[REDACTED:env_value:" in out


def test_short_env_value_kept(ws):
    out, n = redact.load_redactor(ws, {"A_TOKEN": "abc"}).text("abc")
    assert out == "abc" and n == 0


def test_password_assignment_always(redactor):
    assert "hunter22" not in redactor.text("password=hunter22")[0]


def test_secret_assignment_needs_entropy(redactor):
    hi = "Zx81" + "kQ02pLm7"   # entropy above the threshold, so built at runtime
    assert redactor.text("api_key=aaaaaaaaaa")[0] == "api_key=aaaaaaaaaa"
    assert hi not in redactor.text("api_key=" + hi)[0]


def test_shannon_entropy():
    assert redact.shannon_entropy("aaaa") == 0.0 and redact.shannon_entropy("abcd") == 2.0


def test_url_credentials(redactor):
    out, _ = redactor.text("postgres://app:" + "pa55" + "word@db.local/x")
    assert "pa55word" not in out and "db.local/x" in out


def test_bytes_non_utf8_preserved(redactor):
    out, n = redactor.bytes_(b"\xff\xfe ok " + GH.encode() + b" \x80")
    assert out.startswith(b"\xff\xfe ok ") and out.endswith(b" \x80") and GH.encode() not in out and n == 1


def test_obj_redacts_values_not_keys(redactor):
    out, n = redactor.obj({"password": ["x", GH], GH: 1})
    assert GH not in out["password"][1] and GH in out and n == 1


def test_extra_env_names(ws):
    cfg = json.loads(ws.path("config.json").read_text()); cfg["redaction"]["extra_env_names"] = ["DB_URL"]
    ws.path("config.json").write_bytes(workspace.config_bytes(cfg))
    val = "mysql-conn" + "-string-1"   # entropy above the threshold, so built at runtime
    r = redact.load_redactor(ws, {"DB_URL": val})
    assert val not in r.text("x " + val)[0]


# --- additions -------------------------------------------------------------------------

B36 = "aB3dE5gH7jK9mN1pQ3rS5tU7vW9xY1zA3bC5"   # 36 alphanumerics
B20 = "Qw3Er5Ty7Ui9Op1As3Df5"

# kind -> (text containing one secret, the secret), every value built at runtime
KIND_CASES = {
    "private_key": ("-----BEGIN RSA PRIVATE" + " KEY-----\nMIIEow" + "IBAAKCAQEA\n-----END RSA PRIVATE" + " KEY-----",
                    "MIIEow" + "IBAAKCAQEA"),
    "github_token": ("x " + "github" + "_pat_" + B20 + "Zz9 y", "github" + "_pat_" + B20 + "Zz9"),
    "gitlab_token": ("gl" + "pat-" + B20, "gl" + "pat-" + B20),
    "aws_access_key": ("id " + "AK" + "IA" + "Q3W5E7R9T1Y3U5I7", "AK" + "IA" + "Q3W5E7R9T1Y3U5I7"),
    "slack_token": ("xo" + "xb-" + "1234567890-" + "abcDEF123456", "xo" + "xb-" + "1234567890-" + "abcDEF123456"),
    "stripe_key": ("sk" + "_live_" + B20, "sk" + "_live_" + B20),
    "google_api_key": ("AI" + "za" + B36[:35], "AI" + "za" + B36[:35]),
    "npm_token": ("np" + "m_" + B36, "np" + "m_" + B36),
    "jwt": ("ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "sig-Abc_123",
            "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "sig-Abc_123"),
    "anthropic_key": ("sk" + "-ant-" + B20 + "-Zz", "sk" + "-ant-" + B20 + "-Zz"),
    "openai_key": ("sk" + "-proj-" + B20, "sk" + "-proj-" + B20),
    "url_credentials": ("https://bob:" + "p4ss" + "w0rd@host/x", "p4ss" + "w0rd"),
    "password_assignment": ('"db_pass' + 'word": "hunt' + 'er22"', "hunt" + "er22"),
    "secret_assignment": ("X-Api-Key: " + "Zx81" + "kQ02pLm7", "Zx81" + "kQ02pLm7"),
}


def test_every_kind_in_policy_has_a_case():
    assert [p.kind for p in redact.load_patterns()] == list(KIND_CASES)


@pytest.mark.parametrize("kind", list(KIND_CASES))
def test_each_kind_is_redacted(redactor, kind):
    text, secret = KIND_CASES[kind]
    out, n = redactor.text(text)
    assert secret not in out and n == 1 and "[REDACTED:%s:" % kind in out


def test_anthropic_key_is_not_labelled_openai(redactor):
    out, n = redactor.text(KIND_CASES["anthropic_key"][0])
    assert n == 1 and "openai_key" not in out


def test_existing_marker_is_left_alone(redactor):
    text = "see [REDACTED:github_token:0123abcd] and [REDACTED:env_value:89abcdef]"
    assert redactor.text(text) == (text, 0)


def test_marker_inserted_by_a_pattern_is_not_rescanned(redactor):
    out, n = redactor.text(GH)
    assert n == 1 and re.fullmatch(r"\[REDACTED:github_token:[0-9a-f]{8}\]", out)


def test_assignment_keeps_key_and_quotes(redactor):
    out, n = redactor.text('{"password": "hunter22", "ok": 1}')
    assert n == 1 and out.startswith('{"password": "[REDACTED:password_assignment:') and out.endswith('", "ok": 1}')


def test_env_name_is_case_insensitive_and_longest_value_first(ws):
    short, long_ = "abcdefgh", "abcdefgh-ijkl"
    r = redact.load_redactor(ws, {"my_api_token": short, "DB_PASSWORD": long_})
    out, n = r.text(long_ + " " + short)
    assert short not in out and long_ not in out and n == 2


def test_environ_none_uses_process_environment(ws, monkeypatch):
    val = "proc" + "-env-value"
    monkeypatch.setenv("QA_TEST_SECRET", val)
    assert val not in redact.load_redactor(ws).text("a " + val)[0]


@pytest.mark.parametrize("content", [b"short", b"k" * 33, None])
def test_bad_redaction_key_fails(ws, content):
    if content is None:
        ws.path("redaction.key").unlink()
    else:
        ws.path("redaction.key").write_bytes(content)
    with pytest.raises(QaError) as ei:
        redact.load_redactor(ws, {})
    assert ei.value.code == "redaction_key_invalid"


def test_malformed_config_fails_closed(ws):
    ws.path("config.json").write_bytes(b'{"redaction": {"extra_env_names": "DB_URL"}}')
    with pytest.raises(QaError) as ei:
        redact.load_redactor(ws, {})
    assert ei.value.code == "schema_invalid"
    ws.path("config.json").write_bytes(b"{not json")
    with pytest.raises(QaError) as ei:
        redact.load_redactor(ws, {})
    assert ei.value.code == "invalid_json"


def test_load_patterns_is_cached_and_ordered():
    assert redact.load_patterns() is redact.load_patterns()
    assert redact.load_patterns()[0].kind == "private_key"


@pytest.mark.parametrize("bad", ["(", "(a)"])
def test_invalid_policy_regex_or_group(monkeypatch, tmp_path, bad):
    policies = tmp_path / "policies"
    policies.mkdir()
    group = 2 if bad == "(a)" else 0
    doc = {"patterns": [{"kind": "x", "regex": bad, "group": group, "min_len": 0, "min_entropy": None}]}
    (policies / "redaction.json").write_text(json.dumps(doc))
    real = redact.resource_dir
    monkeypatch.setattr(redact, "resource_dir", lambda kind: policies if kind == "policies" else real(kind))
    monkeypatch.setattr(redact, "_PATTERNS", None)
    with pytest.raises(QaError) as ei:
        redact.load_patterns()
    assert ei.value.code == "redaction_policy_invalid"


PEM_HEAD = "-----BEGIN RSA PRIVATE" + " KEY-----"


def test_truncated_private_key_stops_at_the_pem_body(redactor):
    body = "MIIEow" + "IBAAKCAQEAx9" + "Zk3Lm" * 8
    text = "start\n" + PEM_HEAD + "\nProc-Type: 4,ENCRYPTED\n\n" + body + "\n" + body[:20] + "\n2026-11-07 INFO next step ok\n"
    out, n = redactor.text(text)
    assert n == 1 and body not in out and body[:20] not in out and "Proc-Type" not in out
    assert out.startswith("start\n[REDACTED:private_key:") and out.endswith("\n2026-11-07 INFO next step ok\n")


def test_header_mentioned_in_a_sentence_keeps_following_lines(redactor):
    text = 'expected "' + PEM_HEAD + '" here\nsecond line of the log\nthird line\n'
    out, _ = redactor.text(text)
    assert out.endswith('" here\nsecond line of the log\nthird line\n')


@pytest.mark.parametrize("keyword", ["password", "token"])
def test_assignment_patterns_are_linear_on_long_keyword_runs(redactor, keyword):
    import time

    run = keyword * (200_000 // len(keyword))
    start = time.process_time()
    out, _ = redactor.text(run + "=" + "Zx81" + "kQ02pLm7 and " + keyword + "=" + "Qw3Er5Ty")
    assert time.process_time() - start < 2.0
    assert "Qw3Er5Ty" not in out


@pytest.mark.parametrize("text", [
    "a" * 200_000,
    "http://" + "a" * 200_000,
    "-----BEGIN PRIVATE" + " KEY-----\n" + " " * 200_000 + "x",
    "-----BEGIN PRIVATE" + " KEY-----\n" * 5000,
], ids=["long_run", "long_scheme_host", "blank_pem_line", "many_headers"])
def test_other_patterns_are_linear(redactor, text):
    import time

    start = time.process_time()
    redactor.text(text)
    assert time.process_time() - start < 2.0


def test_a_symlinked_key_is_refused(ws):  # I9
    key = ws.path("redaction.key")
    copy = ws.path("redaction.copy")
    copy.write_bytes(key.read_bytes())
    key.unlink()
    key.symlink_to(copy)
    with pytest.raises(QaError) as e:
        redact.load_redactor(ws)
    assert e.value.code == "redaction_key_invalid"


def test_a_fifo_at_the_key_is_refused_without_hanging(ws):  # I9: the review's probe blocked qa run
    key = ws.path("redaction.key")
    key.unlink()
    os.mkfifo(str(key))
    with pytest.raises(QaError) as e:
        finishes(lambda: redact.load_redactor(ws))
    assert e.value.code == "redaction_key_invalid"


def test_a_key_over_the_read_cap_is_refused(ws):  # I9: a regular file, never a device
    ws.path("redaction.key").write_bytes(b"k" * 100000)
    with pytest.raises(QaError) as e:
        redact.load_redactor(ws)
    assert e.value.code == "redaction_key_invalid"


# --------------------------------------------------------------------------- final review: I11, I12, M4

@pytest.mark.parametrize("word", ["inconclu", "test-execution", "structured", "suspected"])
def test_a_value_inside_a_vocabulary_word_is_not_a_secret_to_mask(word):  # I11
    assert redact._env_values({"MY_SECRET": word}, []) == {}


def test_a_value_merely_containing_a_vocabulary_word_stays_a_secret():  # I11
    assert redact._env_values({"MY_SECRET": "inconclusive-but-long-x9"}, []) == {"MY_SECRET": "inconclusive-but-long-x9"}


def test_a_secret_like_a_vocabulary_word_cannot_break_a_closed_field(trusted, actor, py, monkeypatch):  # I11
    monkeypatch.setenv("MY_SECRET", "test-execution")
    r = execution.execute(trusted, actor, execution.RunRequest(command=py.argv("pass")))
    assert r["status"] in ("passed", "not_run", "inconclusive") and r["execution"] in state.load(trusted)["executions"]


CLOSING = "<failure>secret_token=abcd1234EFGH5678</failure>"


def test_an_assignment_stops_at_a_closing_tag(ws):  # I12
    out, count = redact.load_redactor(ws, environ={}).text(CLOSING)
    assert count == 1 and out.endswith("</failure>") and "abcd1234EFGH5678" not in out


def test_a_closing_tag_survives_for_a_password_too(ws):  # I12
    out, count = redact.load_redactor(ws, environ={}).text("<m>password=hunter2222</m>")
    assert count == 1 and out.endswith("</m>") and "hunter2222" not in out


def test_a_slash_inside_a_value_is_still_part_of_it(ws):  # I12: only "</" ends it
    out, _ = redact.load_redactor(ws, environ={}).text("password=abc/def<ghi")
    assert "abc/def<ghi" not in out and "def" not in out


@pytest.mark.parametrize("before,after", [
    ('\\"', '\\"'), ("\\'", "\\'"), ("&quot;", "&quot;"), ("&apos;", "&apos;"), ("&#34;", "&#34;"), ("&#39;", "&#39;"),
], ids=["json_double", "json_single", "xml_quot", "xml_apos", "xml_34", "xml_39"])
@pytest.mark.parametrize("name", ["password", "api_key"])
def test_an_escaped_quote_does_not_hide_the_value(ws, name, before, after):  # M4
    secret = "Hunter2Gg7Zq"
    for text in ("%s=%s%s%s" % (name, before, secret, after), "%s%s%s:%s%s%s" % (before, name, before, before, secret, after)):
        out, count = redact.load_redactor(ws, environ={}).text(text)
        assert count == 1 and secret not in out and out.endswith(after), text


def test_a_plain_quoted_value_is_masked_as_before(ws):  # M4
    out, _ = redact.load_redactor(ws, environ={}).text('password="two words here"')
    assert "two words" not in out and out.endswith('"')
