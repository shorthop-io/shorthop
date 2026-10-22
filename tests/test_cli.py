"""The operator CLI: the way into a fresh install without Google SSO."""
import io

import pytest

from app import cli
from app.auth import verify_password
from app.db import SessionLocal
from app.models import AuditEntry, User


def _user(email):
    SessionLocal.remove()
    return SessionLocal.query(User).filter(User.email == email).first()


def test_admin_creates_a_user_who_can_sign_in(client):
    assert cli.make_admin("Ops@Example.com", "correct horse battery") == "created"
    u = _user("ops@example.com")
    assert u.role == "admin" and not u.disabled
    assert verify_password("correct horse battery", u.password_hash)
    r = client.post("/login", data={"email": "ops@example.com", "password": "correct horse battery", "next": "/"})
    assert r.status_code in (302, 303) and r.headers["Location"].rstrip("/") in ("", "http://localhost")
    assert client.get("/api/v1/me").status_code == 200        # the session is real
    bad = client.post("/login", data={"email": "ops@example.com", "password": "wrong password!", "next": "/"})
    assert bad.status_code not in (302, 303)
    assert SessionLocal.query(AuditEntry).filter(AuditEntry.action == "user.admin.cli").count() == 1


def test_admin_promotes_and_re_enables_an_existing_user():
    SessionLocal.add(User(email="ann@example.com", name="Ann", role="viewer", disabled=True))
    SessionLocal.commit()
    assert cli.make_admin("ann@example.com", "another long secret") == "updated"
    u = _user("ann@example.com")
    assert (u.role, u.disabled, u.name) == ("admin", False, "Ann")


@pytest.mark.parametrize("email,pw", [("not-an-email", "long enough pass"), ("a@b.co", "short")])
def test_admin_refuses_bad_input(email, pw):
    with pytest.raises(ValueError):
        cli.make_admin(email, pw)


def test_password_comes_from_stdin_never_an_argument(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped in password\n"))
    assert cli.main(["admin", "pipe@example.com", "--password-stdin"]) == 0
    assert "piped in password" not in capsys.readouterr().out
    assert verify_password("piped in password", _user("pipe@example.com").password_hash)


def test_without_a_terminal_it_says_how_instead_of_a_traceback(monkeypatch):
    # `kubectl exec -i … < password` without --password-stdin: no prompt is possible.
    monkeypatch.setattr("sys.stdin", io.StringIO("piped in password\n"))
    with pytest.raises(SystemExit) as e:
        cli.main(["admin", "notty@example.com"])
    assert "--password-stdin" in str(e.value)
