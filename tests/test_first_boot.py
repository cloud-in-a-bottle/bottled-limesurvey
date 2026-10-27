"""First-boot smoke tests.

The app is public (public_paths = ["/"]): anonymous respondents reach surveys,
while the owner is auto-logged into the admin panel via the router-injected
X-OpenHost-Is-Owner header (Authwebserver plugin).

- ``stack.url`` goes through the router; requests carrying the owner's router
  session cookie (``_owner_session``) get the owner header — this is the owner's view.
- requests without that cookie are the anonymous visitor's view (the app is
  public, so the router passes them through without the owner header).
  ``stack.app_url`` hits the container directly, bypassing the router.

LimeSurvey builds absolute redirect URLs from HOST_INFO
(https://{app}.{zone}), which is correct in production but not reachable from
the test host, so the requests-based helpers rewrite that origin back to the
base URL under test. It also marks its cookies Secure (Apache sets HTTPS=on),
so the test sessions send Secure cookies over the plain-http test URLs.
"""

import re
import time
from urllib.parse import urljoin

import requests
from openhost_test_harness import OpenhostStack
from playwright.sync_api import Browser

LOGIN_PATH = "/index.php/admin/authentication/sa/login"


def _external_origin(stack: OpenhostStack) -> str:
    return f"https://{stack.manifest.name}.{stack.zone_domain}"


class _Session(requests.Session):
    """Sends Secure cookies over plain http, like a browser does for localhost."""

    def prepare_request(self, request: requests.Request) -> requests.PreparedRequest:
        for cookie in self.cookies:
            cookie.secure = False
        return super().prepare_request(request)


def _owner_session(stack: OpenhostStack) -> requests.Session:
    """A fresh session (no LimeSurvey cookies yet) carrying the owner's router login.

    Copies headers too: the router only honors the owner cookie alongside browser Fetch-Metadata headers.
    """
    sess = _Session()
    sess.headers.update(stack.owner_session.headers)
    sess.cookies.update(stack.owner_session.cookies)
    return sess


def _get_following_redirects(
    sess: requests.Session, stack: OpenhostStack, base: str, path: str
) -> requests.Response:
    ext = _external_origin(stack)
    url = base + path
    for _ in range(8):
        resp = sess.get(url, allow_redirects=False, timeout=30)
        if resp.status_code not in (301, 302, 303):
            return resp
        loc = resp.headers["Location"]
        url = base + loc[len(ext):] if loc.startswith(ext) else urljoin(url, loc)
    raise AssertionError(f"too many redirects, last at {url}")


