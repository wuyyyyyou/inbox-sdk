from mail_agent.local_query import parse_local_query


def test_field_qualified_quoted_phrases_and_parentheses():
    parsed = parse_local_query('subject:"project alpha" AND body:"paid invoice"')

    assert parsed.error == ""
    assert parsed.expression is not None
    assert parsed.expression.kind == "term"
    assert parsed.expression.term is not None
    assert parsed.expression.term.value == "re: collaboration: meet anna"
    assert [node.term.value for node in parsed.expression.children if node.term] == [
        "project alpha",
        "paid invoice",
    ]
    assert parse_local_query(
        '(subject:"project alpha" OR body:"paid invoice") AND invoice'
    ).error == ""


def test_unquoted_field_values_keep_existing_spaces():
    parsed = parse_local_query("subject:Re: Collaboration: Meet Anna")

    assert parsed.error == ""
    assert parsed.expression is not None
    assert parsed.expression.kind == "and"
