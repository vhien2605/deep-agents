"""Chat front end: slash-skill expansion, project discovery, streamed text."""

from src.chat import _text, expand_skill_command, find_project_dirs


def test_known_skill_is_expanded_with_the_request():
    out = expand_skill_command("/make-build chỉ test calculator.cpp")
    assert "Follow the make-build skill" in out
    assert out.endswith("Request: chỉ test calculator.cpp")


def test_skill_request_may_start_on_a_new_line():
    out = expand_skill_command("/make-build\nchỉ test calculator.cpp")
    assert out.endswith("Request: chỉ test calculator.cpp")


def test_bare_skill_gets_a_default_request():
    assert "Request: Generate unit tests" in expand_skill_command("/make-build")


def test_unknown_or_unsafe_names_pass_through():
    for message in ("/no-such-skill do it", "/../src x", "/test 90", "plain text"):
        assert expand_skill_command(message) == message


def test_find_projects_ranks_name_matches_and_skips_vendored_dirs(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "Makefile").write_text("")
    (tmp_path / "apps" / "calc-web").mkdir(parents=True)
    (tmp_path / "apps" / "calc-web" / "pom.xml").write_text("")
    (tmp_path / "calcs" / "lib").mkdir(parents=True)
    (tmp_path / "calcs" / "lib" / "pyproject.toml").write_text("")
    (tmp_path / "node_modules" / "calc").mkdir(parents=True)
    (tmp_path / "node_modules" / "calc" / "Makefile").write_text("")

    found = find_project_dirs(tmp_path, "CALC")
    assert [(p["path"], p["language"]) for p in found] == [
        (str(tmp_path / "apps" / "calc-web"), "java"),
        (str(tmp_path / "calc"), "cpp"),
        (str(tmp_path / "calcs" / "lib"), "python"),  # path match ranks last
    ]


def test_streamed_text_blocks_join_without_spaces():
    content = [{"type": "text", "text": "Xin"}, {"type": "tool_use", "id": "t"}, {"type": "text", "text": "chào"}]
    assert _text(content) == "Xinchào"