def _wait_for_sso_provisioning(stack: OpenhostStack) -> None:
    """The start script activates the Authwebserver plugin in the background
    once the installer has created the schema; allow a few seconds for it."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        resp = _get_following_redirects(_owner_session(stack), stack, stack.url, "/index.php/admin")
        if 'name="password"' not in resp.text:
            return
        time.sleep(2)
    raise AssertionError("owner SSO never became active — still seeing the login form")


def test_health(stack: OpenhostStack) -> None:
    """health.php verifies DB connectivity and that the schema is installed."""
    resp = requests.get(stack.app_url + "/health.php", timeout=10)
    assert resp.status_code == 200, resp.text
    assert resp.text.strip() == "ok"


def test_anonymous_can_reach_survey_pages(stack: OpenhostStack) -> None:
    """Anonymous visitors get through the router (public_paths = ["/"]) to the
    survey front page, since respondents are anonymous."""
    resp = _get_following_redirects(_Session(), stack, stack.url, "/")
    assert resp.status_code == 200, f"{resp.status_code} at {resp.url}: {resp.text[:500]}"
    assert "survey" in resp.text.lower()


def test_anonymous_admin_gets_login_form(stack: OpenhostStack) -> None:
    """Without the owner's router login the admin panel must demand
    credentials, even when the visitor forges the owner header."""
    _wait_for_sso_provisioning(stack)  # the forged header would only matter once SSO is on
    for headers in ({}, {"X-OpenHost-Is-Owner": "true"}):
        sess = _Session()
        sess.headers.update(headers)
        resp = _get_following_redirects(sess, stack, stack.url, "/index.php/admin")
        assert resp.status_code == 200, f"{resp.status_code} at {resp.url}: {resp.text[:500]}"
        assert 'name="password"' in resp.text, f"expected the LimeSurvey login form with {headers}"


def test_owner_is_autologged_into_admin(stack: OpenhostStack, browser: Browser) -> None:
    """Through the router (owner header injected) the admin panel opens with
    no login form."""
    _wait_for_sso_provisioning(stack)

    sess = _owner_session(stack)
    resp = _get_following_redirects(sess, stack, stack.url, "/index.php/admin")
    assert resp.status_code == 200
    assert 'name="password"' not in resp.text, "owner hit the login form — SSO failed"
    assert "/dashboard/" in resp.url, f"did not land on the dashboard: {resp.url}"

    # Render the dashboard in a real browser with the SSO session's cookies.
    # (The browser can't follow the login redirect itself: LimeSurvey redirects
    # to the absolute production origin, unreachable from the test host.)
    context = browser.new_context()
    context.add_cookies(
        [{"name": c.name, "value": c.value, "url": stack.url} for c in sess.cookies]
    )
    page = context.new_page()
    try:
        page.goto(stack.url + "/index.php/dashboard/view", wait_until="networkidle")
        page.screenshot(path="tests/_admin_dashboard.png", full_page=True)
        assert page.locator("input[name='password']").count() == 0
        assert stack.owner_username in page.content()
    finally:
        context.close()


def test_owner_root_redirects_to_admin(stack: OpenhostStack) -> None:
    """The app root is the confusing public survey list — the owner should be
    taken to the admin dashboard instead."""
    _wait_for_sso_provisioning(stack)

    # The Location must be the absolute external origin — the router forwards
    # with an internal Host header and doesn't rewrite Location, so anything
    # Host-derived would send the browser to 127.0.0.1.
    hop = _owner_session(stack).get(stack.url + "/", allow_redirects=False, timeout=30)
    assert hop.status_code == 302
    assert hop.headers["Location"] == _external_origin(stack) + "/index.php/admin", (
        f"redirect leaks an internal origin: {hop.headers['Location']}"
    )

    resp = _get_following_redirects(_owner_session(stack), stack, stack.url, "/")
    assert resp.status_code == 200
    assert "/dashboard/" in resp.url, f"owner did not land on the dashboard: {resp.url}"
    assert 'name="password"' not in resp.text


def test_site_contact_is_seeded(stack: OpenhostStack) -> None:
    """The empty public survey list shows the site contact; the provisioner
    replaces LimeSurvey's placeholder with the owner's name/email."""
    # The provisioner seeds it in the background once the installer is done, so
    # poll: on a slow runner the page can be requested before that.
    deadline = time.monotonic() + 120
    while True:
        resp = _get_following_redirects(_Session(), stack, stack.app_url, "/")
        seeded = (
            resp.status_code == 200
            and "your-email@example.net" not in resp.text
            and "Your Name" not in resp.text
            and stack.owner_username in resp.text
        )
        if seeded or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert resp.status_code == 200
    assert "your-email@example.net" not in resp.text, "placeholder email still showing"
    assert "Your Name" not in resp.text, "placeholder site admin name still showing"
    assert stack.owner_username in resp.text


def test_admin_directory_link_stays_external(stack: OpenhostStack) -> None:
    """The editor's home link is /admin, a real directory: Apache's mod_dir
    would 301 it to /admin/ on the internal host (DirectorySlash Off fixes it)."""
    _wait_for_sso_provisioning(stack)

    resp = _owner_session(stack).get(stack.url + "/admin", allow_redirects=False, timeout=30)
    location = resp.headers.get("Location", "")
    assert "127.0.0.1" not in location, f"/admin redirects to the internal host: {location}"
    if resp.status_code in (301, 302, 303):
        assert location.startswith((_external_origin(stack), "/")), f"/admin redirects to {location}"


def test_state_in_app_data(stack: OpenhostStack) -> None:
    """The bundled MariaDB and LimeSurvey's upload dir live in the app data
    dir, not in the container."""
    _wait_for_sso_provisioning(stack)

    # Only check the top-level dirs: what's inside belongs to the container's mysql/www-data users.
    assert (stack.data_dir / "mysql").is_dir(), "no MariaDB datadir in the app data dir"
    assert (stack.data_dir / "upload").is_dir(), "upload/ wasn't moved to the app data dir"


def test_editor_rest_api_works_through_router(stack: OpenhostStack) -> None:
    """The new survey editor authenticates /rest calls with a bearer token
    minted via editorLink (LS_AUTH_INIT cookie). LimeSurvey reads the
    Authorization header case-sensitively while the router lowercases header
    names, so without the Apache re-casing fix every /rest call 401s and the
    editor hangs on "Checking permissions"."""
    import json
    from urllib.parse import unquote

    _wait_for_sso_provisioning(stack)

    sess = _owner_session(stack)
    resp = _get_following_redirects(sess, stack, stack.url, "/index.php/admin")
    assert resp.status_code == 200

    link = sess.get(
        stack.url + "/index.php/editorLink/index?route=survey%2F1",
        allow_redirects=False,
        timeout=30,
    )
    raw = link.cookies.get("LS_AUTH_INIT")
    assert raw, "editorLink did not set the LS_AUTH_INIT token cookie"
    token = json.loads(unquote(raw))["token"]

    rest = _owner_session(stack).get(
        stack.url + "/rest/v1/user-permissions",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    assert rest.status_code == 200, (
        f"editor REST call failed through the router: {rest.status_code}"
    )
    assert "permissions" in rest.json()


def test_anonymous_password_login_still_works(stack: OpenhostStack) -> None:
    """The generated credentials remain a fallback (Authwebserver is not the
    default auth method), e.g. for additional admin users."""
    admin_password = (stack.data_dir / "secrets" / "admin_password").read_text().strip()

    sess = _Session()
    login = _get_following_redirects(sess, stack, stack.app_url, "/index.php/admin")
    assert login.status_code == 200

    hidden = {}
    for tag in re.findall(r"<input[^>]*type=['\"]hidden['\"][^>]*>", login.text):
        name = re.search(r"name=['\"]([^'\"]+)['\"]", tag)
        value = re.search(r"value=['\"]([^'\"]*)['\"]", tag)
        if name:
            hidden[name.group(1)] = value.group(1) if value else ""
    assert "YII_CSRF_TOKEN" in hidden, f"no CSRF token on login page: {sorted(hidden)}"

    resp = sess.post(
        stack.app_url + LOGIN_PATH,
        data={
            **hidden,
            "user": stack.owner_username,
            "password": admin_password,
            "login_submit": "login",
        },
        allow_redirects=False,
        timeout=30,
    )
    assert resp.status_code in (301, 302, 303), (
        f"login POST did not redirect (status {resp.status_code}) — "
        "credentials likely rejected"
    )

    dashboard = _get_following_redirects(sess, stack, stack.app_url, "/index.php/admin")
    assert dashboard.status_code == 200
    assert "authentication/sa/login" not in dashboard.url, "bounced back to login"
    assert 'name="password"' not in dashboard.text, "login form still showing"
