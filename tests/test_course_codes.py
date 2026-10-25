import pytest

from studentflow.course_codes import course_codes


@pytest.mark.parametrize("text,expected", [
    ("DSS5105 research outline", {"dss5105"}),
    ("dss5105 research outline", {"dss5105"}),
    ("Dss5105 research outline", {"dss5105"}),
    ("DSX1002 / CS101S", {"dsx1002", "cs101s"}),
    ("DSS 5105 and DSS-5101", {"dss5105", "dss5101"}),
    ("DSS\t5105; DSX - 1002", {"dss5105", "dsx1002"}),
    ("Course: dss 5105", {"dss5105"}),
    ("Module code dsx-1002", {"dsx1002"}),
    ("course ID: cs 101S", {"cs101s"}),
    ("ＭＡＴＨ１００１: homework", {"math1001"}),
    ("(DSS5105), [DSS5101].", {"dss5105", "dss5101"}),
    ("DSS5105: Submit a 200-word summary by 30 October 2026.", {"dss5105"}),
])
def test_course_ids_have_stable_normalized_identity(text, expected):
    assert course_codes(text) == expected


@pytest.mark.parametrize("text", [
    "Submit 200-word interpretation of campus survey results",
    "Write 200 words interpreting the campus survey results",
    "A written interpretation of about 200 words",
    "Use a maximum 200 words",
    "Submit 200",
    "Write 200",
    "about 200",
    "SUBMIT 200-WORD INTERPRETATION",
    "WRITE 200 WORDS",
    "ALLOW 120 MINUTES",
    "Page 200 and pages 300",
    "PAGE200 / PAGES300",
    "Year 2026 / years 2026",
    "YEAR2026 / YEARS2026",
    "30 October 2026 at 18:00 Singapore time",
    "NOV2026 / Nov2026 / nov2026",
    "OCTOBER2026 / SEPT2026 / FEB2027",
    "NOV 2026 and OCT-2026",
    "dss 5105",
    "dss-5105",
    "Dss 5105",
    "DSS\n5105",
    "prefix_DSS5105_suffix",
    "DSS510500",
    "DSS51",
    "DSS5105AA",
    "",
])
def test_prose_quantities_dates_and_ambiguous_forms_are_not_course_ids(text):
    assert course_codes(text) == set()


@pytest.mark.parametrize("value", [None, 200, [], {}])
def test_nontext_is_not_course_metadata(value):
    assert course_codes(value) == set()


def test_word_count_paraphrases_do_not_create_false_course_conflicts():
    body = "DSS5105: Submit 200-word interpretation of the campus survey results."
    attachment = "DSS 5105: Write about 200 words explaining the campus survey."
    assert course_codes(body) == course_codes(attachment) == {"dss5105"}


def test_true_course_difference_survives_prose_quantity_filtering():
    first = "DSS5105: Submit 200-word interpretation by 30 October 2026."
    second = "DSS5101: Write about 200 words by 30 October 2026."
    assert course_codes(first).isdisjoint(course_codes(second))
    assert course_codes(first) and course_codes(second)
