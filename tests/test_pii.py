from srotaai import pii


def test_detects_and_redacts_indian_identifiers():
    text = "Call me on +91 9876543210, PAN ABCDE1234F, mail ravi.k@example.com"
    types = {h.type for h in pii.detect(text)}
    assert {"PHONE_IN", "PAN", "EMAIL"} <= types
    red = pii.redact(text)
    for raw in ("9876543210", "ABCDE1234F", "ravi.k@example.com"):
        assert raw not in red


def test_clean_text_has_no_hits():
    assert pii.detect("Took paracetamol and the headache went away.") == []
