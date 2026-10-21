import pytest

from app.core import identity, matchers

IGNORE = ["pod", "instance"]


def test_signature_ignores_instance_labels():
    a = {"alertname": "CrashLoop", "namespace": "service", "pod": "api-7f9c"}
    b = {"alertname": "CrashLoop", "namespace": "service", "pod": "api-1a2b"}
    assert identity.signature(a, IGNORE)[0] == identity.signature(b, IGNORE)[0]
    assert identity.fingerprint("i", a) != identity.fingerprint("i", b)


def test_signature_keeps_problem_labels():
    a = {"alertname": "CrashLoop", "namespace": "service"}
    b = {"alertname": "CrashLoop", "namespace": "monitoring"}
    assert identity.signature(a, IGNORE)[0] != identity.signature(b, IGNORE)[0]


def test_alertname_is_never_dropped_even_if_listed():
    sig, kept = identity.signature({"alertname": "X", "pod": "p"}, ["alertname", "pod"])
    assert kept == {"alertname": "X"}


def test_fingerprint_is_scoped_to_integration():
    labels = {"alertname": "X"}
    assert identity.fingerprint("a", labels) != identity.fingerprint("b", labels)


def test_matchers_equal_regex_and_negation():
    labels = {"alertname": "HighCPU", "env": "lab"}
    m = matchers.validate([{"name": "alertname", "value": "High.*", "isRegex": True}])
    assert matchers.matches(m, labels)
    m = matchers.validate([{"name": "alertname", "value": "High"}])  # anchored, no partial
    assert not matchers.matches(m, labels)
    m = matchers.validate([{"name": "alertname", "value": "HighCPU"},
                           {"name": "env", "value": "live", "isEqual": False}])
    assert matchers.matches(m, labels)


def test_missing_label_reads_as_empty():
    m = matchers.validate([{"name": "alertname", "value": "X"},
                           {"name": "team", "value": "", "isEqual": True}])
    assert matchers.matches(m, {"alertname": "X"})


@pytest.mark.parametrize("bad", [
    [],
    [{"name": "alertname", "value": "x", "isEqual": False}],      # only negative
    [{"name": "alertname", "value": ".*", "isRegex": True}],       # matches empty
    [{"name": "bad-name", "value": "x"}],
    [{"name": "alertname", "value": "(", "isRegex": True}],
    [{"name": "alertname", "value": "a" * 300, "isRegex": True}],
])
def test_matchers_refuse_match_all_and_malformed(bad):
    with pytest.raises(matchers.MatcherError):
        matchers.validate(bad)
